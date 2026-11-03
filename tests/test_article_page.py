"""Tests for `lengthen_articles`: a short feed article's own page, read for its body.

respx answers every page; no test reaches a real site.
"""

import asyncio
import gzip
import logging
import zlib
from datetime import UTC, datetime

import httpx
import pytest
import respx

from cyris.adapters.fetch import article_page
from cyris.adapters.fetch.article_page import PAGE_CONCURRENCY, lengthen_articles
from cyris.domain.models import Article, Lane, SourceConfig

pytestmark = pytest.mark.unit

WINDOW = 200
FEED = SourceConfig(name="Teaser Feed", url="https://pjwu.test/rss.xml")
LETTERS = SourceConfig(name="Letters", type="newsletter", email_match="letters@pjwu.test")
SOURCES = {s.name: s for s in (FEED, LETTERS)}
TEASER = "One sentence about standards."
BODY = " ".join(
    f"Paragraph {i} explains why standards set by others shape the work we choose to do."
    for i in range(12)
)


def _page(body: str = BODY) -> str:
    return (
        "<!doctype html><html><head><title>Others' standards</title></head><body>"
        "<nav><a href='/'>Home</a><a href='/writing/'>Writing</a></nav>"
        f"<article><h1>Others' standards</h1><p>{body}</p></article>"
        "<footer>Site footer</footer></body></html>"
    )


def _html(body: str = BODY, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, text=_page(body), headers={"Content-Type": "text/html; charset=utf-8"}
    )


def _article(url: str, content: str = TEASER, source: SourceConfig = FEED) -> Article:
    return Article(
        id=url,
        title="Others' standards",
        url=url,
        content=content,
        published_at=datetime(2026, 11, 3, tzinfo=UTC),
        source_name=source.name,
        source_lane=Lane.READ,
    )


async def _lengthen(articles: list[Article]) -> list[Article]:
    return await lengthen_articles(articles, summarize_snippet_length=WINDOW, sources=SOURCES)


async def test_a_teaser_runs_on_its_pages_longer_body() -> None:
    url = "https://pjwu.test/writing/others-standards/"
    async with respx.mock:
        page = respx.get(url).mock(return_value=_html())
        (out,) = await _lengthen([_article(url)])

    assert page.call_count == 1
    assert "Paragraph 11 explains why standards" in out.content
    assert len(out.content) > len(TEASER)
    assert "Site footer" not in out.content
    assert (out.url, out.title, out.id) == (url, "Others' standards", url)


async def test_the_request_names_cyris_and_impersonates_no_browser() -> None:
    url = "https://pjwu.test/writing/others-standards/"
    async with respx.mock:
        page = respx.get(url).mock(return_value=_html())
        await _lengthen([_article(url)])

    assert page.call_count == 1
    agent = page.calls.last.request.headers["user-agent"]
    assert "cyris" in agent
    assert "Mozilla" not in agent
    assert "@" not in agent


async def test_the_request_asks_for_the_page_uncompressed() -> None:
    url = "https://pjwu.test/writing/others-standards/"
    async with respx.mock:
        page = respx.get(url).mock(return_value=_html())
        await _lengthen([_article(url)])

    assert page.call_count == 1
    assert page.calls.last.request.headers["accept-encoding"] == "identity"


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
async def test_a_page_sent_encoded_anyway_is_refused_undecoded(
    encoding: str, caplog: pytest.LogCaptureFixture
) -> None:
    url = "https://pjwu.test/writing/others-standards/"
    packed = (
        gzip.compress(_page().encode()) if encoding == "gzip" else zlib.compress(_page().encode())
    )
    async with respx.mock:
        page = respx.get(url).mock(
            return_value=httpx.Response(
                200,
                content=packed,
                headers={"Content-Type": "text/html", "Content-Encoding": encoding},
            )
        )
        with caplog.at_level(logging.INFO, logger=article_page.__name__):
            (out,) = await _lengthen([_article(url)])

    assert page.call_count == 1
    assert out.content == TEASER
    # Refused for its encoding, not for what it inflated to.
    assert f"encoded ({encoding})" in caplog.text


async def test_the_cap_counts_the_bytes_as_they_arrive(monkeypatch) -> None:
    monkeypatch.setattr(article_page, "MAX_PAGE_BYTES", len(_page().encode()))
    url = "https://pjwu.test/writing/exactly-the-cap/"
    async with respx.mock:
        page = respx.get(url).mock(return_value=_html())
        (out,) = await _lengthen([_article(url)])

    assert page.call_count == 1
    assert "Paragraph 11" in out.content


async def test_a_redirected_page_is_followed_to_its_body() -> None:
    url = "https://pinchlime.test/writing/others-standards/"
    moved = "https://pjwu.test/writing/others-standards/"
    async with respx.mock:
        old = respx.get(url).mock(return_value=httpx.Response(301, headers={"Location": moved}))
        new = respx.get(moved).mock(return_value=_html())
        (out,) = await _lengthen([_article(url)])

    assert (old.call_count, new.call_count) == (1, 1)
    assert "Paragraph 11" in out.content
    assert out.url == url


