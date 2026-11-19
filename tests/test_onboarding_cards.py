"""The onboarding vote cards: which articles are drawn, and how the feeds are read for them."""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from cyris.adapters.fetch import onboarding_cards
from cyris.adapters.fetch.onboarding_cards import (
    BUFFER_DAYS,
    CARDS_PER_SOURCE,
    CARDS_TOTAL,
    OnboardingArticles,
    draw_cards,
)
from cyris.adapters.fetch.rss_worker_source import (
    POLL_CONCURRENCY,
    POLL_FEED_TIMEOUT_SECONDS,
    POLL_WRITE_MARGIN_SECONDS,
    CloudflareRssSource,
    poll_timeout_seconds,
)
from cyris.domain.models import Article, Lane, SourceConfig

pytestmark = pytest.mark.unit

ROOT = Path(__file__).parent.parent
WORKER_JS = (ROOT / "workers" / "rss" / "src" / "buffer.js").read_text(encoding="utf-8")
ARCHITECTURE = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)
WORKER = "https://rss.test"


def _source(name: str, **kwargs) -> SourceConfig:
    return SourceConfig(name=name, url=f"https://{name.lower()}.test/rss", **kwargs)


def _article(source: str, days_old: float, n: int = 0) -> Article:
    return Article(
        id=f"{source}-{n}",
        title=f"{source} {n}",
        url=f"https://{source.lower()}.test/{n}",
        content="",
        published_at=NOW - timedelta(days=days_old),
        source_name=source,
        source_lane=Lane.SKIM,
    )


def _feed(source: str, count: int) -> list[Article]:
    """`count` articles, the n-th n days old: 0 is the newest."""
    return [_article(source, days_old=n, n=n) for n in range(count)]


SOURCES = {"A": _source("A"), "B": _source("B")}


class TestTheDraw:
    def test_each_source_gives_its_oldest_articles_up_to_the_fixed_count(self) -> None:
        draw = draw_cards(_feed("A", 10), {"A": SOURCES["A"]})

        assert [a.url for a in draw.cards] == [
            f"https://a.test/{n}" for n in range(9, 9 - CARDS_PER_SOURCE, -1)
        ]

    def test_the_newer_half_is_never_drawn(self) -> None:
        """The newest articles are the first issue's; a voted one leaves its candidates."""
        draw = draw_cards(_feed("A", 4), {"A": SOURCES["A"]})

        assert [a.url for a in draw.cards] == ["https://a.test/3", "https://a.test/2"]

    def test_a_source_with_one_article_gives_no_card_and_is_not_empty(self) -> None:
        draw = draw_cards(_feed("A", 1), {"A": SOURCES["A"]})

        assert (draw.cards, draw.empty) == ([], [])

    def test_the_cards_alternate_between_sources(self) -> None:
        draw = draw_cards(_feed("A", 10) + _feed("B", 10), SOURCES)

        assert [a.source_name for a in draw.cards][:4] == ["A", "B", "A", "B"]
        assert len(draw.cards) == 2 * CARDS_PER_SOURCE

    def test_a_full_pick_is_cut_at_the_total_with_every_source_dealt_once(self) -> None:
        names = [f"S{n}" for n in range(CARDS_TOTAL)]
        sources = {name: _source(name) for name in names}

        draw = draw_cards([a for name in names for a in _feed(name, 10)], sources)

        assert [a.source_name for a in draw.cards] == names

    def test_a_source_the_read_found_nothing_for_is_named_in_the_sources_order(self) -> None:
        sources = {"C": _source("C"), **SOURCES}

        draw = draw_cards(_feed("B", 6), sources)

        assert draw.empty == ["C", "A"]
        assert {a.source_name for a in draw.cards} == {"B"}

    def test_an_article_of_a_source_not_in_the_table_is_never_drawn(self) -> None:
        """The buffer still holds a retired source's rows; the reader did not pick it."""
        draw = draw_cards(_feed("Gone", 6), SOURCES)

        assert draw.cards == [] and draw.empty == ["A", "B"]

    def test_a_newsletter_source_is_neither_drawn_nor_named_empty(self) -> None:
        letters = {"L": SourceConfig(name="L", type="newsletter", email_match="l@x.test")}

        assert draw_cards([], letters) == onboarding_cards.CardDraw(cards=[], empty=[])

    def test_one_url_is_one_card(self) -> None:
        twice = _feed("A", 6) + _feed("A", 6)

        draw = draw_cards(twice, {"A": SOURCES["A"]})

        assert len({a.url for a in draw.cards}) == len(draw.cards) == CARDS_PER_SOURCE


