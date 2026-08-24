"""Use case: full pipeline run — fetch, store, score, digest, output."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from cyris.domain.models import ArticleState, Tier, UsageStats
from cyris.domain.selection import count_dead_links, layer_by_score
from cyris.domain.triage import RejectReason
from cyris.service_layer.digest_pipeline import DigestPipeline
from cyris.service_layer.fetching import fetch_all_articles
from cyris.service_layer.scoring import score_in_batches
from cyris.utils.timezone import now_in_timezone

if TYPE_CHECKING:
    from cyris.bootstrap import Deps

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunOptions:
    period: str = "morning"
    dry_run: bool = False
    force: bool = False
    enable_learning: bool = True


@dataclass
class RunReport:
    status: str  # "ok" | "no_articles" | "no_pending"
    rendered: str | None = None  # dry-run render of the digest
    digest_path: Path | None = None
    html_path: Path | None = None
    raw_path: Path | None = None
    failed_sources: list[str] = field(default_factory=list)


async def run_digest(deps: "Deps", options: RunOptions) -> RunReport:
    """Run the full pipeline: fetch → store → score → digest → output."""
    cfg = deps.cfg
    store = deps.store
    progress = deps.on_progress

    tz = cfg.app.general.timezone
    notify = cfg.app.general.notify
    now = now_in_timezone(tz)
    window_start = now - timedelta(hours=cfg.app.general.digest_window_hours)

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

    if failed_sources:
        progress(f"WARNING: fetch failed for: {', '.join(failed_sources)}")

    if not articles:
        logger.warning("No articles found in time window")
        progress("No articles found. Nothing to process.")
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

    scorable = []
    for a in pending_articles:
        if a.source_tier == Tier.FAN:  # fan tier is never scored
            continue
        if "news" in a.source_tags:
            continue
        if not options.force and a.score is not None:
            continue
        scorable.append(a)

    total_usage = UsageStats(model=cfg.app.llm_provider.model or "none")

    # Load learning data if enabled
    preference_profile = None
    if options.enable_learning:
        from cyris.learn.profile import load_latest_profile

        preference_profile = load_latest_profile(cfg.app.agent_vault.path)
        if preference_profile:
            logger.info(
                "Loaded preference profile (sample_size=%d)", preference_profile.sample_size
            )

    if scorable and deps.llm is not None:
        progress(f"Scoring {len(scorable)} articles...")
        try:
            usage = await score_in_batches(
                scorable,
                deps.llm,
                preference_profile=preference_profile,
                snippet_length=cfg.app.digest.scoring_snippet_length,
                progress=progress,
                persist=None if options.dry_run else store.update_scores,
            )
            total_usage.add(usage.input_tokens, usage.output_tokens)
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
            threshold=cfg.app.vote_similarity.threshold,
            max_seeds=cfg.app.vote_similarity.max_seeds,
        )
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
        return RunReport(status="no_pending", failed_sources=failed_sources)

    # Process all articles through digest pipeline
    digest_pipeline = DigestPipeline(
        deps.llm,
        preference_profile=preference_profile,
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

    # Layer by score to extract featured articles
    content = layer_by_score(content, featured_threshold=cfg.app.routing.score_threshold)

    content.synthetic_url_count = sum(1 for a in digest_articles if a.url.startswith("newsletter:"))
    if content.synthetic_url_count:
        progress(
            f"{content.synthetic_url_count} newsletter article(s) fell back to a synthetic URL."
        )

    content.dead_link_count = count_dead_links(content)
    if content.dead_link_count:
        progress(f"This digest has {content.dead_link_count} dead link(s).")

    # Add scoring usage to content
    content.usage.add(total_usage.input_tokens, total_usage.output_tokens)
    deps.log_usage(content)

    report = RunReport(status="ok", failed_sources=failed_sources)
    digest_url = ""  # online (Cloudflare Pages) URL, set after a successful publish
    publish_failed = False
    if options.dry_run:
        report.rendered = deps.writer.render(content)
    else:
        report.digest_path = deps.writer.write(content)
        progress(f"Digest written to {report.digest_path}")

        # Update article states in store — before the raw outputs, so they show
        # this run's verdicts rather than a store snapshot taken one step early.
        url_to_state: dict[str, tuple[ArticleState, str | None]] = {}
        for url in result.accepted_urls:
            url_to_state[url] = (ArticleState.ACCEPTED, None)
        for url in result.rejected_urls:
            url_to_state[url] = (ArticleState.REJECTED, RejectReason.FILTERED)

        updated_count = store.update_states(url_to_state, digest_date=content.date)
        logger.info("Updated states for %d articles", updated_count)

        # Everything collected in the window: uncapped, unfiltered, states included.
        # None of this may raise: the Discord notification still has to go out.
        collected = []
        try:
            collected = store.load_by_time_range(start=window_start, end=load_end)
            report.raw_path = deps.writer.write_raw_list(content.date, content.period, collected)
            progress(f"Raw list written to {report.raw_path}")
        except Exception as e:
            logger.error("Failed to write raw list: %s", e)

        # HTML output (optional, non-blocking)
        if deps.html_writer is not None:
            try:
                report.html_path = deps.html_writer.write(content)
                progress(f"HTML digest written to {report.html_path}")
            except Exception as e:
                logger.error("Failed to write HTML digest: %s", e)

            # Own try/except: a broken raw page must not cost the digest its publish.
            if collected:
                try:
                    raw_html = deps.html_writer.write_raw(content.date, content.period, collected)
                    progress(f"Raw page written to {raw_html}")
                except Exception as e:
                    logger.error("Failed to write raw HTML page: %s", e)

            if (
                report.html_path is not None
                and deps.publish is not None
                and cfg.app.promote.pages_project
            ):
                slug = f"{content.date}-{content.period}"
                if deps.publish(slug):
                    # Cloudflare Pages serves the extensionless clean URL.
                    digest_url = f"https://{cfg.app.promote.pages_project}.pages.dev/{slug}"
                else:
                    publish_failed = True

    await deps.send_discord(
        notify.discord_webhook_url,
        content,
        digest_url=digest_url,
        publish_failed=publish_failed,
    )

    return report
