"""Use case: full pipeline run — fetch, store, score, digest, output."""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from cyris.domain.models import ArticleState, UsageStats
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


def _render_site(deps: "Deps", content, collected) -> dict[str, bytes]:
    """This run's pages as bytes, keyed by the path Pages will serve them at."""
    writer = deps.html_writer
    slug = f"{content.date}-{content.period}"
    pages = {f"/{slug}.html": writer.render(content)}
    if collected:
        pages[f"/{slug}-raw.html"] = writer.render_raw(content.date, content.period, collected)
    # The archive page lists every digest the site holds, this run's included.
    known = sorted({*deps.site_filenames(), *(p.lstrip("/") for p in pages)})
    pages["/index.html"] = writer.render_index(known)
    return {path: html.encode("utf-8") for path, html in pages.items()}


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
    summary: dict[str, object] = {
        "event": "run_summary",
        # Overwritten on every path that returns; an exception leaves it as it
        # is, which is what makes a crash visible in the same query as a run.
        "status": "error",
        "period": options.period,
        "dry_run": options.dry_run,
    }
    try:
        return await _run_digest(deps, options, summary)
    finally:
        summary["wall_seconds"] = round(time.monotonic() - started, 2)
        logger.info("run_summary %s", json.dumps(summary, ensure_ascii=False, default=str))


async def _run_digest(deps: "Deps", options: RunOptions, summary: dict) -> RunReport:
    """Run the full pipeline: fetch → store → score → digest → output."""
    cfg = deps.cfg
    store = deps.store
    progress = deps.on_progress

    tz = cfg.app.general.timezone
    notify = cfg.app.general.notify
    now = now_in_timezone(tz)
    window_start = now - timedelta(hours=cfg.app.general.digest_window_hours)

    # A fresh deploy has no provider until one is chosen on /settings, and the
    # digest that goes out before then is plain excerpts. Say so on every run:
    # a quietly worse digest is the failure a first deploy is most likely to hit
    # and least likely to notice.
    if deps.llm is None:
        progress(
            "WARNING: no LLM provider configured — this digest is plain excerpts, "
            "unscored and unsummarised. Choose a provider on /settings."
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

    total_usage = UsageStats(model=cfg.app.llm_provider.model or "none")

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
    if options.dry_run:
        report.rendered = deps.html_writer.render(content) if deps.html_writer else ""
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

        # HTML output (optional, non-blocking)
        if deps.html_writer is not None:
            slug = f"{content.date}-{content.period}"
            published = False
            if deps.publish_site is not None:
                # No local archive: the pages are built in memory and the site's
                # file list comes from D1. Nothing here touches the filesystem.
                try:
                    published = deps.publish_site(_render_site(deps, content, collected), slug)
                    report.html_path = Path(f"{slug}.html")  # published, not written
                except Exception as e:
                    logger.error("Failed to publish the HTML digest: %s", e)
            else:
                try:
                    report.html_path = deps.html_writer.write(content)
                    progress(f"HTML digest written to {report.html_path}")
                except Exception as e:
                    logger.error("Failed to write HTML digest: %s", e)

                # Own try/except: a broken raw page must not cost the digest its publish.
                if collected:
                    try:
                        raw = deps.html_writer.write_raw(content.date, content.period, collected)
                        progress(f"Raw page written to {raw}")
                    except Exception as e:
                        logger.error("Failed to write raw HTML page: %s", e)

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
                    # Use custom_domain for operator/self links if set, else pages.dev.
                    if cfg.app.promote.custom_domain:
                        digest_url = f"https://{cfg.app.promote.custom_domain}/{slug}"
                    else:
                        digest_url = f"https://{cfg.app.promote.pages_project}.pages.dev/{slug}"
                else:
                    publish_failed = True

    await deps.send_discord(
        notify.discord_webhook_url,
        content,
        digest_url=digest_url,
        publish_failed=publish_failed,
    )

    summary["status"] = "publish_failed" if publish_failed else "ok"
    summary["digest_url"] = digest_url

    return report
