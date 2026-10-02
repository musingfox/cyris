"""Tests for CloudflareRssSource — reads the RSS Worker's D1 buffer."""

import re
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from cyris.adapters.fetch.rss_worker_source import CloudflareRssSource
from cyris.domain.models import SourceConfig, Tier

pytestmark = pytest.mark.unit

WORKER = "https://cyris-rss.test"
WORKER_JS = Path(__file__).parent.parent / "workers" / "rss" / "src" / "index.js"
AFTER = datetime(2026, 3, 17, tzinfo=UTC)
BEFORE = datetime(2026, 3, 19, tzinfo=UTC)

ROW = {
    "url": "https://a.test/1",
    "guid": "tag:a.test,1",
    "title": "Hello",
    "content": "body",
    "author": "Ada",
    "published_at": "2026-03-18T10:00:00.000Z",
    "source_name": "A",
}


@respx.mock
@pytest.mark.asyncio
async def test_rows_map_to_articles_with_tier_from_config():
    """The buffer stores only the source name; tier and tags come from sources.yaml."""
    respx.get(f"{WORKER}/articles").mock(return_value=httpx.Response(200, json=[ROW]))

    articles = await CloudflareRssSource(WORKER, "tok").fetch_articles(
        after=AFTER,
        before=BEFORE,
        sources={"A": SourceConfig(name="A", url="https://a.test/feed", tier=Tier.SUMMARIZE)},
    )

    assert len(articles) == 1
    assert articles[0].url == "https://a.test/1"
    assert articles[0].source_tier == Tier.SUMMARIZE
    assert articles[0].published_at.tzinfo is not None


def _worker_row_ceiling() -> str:
    """The most rows `GET /articles` returns, read from the Worker's clamp."""
    (ceiling,) = re.findall(
        r'searchParams\.get\("limit"\)\) \|\| \d+, (\d+)\)', WORKER_JS.read_text()
    )
    return ceiling


@respx.mock
@pytest.mark.asyncio
async def test_window_is_passed_to_the_worker():
    route = respx.get(f"{WORKER}/articles").mock(return_value=httpx.Response(200, json=[]))

    await CloudflareRssSource(WORKER, "tok").fetch_articles(after=AFTER, before=BEFORE, sources={})

    request = route.calls.last.request
    assert request.url.params["after"] == AFTER.isoformat()
    assert request.url.params["before"] == BEFORE.isoformat()
    assert request.headers["Authorization"] == "Bearer tok"


@respx.mock
@pytest.mark.asyncio
async def test_the_read_asks_for_the_workers_ceiling_not_the_run_cap():
    """The run cap is applied after the store; a read cut at it loses rows unstored.

    The Worker's own default is lower than its ceiling, so the ceiling is asked for
    by name rather than left to the default.
    """
    route = respx.get(f"{WORKER}/articles").mock(return_value=httpx.Response(200, json=[]))

    await CloudflareRssSource(WORKER, "tok").fetch_articles(after=AFTER, before=BEFORE, sources={})

    assert route.calls.last.request.url.params["limit"] == _worker_row_ceiling()


@respx.mock
@pytest.mark.asyncio
async def test_unknown_source_falls_back_to_filter_tier():
    """A feed added to the Worker but not yet in sources.yaml must not crash the run."""
    respx.get(f"{WORKER}/articles").mock(return_value=httpx.Response(200, json=[ROW]))

    articles = await CloudflareRssSource(WORKER, "tok").fetch_articles(
        after=AFTER, before=BEFORE, sources={}
    )

    assert articles[0].source_tier == Tier.FILTER


@respx.mock
@pytest.mark.asyncio
async def test_a_failed_buffer_read_raises():
    """`fetch_all_articles` skips a raising source and lists it in `failed_sources`,
    which is what the failed-fetch alert reads; an empty list would hide the outage."""
    respx.get(f"{WORKER}/articles").mock(return_value=httpx.Response(500))

    with pytest.raises(httpx.HTTPStatusError):
        await CloudflareRssSource(WORKER, "tok").fetch_articles(
            after=AFTER, before=BEFORE, sources={}
        )


@respx.mock
@pytest.mark.asyncio
async def test_malformed_row_is_skipped_not_fatal():
    respx.get(f"{WORKER}/articles").mock(
        return_value=httpx.Response(200, json=[{"title": "no url"}, ROW])
    )

    articles = await CloudflareRssSource(WORKER, "tok").fetch_articles(
        after=AFTER, before=BEFORE, sources={}
    )

    assert [a.url for a in articles] == ["https://a.test/1"]


@respx.mock
@pytest.mark.asyncio
async def test_a_read_that_fills_the_ceiling_is_logged(monkeypatch, caplog):
    """A full read means older rows were left in the buffer, never stored."""
    monkeypatch.setattr("cyris.adapters.fetch.rss_worker_source.WORKER_ROW_CEILING", 2)
    second = {**ROW, "url": "https://a.test/2", "guid": "tag:a.test,2"}
    respx.get(f"{WORKER}/articles").mock(return_value=httpx.Response(200, json=[ROW, second]))

    with caplog.at_level("WARNING", logger="cyris.adapters.fetch.rss_worker_source"):
        await CloudflareRssSource(WORKER, "tok").fetch_articles(
            after=AFTER, before=BEFORE, sources={}
        )

    assert any("ceiling" in r.getMessage() for r in caplog.records), caplog.records


@respx.mock
@pytest.mark.asyncio
async def test_a_read_below_the_ceiling_logs_no_warning(caplog):
    respx.get(f"{WORKER}/articles").mock(return_value=httpx.Response(200, json=[ROW]))

    with caplog.at_level("WARNING", logger="cyris.adapters.fetch.rss_worker_source"):
        await CloudflareRssSource(WORKER, "tok").fetch_articles(
            after=AFTER, before=BEFORE, sources={}
        )

    assert not caplog.records
