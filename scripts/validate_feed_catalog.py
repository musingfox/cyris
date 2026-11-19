"""Check every entry of the onboarding feed catalog against both RSS parsers.

A feed is collectable only when it parses under the rss Worker's `parseFeed`
(`workers/rss/src/parse.js`) *and* feedparser (`RssSource`, the no-Worker fallback)
and has at least one article inside the 8-day window `docs/milestones/
cold-start-onboarding.md` sets — an HTTP 200 with a body neither parser can read, or
one that is stale, does not count. The two parsers must agree independently because
onboarding writes votes against whichever path a deployment runs, and a feed only one
parser reads would seed votes a real run's own fetch could never reproduce.

This is a reviewer-run gate, not a pytest file (`tests/CLAUDE.md` R3): it asks whether
today's feeds are still up, which is a fact about the network, not about the code. The
code itself — that both parsers run, and that recency is counted the same way a wizard
session would see it — is what `tests/test_feed_catalog_validator.py` holds fixed, against
a local HTTP stub and the real `parse.js`, never a real feed.

Needs `node` and the rss Worker's dependencies: run `bun install` in `workers/rss/`
first, or every feed reports a `parse.js` error.

Usage:
    uv run python scripts/validate_feed_catalog.py
    uv run python scripts/validate_feed_catalog.py --json
    uv run python scripts/validate_feed_catalog.py --only "報導者" --only "BBC News"
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import feedparser

from cyris.adapters.http_client import HttpClient
from cyris.domain.models import SourceConfig

# A fetcher takes a URL and returns the response body, or raises.
Fetcher = Callable[[str], Awaitable[bytes]]

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = REPO_ROOT / "src" / "cyris" / "onboarding_feeds.json"
RSS_WORKER_DIR = REPO_ROOT / "workers" / "rss"

# docs/milestones/cold-start-onboarding.md: collected only when a feed has a new
# article inside this window; an HTTP 200 with nothing recent does not count.
RECENT_WINDOW_DAYS = 8

# workers/rss/src/buffer.js polls 4 feeds per batch; mirrored here so a run of this
# script is no harder on a publisher's host than production's own poll is.
MAX_CONCURRENT_FEEDS = 4

# The Worker's own parser, run out-of-process so this script needs no Python port of
# fast-xml-parser's entity handling. Reads the feed body on stdin, the source name as
# the one positional argument, and prints parseFeed's rows as JSON.
_WORKER_PARSE_SCRIPT = """
import { parseFeed } from './src/parse.js';
let data = '';
process.stdin.setEncoding('utf8');
for await (const chunk of process.stdin) data += chunk;
console.log(JSON.stringify(parseFeed(data, process.argv[1])));
"""


@dataclass
class FeedResult:
    """One catalog entry's outcome; `error` is set only when the fetch itself failed."""

    name: str
    url: str
    feedparser_entries: int
    feedparser_recent: int
    worker_entries: int
    worker_recent: int
    error: str | None = None

    @property
    def usable(self) -> bool:
        """Collectable: no fetch error, and both parsers found an article inside the window."""
        return self.error is None and self.feedparser_recent > 0 and self.worker_recent > 0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "url": self.url,
            "usable": self.usable,
            "feedparser_entries": self.feedparser_entries,
            "feedparser_recent": self.feedparser_recent,
            "worker_entries": self.worker_entries,
            "worker_recent": self.worker_recent,
            "error": self.error,
        }


def load_catalog(path: Path) -> list[SourceConfig]:
    """Read the catalog file and validate every entry as a `SourceConfig`."""
    data = json.loads(path.read_text())
    return [SourceConfig.model_validate(entry) for entry in data["feeds"]]


def _recent_count(dates: list[datetime], now: datetime) -> int:
    cutoff = now - timedelta(days=RECENT_WINDOW_DAYS)
    # A future date is a garbled or scheduled one, not proof of a recent article.
    return sum(1 for published in dates if cutoff <= published <= now)


