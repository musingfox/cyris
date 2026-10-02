"""Use case: full pipeline run — fetch, store, score, digest, output."""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from cyris.domain.models import NO_LLM_MODEL, ArticleState, UsageStats, is_degraded_run
from cyris.domain.selection import count_dead_links, layer_by_score
from cyris.domain.triage import RejectReason
from cyris.service_layer.digest_pipeline import DigestPipeline
from cyris.service_layer.fetching import fetch_all_articles
from cyris.service_layer.schedule import Period
from cyris.service_layer.scoring import score_in_batches, select_scorable
from cyris.utils.timezone import now_in_timezone

if TYPE_CHECKING:
    from cyris.bootstrap import Deps

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunOptions:
    period: Period = "morning"
    dry_run: bool = False
    force: bool = False


@dataclass
class RunReport:
    status: str  # "ok" | "no_articles" | "no_pending"
    rendered: str | None = None  # dry-run render of the digest
    html_path: Path | None = None
    failed_sources: list[str] = field(default_factory=list)


def _render_site(
    deps: "Deps", content, collected, *, raw_page: bool, degraded: bool
) -> dict[str, bytes]:
    """This run's pages as bytes, keyed by the path Pages will serve them at."""
    writer = deps.html_writer
    page = "/" + writer.digest_filename(content.date, content.period)
    pages = {page: writer.render(content, raw_page=raw_page, degraded=degraded)}
    if collected:
        raw = "/" + writer.raw_filename(content.date, content.period)
        pages[raw] = writer.render_raw(content.date, content.period, collected)
    # The archive page lists every digest the site holds, this run's included, and
    # leads with this run's issue: its title and count come from memory, not D1.
    known = sorted({*deps.site_filenames(), *(p.lstrip("/") for p in pages)})
    # Counts are decoration; the list is what Pages recovery rebuilds from. A failed
    # read costs the rows their counts, never the index its issues or the publish.
    try:
        counts = deps.archive_counts()
    except Exception as e:
        logger.error("Failed to read the archive's article counts: %s", e)
        counts = {}
    pages["/index.html"] = writer.render_index(known, content=content, counts=counts)
    # Every deploy, not only the first: a manifest rebuilt from the archive's
    # anchors would not name it.
    assets = {f"/{name}": data for name, data in writer.site_assets().items()}
    return {**{path: html.encode("utf-8") for path, html in pages.items()}, **assets}


async def run_digest(deps: "Deps", options: RunOptions) -> RunReport:
    """Run the full pipeline, and leave one line saying what it cost.

    The summary is emitted whatever happens, including the exception path — a
    run that died is the one whose numbers are worth reading. It is JSON on one
    line because the container's stdout is the only log this deployment keeps:
    `wrangler.toml` sends it to Workers Logs, which retains it for seven days.
    Nothing here is a substitute for `usage_log`, which is the permanent record;
    this is what the seven-day window is for.
    """
    started = time.monotonic()
    started_at = datetime.now(UTC)
    summary: dict[str, object] = {
        "event": "run_summary",
        # Overwritten on every path that returns; an exception leaves it as it
        # is, which is what makes a crash visible in the same query as a run.
        "status": "error",
        "period": options.period,
        "dry_run": options.dry_run,
    }
    # Held here, not read back off `summary["status"]`: a SIGTERM is a
    # CancelledError, which is not an Exception, and that run's status is also
    # "error". Alerting on the status would page somebody for a deploy.
    failure: Exception | None = None
    try:
        return await _run_digest(deps, options, summary)
    except Exception as exc:
        failure = exc
        raise
    finally:
        summary["wall_seconds"] = round(time.monotonic() - started, 2)
        logger.info("run_summary %s", json.dumps(summary, ensure_ascii=False, default=str))
        if deps.record_run is not None:
            # Raising in `finally` would replace the run's own exception, or turn
            # a return into one; the line above already holds the result.
            try:
                deps.record_run(summary)
            except Exception as e:
                logger.error("Failed to record the run: %s", e)
        # After the record. Its own guard, and one per channel inside: composing
        # the message calls str() on the run error, and a channel that raises
        # must not replace that error or skip the other channel.
        try:
            await _send_failure_alert(deps, options, summary, failure, started_at)
        except Exception as e:
            logger.error("Failed to send the failure alert: %s", e)


