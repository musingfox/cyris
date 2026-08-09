#!/usr/bin/env python3
"""Diff what Miniflux and the Cloudflare buffer return for the same window.

The buffer only pays off once it has accumulated: high-volume feeds hold 2-4h
per snapshot, so run this after the Worker has been polling for a full day.

    uv run --with python-dotenv python workers/rss/compare.py [hours]
"""

import asyncio
import json
import os
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

from cyris.adapters.fetch.miniflux import MinifluxClient
from cyris.adapters.fetch.miniflux_source import MinifluxSource
from cyris.adapters.fetch.rss_worker_source import CloudflareRssSource
from cyris.config import load_config

load_dotenv()

LIMIT = 1000  # above any digest limit, so truncation never masks a gap


def report(label: str, urls: set[str], articles: list) -> None:
    print(f"\nonly in {label} ({len(urls)}):")
    counts = Counter(a.source_name for a in articles if a.url in urls)
    for name, count in counts.most_common():
        print(f"  {count:3d}  {name}")
    if not counts:
        print("  —")


def append_log(path: Path, record: dict) -> None:
    """Append one JSON line so several days of parity accumulate unattended.

    Retiring Miniflux needs a run of clean days, not one good measurement — the
    first 24h comparison looked like a 73% capture rate purely because it spanned
    an outage. Delete this log and its launchd job once Miniflux is gone.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


async def main() -> None:
    hours = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 24
    cfg = load_config()
    before = datetime.now(UTC)
    after = before - timedelta(hours=hours)

    miniflux = MinifluxSource(
        MinifluxClient(cfg.app.miniflux.url, os.environ["CYRIS_MINIFLUX_API_KEY"])
    )
    buffer = CloudflareRssSource(cfg.app.rss.worker_url, cfg.app.rss.token)

    mf_articles = await miniflux.fetch_articles(
        after=after, before=before, sources=cfg.sources, aliases=cfg.aliases, limit=LIMIT
    )
    cf_articles = await buffer.fetch_articles(
        after=after, before=before, sources=cfg.sources, limit=LIMIT
    )

    mf_urls = {a.url for a in mf_articles}
    cf_urls = {a.url for a in cf_articles}

    print(f"window : last {hours}h")
    print(f"miniflux: {len(mf_urls)} urls")
    print(f"buffer  : {len(cf_urls)} urls")
    print(f"shared  : {len(mf_urls & cf_urls)}")

    report("miniflux", mf_urls - cf_urls, mf_articles)
    report("buffer", cf_urls - mf_urls, cf_articles)

    if "--log" in sys.argv:
        missing = mf_urls - cf_urls
        record = {
            "checked_at": before.isoformat(),
            "hours": hours,
            "miniflux": len(mf_urls),
            "buffer": len(cf_urls),
            "shared": len(mf_urls & cf_urls),
            "missing_from_buffer": len(missing),
            # by source, so a recurring gap names the feed instead of just a count
            "missing_by_source": dict(
                Counter(a.source_name for a in mf_articles if a.url in missing)
            ),
        }
        append_log(cfg.app.agent_vault.path / "source-parity.jsonl", record)
        print(f"\nlogged to {cfg.app.agent_vault.path / 'source-parity.jsonl'}")


asyncio.run(main())