def parse_with_feedparser(content: bytes) -> list[datetime]:
    """Dates feedparser can read, the same way `RssSource._entry_published` reads them."""
    # As a stream: bare bytes that name a path are opened as that local file.
    parsed = feedparser.parse(io.BytesIO(content))
    dates: list[datetime] = []
    for entry in parsed.entries:
        for key in ("published_parsed", "updated_parsed"):
            if key not in entry:
                continue
            value = entry[key]
            if value:
                dates.append(datetime(*value[:6], tzinfo=UTC))
            break
    return dates


def parse_with_worker(
    content: bytes, name: str, node_bin: str = "node", timeout: float = 30
) -> list[datetime]:
    """Dates the rss Worker's `parseFeed` can read, run through real Node."""
    result = subprocess.run(
        [node_bin, "--input-type=module", "-e", _WORKER_PARSE_SCRIPT, "--", name],
        input=content,
        cwd=RSS_WORKER_DIR,
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"parse.js failed: {result.stderr.decode(errors='replace')}")
    rows = json.loads(result.stdout)
    return [
        datetime.fromisoformat(row["published_at"].replace("Z", "+00:00"))
        for row in rows
        if row.get("published_at")
    ]


async def evaluate_feed(
    source: SourceConfig,
    fetch: Fetcher,
    now: datetime,
) -> FeedResult:
    """Fetch one feed once and run both parsers over the same bytes."""
    try:
        content = await fetch(source.url or "")
    except Exception as exc:  # noqa: BLE001 - reported per feed, not raised
        return FeedResult(source.name, source.url or "", 0, 0, 0, 0, error=str(exc))

    feedparser_dates = parse_with_feedparser(content)
    try:
        worker_dates = parse_with_worker(content, source.name)
    except Exception as exc:  # noqa: BLE001 - reported per feed, not raised
        return FeedResult(
            source.name,
            source.url or "",
            len(feedparser_dates),
            _recent_count(feedparser_dates, now),
            0,
            0,
            error=f"parse.js: {exc}",
        )

    return FeedResult(
        name=source.name,
        url=source.url or "",
        feedparser_entries=len(feedparser_dates),
        feedparser_recent=_recent_count(feedparser_dates, now),
        worker_entries=len(worker_dates),
        worker_recent=_recent_count(worker_dates, now),
    )


def http_fetcher(client: HttpClient) -> Fetcher:
    """The real fetcher: the project's shared `HttpClient`, same UA as a run's own fetch."""

    async def fetch(url: str) -> bytes:
        response = await client.get(url)
        response.raise_for_status()
        return response.content

    return fetch


async def validate_catalog(
    sources: list[SourceConfig], fetch: Fetcher, now: datetime | None = None
) -> list[FeedResult]:
    now = now or datetime.now(UTC)
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_FEEDS)

    async def one(source: SourceConfig) -> FeedResult:
        async with semaphore:
            return await evaluate_feed(source, fetch, now)

    return await asyncio.gather(*(one(s) for s in sources))


def _print_report(results: list[FeedResult]) -> None:
    width = max((len(r.name) for r in results), default=0)
    for r in results:
        status = "OK" if r.usable else "DROP"
        if r.error:
            print(f"{r.name.ljust(width)}  {status:<4}  {r.error}")
            continue
        print(
            f"{r.name.ljust(width)}  {status:<4}  "
            f"feedparser {r.feedparser_recent}/{r.feedparser_entries} recent  "
            f"worker {r.worker_recent}/{r.worker_entries} recent"
        )
    dropped = [r.name for r in results if not r.usable]
    print(f"\n{len(results) - len(dropped)}/{len(results)} usable.")
    if dropped:
        print("Drop: " + ", ".join(dropped))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--only", action="append", default=[], metavar="NAME")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args()

    sources = load_catalog(args.catalog)
    if args.only:
        known = {s.name for s in sources}
        if unknown := sorted(set(args.only) - known):
            parser.error(f"unknown feed: {', '.join(unknown)}")
        sources = [s for s in sources if s.name in args.only]

    async def run() -> list[FeedResult]:
        async with HttpClient() as client:
            return await validate_catalog(sources, http_fetcher(client))

    results = asyncio.run(run())

    if args.json:
        json.dump([r.to_dict() for r in results], sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        _print_report(results)

    raise SystemExit(0 if all(r.usable for r in results) else 1)


if __name__ == "__main__":
    main()
