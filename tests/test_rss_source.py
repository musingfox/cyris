"""Tests for RssSource — the direct-polling feed adapter."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from cyris.adapters.fetch.rss_source import RssSource
from cyris.domain.models import SourceConfig, Tier

AFTER = datetime(2026, 3, 17, tzinfo=UTC)
BEFORE = datetime(2026, 3, 19, tzinfo=UTC)


def _feed(*entries: str) -> bytes:
    items = "".join(entries)
    return f"""<?xml version="1.0"?>
    <rss version="2.0"><channel><title>Feed</title>{items}</channel></rss>
    """.encode()


def _item(title: str, link: str, date: str) -> str:
    return f"<item><title>{title}</title><link>{link}</link><pubDate>{date}</pubDate></item>"


def _http_returning(*bodies: bytes) -> AsyncMock:
    """A fake HttpClient handing each successive GET the next body."""
    http = AsyncMock()
    responses = []
    for body in bodies:
        response = AsyncMock()
        response.content = body
        response.raise_for_status = lambda: None
        responses.append(response)
    http.get.side_effect = responses
    return http


@pytest.mark.asyncio
async def test_published_at_is_always_tz_aware():
    """Naive datetimes would raise TypeError against the tz-aware digest window."""
    http = _http_returning(_feed(_item("A", "https://a.test/1", "Tue, 18 Mar 2026 10:00:00 GMT")))
    source = RssSource(http)

    articles = await source.fetch_articles(
        after=AFTER,
        before=BEFORE,
        sources={"F": SourceConfig(name="F", url="https://a.test/feed", tier=Tier.SUMMARIZE)},
    )

    assert len(articles) == 1
    assert articles[0].published_at.tzinfo is not None
    assert articles[0].source_tier == Tier.SUMMARIZE


@pytest.mark.asyncio
async def test_entries_outside_the_window_are_dropped():
    """Window filtering is this adapter's job — nothing upstream does it."""
    http = _http_returning(
        _feed(
            _item("old", "https://a.test/old", "Mon, 10 Mar 2026 10:00:00 GMT"),
            _item("in", "https://a.test/in", "Tue, 18 Mar 2026 10:00:00 GMT"),
            _item("undated", "https://a.test/undated", "not a date"),
        )
    )

    articles = await RssSource(http).fetch_articles(
        after=AFTER,
        before=BEFORE,
        sources={"F": SourceConfig(name="F", url="https://a.test/feed")},
    )

    assert [a.url for a in articles] == ["https://a.test/in"]


@pytest.mark.asyncio
async def test_limit_applies_across_feeds_not_per_feed():
    """Per-feed limiting would let 51 feeds x limit articles reach the LLM."""
    http = _http_returning(
        _feed(_item("a1", "https://a.test/1", "Tue, 18 Mar 2026 10:00:00 GMT")),
        _feed(_item("b1", "https://b.test/1", "Tue, 18 Mar 2026 11:00:00 GMT")),
    )

    articles = await RssSource(http).fetch_articles(
        after=AFTER,
        before=BEFORE,
        sources={
            "A": SourceConfig(name="A", url="https://a.test/feed"),
            "B": SourceConfig(name="B", url="https://b.test/feed"),
        },
        limit=1,
    )

    assert len(articles) == 1
    assert articles[0].url == "https://b.test/1"  # newest wins


@pytest.mark.asyncio
async def test_one_broken_feed_does_not_sink_the_run():
    http = AsyncMock()
    ok = AsyncMock()
    ok.content = _feed(_item("a1", "https://a.test/1", "Tue, 18 Mar 2026 10:00:00 GMT"))
    ok.raise_for_status = lambda: None
    http.get.side_effect = [OSError("connection refused"), ok]

    articles = await RssSource(http).fetch_articles(
        after=AFTER,
        before=BEFORE,
        sources={
            "Broken": SourceConfig(name="Broken", url="https://broken.test/feed"),
            "A": SourceConfig(name="A", url="https://a.test/feed"),
        },
    )

    assert [a.url for a in articles] == ["https://a.test/1"]


@pytest.mark.asyncio
async def test_tracking_params_are_stripped():
    """Unstripped tracking params defeat the store's URL primary key."""
    http = _http_returning(
        _feed(
            _item(
                "A",
                "https://dq.yam.com/post/17015?utm_source=rss&utm_medium=rss",
                "Tue, 18 Mar 2026 10:00:00 GMT",
            )
        )
    )

    articles = await RssSource(http).fetch_articles(
        after=AFTER,
        before=BEFORE,
        sources={"F": SourceConfig(name="F", url="https://a.test/feed")},
    )

    assert articles[0].url == "https://dq.yam.com/post/17015"


@pytest.mark.asyncio
async def test_email_only_sources_are_ignored():
    """Newsletter sources arrive via the Email Worker and have no feed URL."""
    http = _http_returning()

    articles = await RssSource(http).fetch_articles(
        after=AFTER,
        before=BEFORE,
        sources={"曼報": SourceConfig(name="曼報", type="newsletter", email_match="from:m@x.test")},
    )

    assert articles == []
    http.get.assert_not_called()