@pytest.mark.parametrize("extra", [0, 1, 500], ids=["exactly-the-window", "one-over", "long"])
async def test_a_feed_text_as_long_as_the_window_sends_no_request(extra: int) -> None:
    content = "x" * (WINDOW + extra)
    async with respx.mock:
        anything = respx.route().mock(return_value=_html())
        (out,) = await _lengthen([_article("https://pjwu.test/long", content)])

    assert not anything.called
    assert out.content == content


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(404, text="gone"),
        httpx.Response(403, text="<html>blocked</html>", headers={"Content-Type": "text/html"}),
        httpx.Response(500, text="oops"),
        httpx.ReadTimeout("slow"),
        httpx.ConnectError("refused"),
        httpx.Response(200, content=b"%PDF-1.7", headers={"Content-Type": "application/pdf"}),
        httpx.Response(200, text="<p>Tiny.</p>", headers={"Content-Type": "text/html"}),
        httpx.Response(200, text="", headers={"Content-Type": "text/html"}),
    ],
    ids=["404", "403", "500", "timeout", "refused", "pdf", "shorter-body", "empty"],
)
async def test_a_page_that_fails_or_says_less_keeps_the_feed_text(answer) -> None:
    url = "https://pjwu.test/writing/others-standards/"
    async with respx.mock:
        page = respx.get(url).mock(side_effect=[answer])
        (out,) = await _lengthen([_article(url)])

    assert page.call_count == 1
    assert out.content == TEASER


async def test_a_page_slower_than_the_timeout_keeps_the_feed_text(monkeypatch) -> None:
    monkeypatch.setattr(article_page, "PAGE_TIMEOUT_SECONDS", 0.05)
    url = "https://pjwu.test/writing/slow/"
    asked = []

    async def stall(request):
        asked.append(request.url)
        await asyncio.sleep(5)
        return _html()

    async with respx.mock:
        page = respx.get(url).mock(side_effect=stall)
        (out,) = await asyncio.wait_for(_lengthen([_article(url)]), timeout=2)

    assert asked == [url]
    # respx counts a call when its answer returns, and this one never does.
    assert page.call_count == 0
    assert out.content == TEASER


async def test_a_page_larger_than_the_cap_keeps_the_feed_text(monkeypatch) -> None:
    monkeypatch.setattr(article_page, "MAX_PAGE_BYTES", 1024)
    url = "https://pjwu.test/writing/huge/"
    async with respx.mock:
        page = respx.get(url).mock(return_value=_html())
        (out,) = await _lengthen([_article(url)])

    assert page.call_count == 1
    assert out.content == TEASER


@pytest.mark.parametrize(
    "url",
    [
        "newsletter:3f2a9c",
        "ftp://pjwu.test/writing/others-standards.txt",
        "mailto:editor@pjwu.test",
        "file:///etc/passwd",
        "https://",
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8080/admin",
        "http://[::1]/",
    ],
)
async def test_an_address_that_is_not_a_public_web_page_is_never_fetched(url: str) -> None:
    async with respx.mock:
        anything = respx.route().mock(return_value=_html())
        (out,) = await _lengthen([_article(url)])

    assert not anything.called
    assert out.content == TEASER


async def test_a_redirect_to_a_private_address_is_not_followed() -> None:
    url = "https://pjwu.test/writing/moved/"
    async with respx.mock:
        hop = respx.get(url).mock(
            return_value=httpx.Response(302, headers={"Location": "http://10.0.0.7/internal"})
        )
        inside = respx.get("http://10.0.0.7/internal").mock(return_value=_html())
        (out,) = await _lengthen([_article(url)])

    assert hop.call_count == 1
    assert not inside.called
    assert out.content == TEASER


# Loopback written the ways a resolver reads it, beyond the dotted quad: socket.inet_aton
# takes short, integer, hex and octal IPv4, and RFC 6761 reserves localhost and its names.
LOCAL_HOSTS = [
    "127.1",
    "2130706433",
    "0x7f000001",
    "0177.0.0.1",
    "localhost",
    "localhost.",
    "LOCALHOST",
    "app.localhost",
]


@pytest.mark.parametrize("host", LOCAL_HOSTS)
async def test_a_local_host_in_any_spelling_is_never_fetched(host: str) -> None:
    async with respx.mock:
        anything = respx.route().mock(return_value=_html())
        (out,) = await _lengthen([_article(f"http://{host}/admin")])

    assert not anything.called
    assert out.content == TEASER


