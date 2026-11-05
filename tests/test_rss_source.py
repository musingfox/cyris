"""Tests for RssSource — the direct-polling feed adapter."""

import asyncio
import threading
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import feed_image_cases
import pytest
from captioned_post import CAPTIONED_IMAGE, FIRST_SENTENCE, POST_HTML

from cyris.adapters.fetch.rss_source import RssSource
from cyris.domain.models import Lane, SourceConfig

pytestmark = pytest.mark.unit

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
        sources={"F": SourceConfig(name="F", url="https://a.test/feed", lane=Lane.READ)},
    )

    assert len(articles) == 1
    assert articles[0].published_at.tzinfo is not None
    assert articles[0].source_lane == Lane.READ


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
async def test_every_windowed_entry_of_every_feed_is_returned():
    """The run cap is applied once, after the store, so this adapter cuts nothing."""
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
    )

    assert sorted(a.url for a in articles) == ["https://a.test/1", "https://b.test/1"]


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
async def test_every_feed_failing_is_a_failed_fetch_not_an_empty_one():
    http = AsyncMock()
    http.get.side_effect = [OSError("connection refused"), OSError("timed out")]

    with pytest.raises(RuntimeError, match="every feed failed: A, B"):
        await RssSource(http).fetch_articles(
            after=AFTER,
            before=BEFORE,
            sources={
                "A": SourceConfig(name="A", url="https://a.test/feed"),
                "B": SourceConfig(name="B", url="https://b.test/feed"),
            },
        )


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


@pytest.mark.asyncio
async def test_a_body_naming_a_local_file_is_parsed_as_text_not_opened(tmp_path):
    """feedparser opens bare bytes that name a path, so a feed server could make a run read it."""
    local = tmp_path / "local.xml"
    local.write_bytes(
        _feed(_item("From disk", "https://a.test/1", "Tue, 18 Mar 2026 10:00:00 GMT"))
    )
    source = RssSource(_http_returning(str(local).encode()))

    articles = await source.fetch_articles(
        after=AFTER,
        before=BEFORE,
        sources={"F": SourceConfig(name="F", url="https://a.test/feed", lane=Lane.READ)},
    )

    assert articles == []


@pytest.mark.asyncio
async def test_entities_inside_a_cdata_title_are_decoded():
    """CDATA keeps `&amp;` as text, so the page would show it after escaping once more."""
    item = _item(
        "<![CDATA[Highly &amp; Readwise: Ukraine&#8217;s]]>",
        "https://a.test/1",
        "Tue, 18 Mar 2026 10:00:00 GMT",
    )
    source = RssSource(_http_returning(_feed(item)))

    articles = await source.fetch_articles(
        after=AFTER,
        before=BEFORE,
        sources={"F": SourceConfig(name="F", url="https://a.test/feed")},
    )

    assert articles[0].title == "Highly & Readwise: Ukraine’s"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(feed_image_cases.CASES))
async def test_an_entry_takes_the_image_its_feed_names(name):
    """The same order and rules as the Worker's parser (`tests/feed_image_cases.py`)."""
    inner, wanted = feed_image_cases.CASES[name]
    source = RssSource(_http_returning(feed_image_cases.feed(inner).encode()))

    (article,) = await source.fetch_articles(
        after=AFTER,
        before=BEFORE,
        sources={"F": SourceConfig(name="F", url="https://d.test/feed")},
    )

    assert article.image_url == wanted


@pytest.mark.asyncio
async def test_markup_ahead_of_the_body_does_not_reach_article_content():
    """The same Substack-shaped body as the Worker path: prompts cut `content[:1000]`."""
    item = (
        "<item><title>T</title><link>https://a.test/1</link>"
        "<pubDate>Tue, 18 Mar 2026 10:00:00 GMT</pubDate>"
        f"<description><![CDATA[{POST_HTML}]]></description></item>"
    )
    source = RssSource(_http_returning(_feed(item)))

    (article,) = await source.fetch_articles(
        after=AFTER,
        before=BEFORE,
        sources={"F": SourceConfig(name="F", url="https://a.test/feed")},
    )

    assert len(CAPTIONED_IMAGE) > 4000
    assert article.content.startswith(FIRST_SENTENCE)
    assert "<" not in article.content
    assert "srcset" not in article.content


@pytest.mark.asyncio
async def test_the_image_is_read_from_the_html_before_it_becomes_text():
    item = (
        "<item><title>T</title><link>https://a.test/1</link>"
        "<pubDate>Tue, 18 Mar 2026 10:00:00 GMT</pubDate>"
        f"<description><![CDATA[{POST_HTML}]]></description></item>"
    )
    source = RssSource(_http_returning(_feed(item)))

    (article,) = await source.fetch_articles(
        after=AFTER,
        before=BEFORE,
        sources={"F": SourceConfig(name="F", url="https://a.test/feed")},
    )

    assert article.image_url == "https://img.test/w_1456.png"


@pytest.mark.asyncio
async def test_the_conversion_runs_in_the_thread_that_parses_the_feed(monkeypatch):
    """A feed's entries are converted off the event loop."""
    loop_thread = threading.get_ident()
    converted_on: list[int] = []
    to_thread_calls: list[object] = []
    real_to_thread = asyncio.to_thread

    def recording_plain_text(markup: str) -> str:
        converted_on.append(threading.get_ident())
        return markup

    async def recording_to_thread(func, /, *args, **kwargs):
        to_thread_calls.append(func)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr("cyris.adapters.fetch.rss_source.plain_text", recording_plain_text)
    monkeypatch.setattr(asyncio, "to_thread", recording_to_thread)
    items = "".join(
        f"<item><title>T{n}</title><link>https://a.test/{n}</link>"
        "<pubDate>Tue, 18 Mar 2026 10:00:00 GMT</pubDate>"
        "<description>body</description></item>"
        for n in range(3)
    )
    source = RssSource(_http_returning(_feed(items)))

    articles = await source.fetch_articles(
        after=AFTER,
        before=BEFORE,
        sources={"F": SourceConfig(name="F", url="https://a.test/feed")},
    )

    assert len(articles) == 3
    assert len(converted_on) == 3
    assert loop_thread not in converted_on
    assert len(to_thread_calls) == 1