def _failure_alert_text(failure: Exception) -> str:
    """`RuntimeError: ...`, or just the type when the message is empty.

    `str(failure)` can itself raise. Callers keep that inside the alert guard.
    """
    message = str(failure)
    name = type(failure).__name__
    return f"{name}: {message}" if message else name


async def _send_failure_alert(
    deps: "Deps",
    options: RunOptions,
    summary: dict,
    failure: Exception | None,
    started_at: datetime,
) -> None:
    """One alert per configured channel when a run raised, or fetched nothing
    while at least one source failed.

    An empty window whose sources all answered is not an alert. Empty
    configuration does not call the sender. Each call has its own guard so one
    channel's failure still leaves the other its attempt.
    """
    if options.dry_run:
        return
    if failure is not None:
        headline = "Digest run failed"
        text = _failure_alert_text(failure)
    else:
        failed = summary.get("failed_sources") or []
        if summary.get("status") != "no_articles" or not failed:
            return
        headline = "Digest run fetched nothing"
        text = "Failed sources: " + ", ".join(failed)
    tz = deps.cfg.app.general.timezone
    subject = (
        f"{headline}: {options.period}, {started_at.astimezone(ZoneInfo(tz)):%Y-%m-%d %H:%M} {tz}"
    )
    notify = deps.cfg.app.notify
    if not notify.discord_webhook_url:
        logger.info("Failure alert: no webhook set, skipping Discord")
    else:
        try:
            await deps.send_discord_alert(notify.discord_webhook_url, subject, text)
        except Exception as e:
            logger.error("Failure alert: Discord skipped: %s", e)
    if not notify.email_to:
        logger.info("Failure alert: no email address set, skipping mail")
    elif deps.send_email_alert is None:
        logger.warning(
            "Failure alert: an email address is set, but CLOUDFLARE_ACCOUNT_ID or "
            "CLOUDFLARE_API_TOKEN is missing, so no mail can be sent"
        )
    else:
        try:
            await deps.send_email_alert(notify.email_to, notify.email_from, subject, text)
        except Exception as e:
            logger.error("Failure alert: mail skipped: %s", e)


def _worker_domain(deps: "Deps") -> str:
    """The first of the app Worker's custom domains, or "" to fall back to pages.dev.

    Pages lacks `/api/vote`, so a reader who arrives there sees no vote buttons.
    A failed lookup still falls back, and the warning keeps a token that lacks
    Workers Scripts Read from passing as a Worker with no domain.
    """
    if deps.worker_domains is None:
        return ""
    try:
        hosts = deps.worker_domains()
    except Exception as e:  # noqa: BLE001 - any failure means the same fallback
        logger.warning("Could not list the app Worker's custom domains: %s", e)
        return ""
    if len(hosts) > 1:
        logger.info("The app Worker has %s; the digest link uses %s", ", ".join(hosts), hosts[0])
    return hosts[0] if hosts else ""


