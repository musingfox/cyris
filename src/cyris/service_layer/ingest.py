"""Poll the feeds and store what they hold, without scoring or publishing.

The local edition's RSS buffer: a feed keeps only a few hours of entries, and the
digest reads the store's window by `first_seen_at`, so storing every hour lets the
digest see what a busy feed dropped before the digest hour (ADR-0001's measurement).
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from cyris.service_layer.fetching import fetch_all_articles
from cyris.service_layer.ports import ArticleRepository, FetchSource
from cyris.utils.timezone import now_in_timezone

if TYPE_CHECKING:
    from cyris.config import Config

logger = logging.getLogger(__name__)


@dataclass
class IngestReport:
    status: str  # "ok" | "failed"
    fetched: int = 0
    saved: int = 0
    skipped: int = 0
    failed_sources: list[str] = field(default_factory=list)


async def ingest(
    cfg: "Config", store: ArticleRepository, fetch_sources: list[FetchSource]
) -> IngestReport:
    started_at = datetime.now(UTC)
    now = now_in_timezone(cfg.app.general.timezone)
    articles, failed_sources = await fetch_all_articles(
        fetch_sources=fetch_sources,
        after=now - timedelta(hours=cfg.app.general.digest_window_hours),
        before=now,
        sources=cfg.sources,
    )
    report = IngestReport(
        status="failed" if failed_sources else "ok",
        fetched=len(articles),
        failed_sources=failed_sources,
    )
    if articles:
        saved = store.save(articles)
        report.saved, report.skipped = saved.saved_count, saved.skipped_count
    summary = {"event": "ingest_summary", "started_at": started_at.isoformat(), **vars(report)}
    logger.info("ingest_summary %s", json.dumps(summary))
    return report
