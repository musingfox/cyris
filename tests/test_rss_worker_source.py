"""Tests for CloudflareRssSource — reads the RSS Worker's D1 buffer."""

from datetime import UTC, datetime

import httpx
import pytest
import respx

from cyris.adapters.fetch.rss_worker_source import CloudflareRssSource
from cyris.domain.models import SourceConfig, Tier

WORKER = "https://cyris-rss.test"
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


@respx.mock
@pytest.mark.asyncio
async def test_window_is_passed_to_the_worker():
    route = respx.get(f"{WORKER}/articles").mock(return_value=httpx.Response(200, json=[]))

    await CloudflareRssSource(WORKER, "tok").fetch_articles(
        after=AFTER, before=BEFORE, sources={}, limit=42
    )

    request = route.calls.last.request
    assert request.url.params["after"] == AFTER.isoformat()
    assert request.url.params["before"] == BEFORE.isoformat()
    assert request.url.params["limit"] == "42"
    assert request.headers["Authorization"] == "Bearer tok"


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
async def test_worker_failure_degrades_to_empty():
    """A dead buffer must not sink the digest — other sources still run."""
    respx.get(f"{WORKER}/articles").mock(return_value=httpx.Response(500))

    articles = await CloudflareRssSource(WORKER, "tok").fetch_articles(
        after=AFTER, before=BEFORE, sources={}
    )

    assert articles == []


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