class FakeSource:
    """A `FetchSource` that records each window it was asked for."""

    def __init__(self, articles: list[Article]) -> None:
        self.articles = articles
        self.windows: list[tuple[datetime, datetime, set[str]]] = []

    async def fetch_articles(self, after, before, sources) -> list[Article]:
        self.windows.append((after, before, set(sources)))
        return self.articles

    async def health_check(self) -> bool:
        return True


class TestTheRead:
    async def test_it_reads_the_whole_buffer_up_to_now(self) -> None:
        source = FakeSource(_feed("A", 3))

        await OnboardingArticles(source).read(SOURCES, NOW)

        assert source.windows == [(NOW - timedelta(days=BUFFER_DAYS), NOW, {"A", "B"})]

    async def test_with_no_feed_it_asks_nothing(self) -> None:
        source = FakeSource([])
        polls: list[int] = []

        async def poll(feeds: int) -> None:
            polls.append(feeds)

        read = await OnboardingArticles(source, poll).refresh({}, lambda: NOW)

        assert (read.articles, source.windows, polls) == ([], [], [])

    async def test_it_polls_every_feed_before_it_reads(self) -> None:
        events: list[str] = []
        source = FakeSource(_feed("A", 2))

        async def poll(feeds: int) -> None:
            events.append(f"poll {feeds}")

        def now() -> datetime:
            events.append("now")
            return NOW

        read = await OnboardingArticles(source, poll).refresh(SOURCES, now)

        assert events == ["poll 2", "now"]
        assert read.poll_error is None and len(read.articles) == 2

    async def test_a_failed_poll_still_reads_and_says_why(self) -> None:
        source = FakeSource(_feed("A", 2))

        async def poll(feeds: int) -> None:
            raise httpx.ReadTimeout("timed out")

        read = await OnboardingArticles(source, poll).refresh(SOURCES, lambda: NOW)

        assert read.poll_error == "timed out"
        assert len(read.articles) == 2

    async def test_a_failed_read_raises(self) -> None:
        class Broken(FakeSource):
            async def fetch_articles(self, after, before, sources):
                raise httpx.ConnectError("down")

        with pytest.raises(httpx.ConnectError):
            await OnboardingArticles(Broken([])).read(SOURCES, NOW)


class TestTheWorkerPoll:
    @respx.mock
    async def test_it_posts_poll_with_the_bearer(self) -> None:
        route = respx.post(f"{WORKER}/poll").mock(
            return_value=httpx.Response(200, json={"feeds": 2})
        )

        await CloudflareRssSource(WORKER, "tok").poll(2)

        assert route.call_count == 1
        assert route.calls[0].request.headers["Authorization"] == "Bearer tok"

    @respx.mock
    async def test_a_failed_poll_raises(self) -> None:
        route = respx.post(f"{WORKER}/poll").mock(return_value=httpx.Response(500))

        with pytest.raises(httpx.HTTPStatusError):
            await CloudflareRssSource(WORKER, "tok").poll(2)

        assert route.call_count == 1

    def test_twelve_feeds_wait_three_batches_and_the_writes(self) -> None:
        assert poll_timeout_seconds(12) == 3 * POLL_FEED_TIMEOUT_SECONDS + POLL_WRITE_MARGIN_SECONDS
        assert poll_timeout_seconds(13) == 4 * POLL_FEED_TIMEOUT_SECONDS + POLL_WRITE_MARGIN_SECONDS


class TestTheConstantsMirrorTheWorker:
    def test_the_poll_shape_is_the_workers(self) -> None:
        concurrency = re.search(r"const CONCURRENCY = (\d+);", WORKER_JS)
        timeout = re.search(r"const FETCH_TIMEOUT_MS = (\d+);", WORKER_JS)
        assert concurrency and timeout

        assert int(concurrency[1]) == POLL_CONCURRENCY
        assert int(timeout[1]) == POLL_FEED_TIMEOUT_SECONDS * 1000

    def test_the_cards_reach_as_far_back_as_the_buffer_keeps(self) -> None:
        retention = re.search(r"const RETENTION_DAYS = (\d+);", WORKER_JS)
        assert retention

        assert int(retention[1]) == BUFFER_DAYS

    def test_the_card_count_is_the_one_architecture_grades(self) -> None:
        assert f"`CARDS_PER_SOURCE` = {CARDS_PER_SOURCE}" in ARCHITECTURE
        assert f"`CARDS_TOTAL` = {CARDS_TOTAL}" in ARCHITECTURE