async def _run_digest(deps: "Deps", options: RunOptions, summary: dict) -> RunReport:
    """Run the full pipeline: fetch → store → score → digest → output."""
    cfg = deps.cfg
    store = deps.store
    progress = deps.on_progress

    tz = cfg.app.general.timezone
    notify = cfg.app.notify
    now = now_in_timezone(tz)
    window_start = now - timedelta(hours=cfg.app.general.digest_window_hours)

    # A provider whose key is missing builds no client, and its digest is plain
    # excerpts. Say so on every run: a quietly worse digest is the failure least
    # likely to be noticed. Provider none is the same digest by choice.
    llm_cfg = cfg.app.llm_provider
    if deps.llm is None and llm_cfg.chooses_no_llm:
        progress("LLM provider none: this digest lists plain excerpts, by choice.")
    elif deps.llm is None:
        progress(
            f"WARNING: no LLM client for {llm_cfg.provider} — this digest is plain excerpts, "
            "unscored and unsummarised. Set its API key, or choose another provider "
            "on /settings."
        )

    # Pull promote-button clicks from the cloud Worker (non-blocking on failure)
    if deps.sync_promotions is not None:
        try:
            vote_count = await asyncio.to_thread(deps.sync_promotions)
            if vote_count:
                progress(f"Synced {vote_count} digest vote(s).")
        except Exception as e:
            logger.warning("Promotion sync failed: %s", e)

    # Fetch
    articles, failed_sources = await fetch_all_articles(
        fetch_sources=deps.fetch_sources,
        after=window_start,
        before=now,
        sources=cfg.sources,
        limit=cfg.app.digest.max_articles_per_digest,
    )

    summary["fetched"] = len(articles)
    summary["failed_sources"] = failed_sources

    if failed_sources:
        progress(f"WARNING: fetch failed for: {', '.join(failed_sources)}")

    if not articles:
        logger.warning("No articles found in time window")
        progress("No articles found. Nothing to process.")
        summary["status"] = "no_articles"
        return RunReport(status="no_articles", failed_sources=failed_sources)

    # Save articles to store
    if not options.dry_run:
        save_result = store.save(articles)
        logger.info(
            "Saved %d new articles to store (%d skipped duplicates)",
            save_result.saved_count,
            save_result.skipped_count,
        )

    # Articles saved in this run get a first_seen_at later than the `now`
    # captured at run start, and load_by_time_range's end bound is exclusive —
    # take a fresh end bound so this run's own articles are included.
    load_end = datetime.now(UTC)

    # Score unscored PENDING non-news articles
    state_filter = None if options.force else ArticleState.PENDING
    pending_articles = store.load_by_time_range(
        start=window_start,
        end=load_end,
        state_filter=state_filter,
    )[: cfg.app.digest.max_articles_per_digest]

    scorable = select_scorable(pending_articles, force=options.force)

    total_usage = UsageStats(model=cfg.app.llm_provider.model or NO_LLM_MODEL)

    persist_tags = None
    if not options.dry_run and deps.tag_store is not None:

        def persist_tags(url_to_tags) -> None:
            try:
                deps.tag_store.save(url_to_tags)
            except Exception as e:
                logger.warning("Failed to persist scoring tags: %s", e)

    if scorable and deps.llm is not None:
        progress(f"Scoring {len(scorable)} articles...")
        try:
            usage = await score_in_batches(
                scorable,
                deps.llm,
                snippet_length=cfg.app.digest.scoring_snippet_length,
                progress=progress,
                persist=None if options.dry_run else store.update_scores,
                persist_tags=persist_tags,
            )
            total_usage.merge(usage)
        except Exception:
            logger.warning("Scoring failed; continuing without scores", exc_info=True)
    elif scorable:
        logger.info("No LLM configured; skipping scoring for %d articles", len(scorable))

    # Reload pending articles after scoring
    pending_articles = store.load_by_time_range(
        start=window_start,
        end=load_end,
        state_filter=state_filter,
    )[: cfg.app.digest.max_articles_per_digest]

    # Vote similarity runs over every candidate, not just the scored ones: the
    # scorer skips news, and the class that drew the first downvote is news-tagged.
    if cfg.app.vote_similarity.enabled and deps.embedder is not None:
        from cyris.service_layer.vote_similarity import judge_by_votes

        similarity = await judge_by_votes(
            store,
            deps.embedder,
            pending_articles,
            threshold=deps.embedding_threshold,
            max_seeds=cfg.app.vote_similarity.max_seeds,
        )
        # `Embedder.usage` exists because `embed-compare` needs it; until this
        # line a digest run embedded ~600 texts and reported none of it.
        summary["embedding"] = deps.embedder.usage.as_dict()
        summary["suppressed"] = len(similarity.suppressed_urls)

        if similarity.suppressed_urls:
            dropped = set(similarity.suppressed_urls)
            pending_articles = [a for a in pending_articles if a.url not in dropped]
            progress(f"Vote similarity suppressed {len(dropped)} article(s).")
        elif not similarity.ran:
            logger.info("Vote similarity skipped: %s", similarity.skipped_reason)

    article_scores = {a.url: a.score for a in pending_articles if a.score is not None}
    digest_articles = [a.to_article() for a in pending_articles]

    if not digest_articles:
        progress("No pending articles to process.")
        summary["status"] = "no_pending"
        return RunReport(status="no_pending", failed_sources=failed_sources)

    # Process all articles through digest pipeline
    digest_pipeline = DigestPipeline(
        deps.llm,
        max_digest_output=cfg.app.digest.max_articles_per_digest_output,
        summarize_snippet_length=cfg.app.digest.summarize_snippet_length,
        filter_snippet_length=cfg.app.digest.filter_snippet_length,
        score_threshold=cfg.app.routing.summarize_score_threshold,
        output_language=cfg.app.digest.output_language,
        style_prompt=cfg.app.digest.style_prompt,
    )
    result = await digest_pipeline.process(
        digest_articles,
        cfg.sources,
        period=options.period,
        timezone=tz,
        article_scores=article_scores,
    )
    content = result.content
    if not options.dry_run and deps.tag_store is not None and result.url_to_tags:
        try:
            deps.tag_store.save(result.url_to_tags)
        except Exception as e:
            logger.warning("Failed to persist cluster tags: %s", e)
    # Deliberately no empty-records guard: a re-run that clustered nothing must
    # clear the window's rows, not leave a previous run's stories looking current.
    if not options.dry_run and deps.story_store is not None:
        try:
            deps.story_store.save(content.date, content.period, result.story_records)
        except Exception as e:
            logger.warning("Failed to persist story membership: %s", e)

    # Layer by score to extract featured articles
    content = layer_by_score(
        content,
        featured_threshold=cfg.app.routing.score_threshold,
        max_featured=cfg.app.digest.max_featured,
    )

    content.synthetic_url_count = sum(1 for a in digest_articles if a.url.startswith("newsletter:"))
    if content.synthetic_url_count:
        progress(
            f"{content.synthetic_url_count} newsletter article(s) fell back to a synthetic URL."
        )

    content.dead_link_count = count_dead_links(content)
    if content.dead_link_count:
        progress(f"This digest has {content.dead_link_count} dead link(s).")

    # Add scoring usage to content
    content.usage.merge(total_usage)
    summary["llm"] = content.usage.model_dump()
    # Only this path has usage to judge; the early returns leave the key absent.
    degraded = is_degraded_run(content.usage, chooses_no_llm=llm_cfg.chooses_no_llm)
    summary["degraded"] = degraded
    summary["received"] = content.articles_received
    summary["included"] = content.articles_included

    # Its own try/except, like every call below it: this one used to be the
    # single unguarded step between a finished digest and its publish, so a D1
    # hiccup here cost the period its digest rather than its usage row.
    try:
        deps.log_usage(content)
    except Exception as e:
        logger.error("Failed to log usage: %s", e)

    report = RunReport(status="ok", failed_sources=failed_sources)
    digest_url = ""  # online (Cloudflare Pages) URL, set after a successful publish
    publish_failed = False
    raw_page = False
    if options.dry_run:
        report.rendered = (
            deps.html_writer.render(content, degraded=degraded) if deps.html_writer else ""
        )
    else:
        # Update article states in store — before the raw outputs, so they show
        # this run's verdicts rather than a store snapshot taken one step early.
        url_to_state: dict[str, tuple[ArticleState, str | None]] = {}
        for url in result.accepted_urls:
            url_to_state[url] = (ArticleState.ACCEPTED, None)
        for url in result.rejected_urls:
            url_to_state[url] = (ArticleState.REJECTED, RejectReason.FILTERED)

        updated_count = store.update_states(url_to_state, digest_date=content.date)
        logger.info("Updated states for %d articles", updated_count)

        # The raw page: what this run judged, plus what is still pending. The 24h
        # window overlaps the previous run's, so rows judged there are left off —
        # they were on that run's raw page. Nothing in the window escapes every raw
        # page: a row stays pending (and listed) until some run judges it.
        # ponytail: a run whose publish failed has judged rows but no live raw page;
        # they are gone from the listing along with that digest.
        # None of this may raise: the Discord notification still has to go out.
        collected = []
        try:
            collected = [
                a
                for a in store.load_by_time_range(start=window_start, end=load_end)
                if a.url in url_to_state or a.state == ArticleState.PENDING
            ]
        except Exception as e:
            logger.error("Failed to load the window's collected articles: %s", e)

        raw_page = deps.html_writer is not None and bool(collected)
        if raw_page and deps.publish_site is None:
            # Raw goes first so the archive index the digest write regenerates
            # already sees it and offers its All articles entry. Own try/except:
            # a broken raw page must not cost the digest its publish.
            try:
                raw = deps.html_writer.write_raw(content.date, content.period, collected)
                progress(f"Raw page written to {raw}")
            except Exception as e:
                logger.error("Failed to write raw HTML page: %s", e)
                raw_page = False

        # After the last change to `content` and the raw-page decision, before any
        # page is written or published: the row must reproduce the page the reader
        # gets, and a failed publish must not cost the period its content.
        if deps.digest_store is not None:
            try:
                deps.digest_store.save(content, raw_page=raw_page)
            except Exception as e:
                logger.error("Failed to store the digest content: %s", e)
                summary["digest_store_error"] = str(e)

        # HTML output (optional, non-blocking)
        if deps.html_writer is not None:
            filename = deps.html_writer.digest_filename(content.date, content.period)
            slug = Path(filename).stem
            published = False
            if deps.publish_site is not None:
                # No local archive: the pages are built in memory and the site's
                # file list comes from D1. Nothing here touches the filesystem.
                try:
                    published = deps.publish_site(
                        _render_site(
                            deps, content, collected, raw_page=raw_page, degraded=degraded
                        ),
                        slug,
                    )
                    report.html_path = Path(filename)  # published, not written
                except Exception as e:
                    logger.error("Failed to publish the HTML digest: %s", e)
            else:
                try:
                    report.html_path = deps.html_writer.write(
                        content, raw_page=raw_page, degraded=degraded
                    )
                    progress(f"HTML digest written to {report.html_path}")
                except Exception as e:
                    logger.error("Failed to write HTML digest: %s", e)

                if (
                    report.html_path is not None
                    and deps.publish is not None
                    and cfg.app.promote.pages_project
                ):
                    published = deps.publish(slug)

            if deps.publish_site is not None or (
                deps.publish is not None and cfg.app.promote.pages_project
            ):
                if published:
                    # Cloudflare Pages serves the extensionless clean URL.
                    host = cfg.app.promote.custom_domain or _worker_domain(deps)
                    if host:
                        digest_url = f"https://{host}/{slug}"
                    else:
                        digest_url = f"https://{cfg.app.promote.pages_project}.pages.dev/{slug}"
                else:
                    publish_failed = True

    if not notify.discord_webhook_url:
        # A run that notifies nobody looks exactly like a run that notified
        # everybody, from the log. Say which one it was.
        logger.info("No Discord webhook configured — skipping the digest notification")

    await deps.send_discord(
        notify.discord_webhook_url,
        content,
        digest_url=digest_url,
        publish_failed=publish_failed,
        degraded=degraded,
    )

    if not notify.email_to:
        logger.info("No email address configured — skipping the digest mail")
    elif deps.send_email is None:
        logger.warning(
            "An email address is set, but CLOUDFLARE_ACCOUNT_ID or CLOUDFLARE_API_TOKEN "
            "is missing, so no mail can be sent"
        )
    else:
        await deps.send_email(
            notify.email_to,
            notify.email_from,
            content,
            digest_url=digest_url,
            publish_failed=publish_failed,
            raw_page=raw_page,
            degraded=degraded,
        )

    summary["status"] = "publish_failed" if publish_failed else "ok"
    summary["digest_url"] = digest_url

    return report