@pytest.mark.parametrize("host", LOCAL_HOSTS)
async def test_a_redirect_to_a_local_host_in_any_spelling_is_not_followed(host: str) -> None:
    url = "https://pjwu.test/writing/moved/"
    async with respx.mock:
        hop = respx.get(url).mock(
            return_value=httpx.Response(302, headers={"Location": f"http://{host}/admin"})
        )
        inside = respx.route().mock(return_value=_html())
        (out,) = await _lengthen([_article(url)])

    assert hop.call_count == 1
    assert not inside.called
    assert out.content == TEASER


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=Nbwv5wHQoj0",
        "https://www.youtube.com/shorts/Nbwv5wHQoj0",
        "https://youtu.be/Nbwv5wHQoj0",
    ],
    ids=["watch", "shorts", "short-link"],
)
async def test_a_video_whose_description_is_short_sends_no_request(url: str) -> None:
    # A video page's extracted text is its footer navigation, which beats a short
    # description on length.
    description = "A short description of the video."
    async with respx.mock:
        anything = respx.route().mock(return_value=_html())
        (out,) = await _lengthen([_article(url, description)])

    assert not anything.called
    assert out.content == description


@pytest.mark.parametrize(
    "source", [LETTERS, SourceConfig(name="Gone")], ids=["newsletter", "unlisted"]
)
async def test_only_an_rss_sources_article_is_fetched(source: SourceConfig) -> None:
    url = "https://pjwu.test/p/issue-12"
    async with respx.mock:
        anything = respx.route().mock(return_value=_html())
        (out,) = await _lengthen([_article(url, source=source)])

    assert not anything.called
    assert out.content == TEASER


async def test_pages_are_fetched_at_most_the_concurrency_at_once() -> None:
    in_flight = 0
    peak = 0

    async def answer(request):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return _html()

    urls = [f"https://pjwu.test/writing/{i}/" for i in range(PAGE_CONCURRENCY * 3)]
    async with respx.mock:
        pages = respx.get(url__regex=r"https://pjwu\.test/writing/\d+/").mock(side_effect=answer)
        out = await _lengthen([_article(u) for u in urls])

    assert pages.call_count == len(urls)
    assert peak == PAGE_CONCURRENCY
    assert [a.url for a in out] == urls
    assert all("Paragraph 11" in a.content for a in out)


async def test_one_failed_page_leaves_the_others_lengthened_in_order() -> None:
    ok, down, long = (f"https://pjwu.test/writing/{name}/" for name in ("ok", "down", "long"))
    articles = [_article(ok), _article(down), _article(long, "y" * WINDOW)]
    async with respx.mock:
        good = respx.get(ok).mock(return_value=_html())
        bad = respx.get(down).mock(side_effect=httpx.ConnectError("refused"))
        skipped = respx.get(long).mock(return_value=_html())
        out = await _lengthen(articles)

    assert (good.call_count, bad.call_count) == (1, 1)
    assert not skipped.called
    assert [a.url for a in out] == [ok, down, long]
    assert "Paragraph 11" in out[0].content
    assert out[1].content == TEASER
    assert out[2].content == "y" * WINDOW


@pytest.mark.parametrize("charset", ["x-bogus", "none"])
async def test_a_page_with_an_unknown_charset_costs_the_others_nothing(charset: str) -> None:
    ok, odd = "https://pjwu.test/writing/ok/", "https://pjwu.test/writing/odd/"
    async with respx.mock:
        good = respx.get(ok).mock(return_value=_html())
        unknown = respx.get(odd).mock(
            return_value=httpx.Response(
                200,
                content=_page().encode(),
                headers={"Content-Type": f"text/html; charset={charset}"},
            )
        )
        out = await _lengthen([_article(ok), _article(odd)])

    assert (good.call_count, unknown.call_count) == (1, 1)
    assert "Paragraph 11" in out[0].content
    # Read as utf-8, the page's own body is still there to use.
    assert "Paragraph 11" in out[1].content


async def test_an_extractor_that_raises_costs_the_others_nothing(monkeypatch) -> None:
    ok, odd = "https://pjwu.test/writing/ok/", "https://pjwu.test/writing/odd/"
    real = article_page.page_text

    def page_text(markup):
        if "Odd one out" in markup:
            raise RuntimeError("lxml gave up")
        return real(markup)

    monkeypatch.setattr(article_page, "page_text", page_text)
    async with respx.mock:
        good = respx.get(ok).mock(return_value=_html())
        broken = respx.get(odd).mock(return_value=_html(f"Odd one out. {BODY}"))
        out = await _lengthen([_article(odd), _article(ok)])

    assert (good.call_count, broken.call_count) == (1, 1)
    assert out[0].content == TEASER
    assert "Paragraph 11" in out[1].content


async def test_the_articles_handed_in_are_not_changed() -> None:
    url = "https://pjwu.test/writing/others-standards/"
    article = _article(url)
    async with respx.mock:
        page = respx.get(url).mock(return_value=_html())
        await _lengthen([article])

    assert page.call_count == 1
    assert article.content == TEASER
