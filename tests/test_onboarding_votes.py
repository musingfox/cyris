"""The onboarding votes step's two routes, over the real D1 stores on SQLite.

`POST /api/onboarding/cards` deals cards from the reader's feeds and writes
nothing; `POST /api/onboarding/votes` stores each marked article and then votes
on it, as `/labels` does, so a vote is a human verdict with `triaged_at`. A card
the reader left alone is never sent, so it has no row at all. The wizard's own
vote-similarity body is stored here through the real route too.

The handlers are called directly with the body the page sends: what a browser
does with them is `scripts/onboarding_probe.py`'s.
"""

import json
from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest
from fakes import SqliteD1

from cyris.adapters.fetch.onboarding_cards import CARDS_PER_SOURCE, OnboardingArticles
from cyris.adapters.store.d1_store import D1ArticleStore
from cyris.adapters.store.settings import D1Settings
from cyris.adapters.store.source_store import D1SourceStore
from cyris.diagnostics.doctor import Check
from cyris.domain.models import Article, ArticleState, Lane, SourceConfig
from cyris.entrypoints.triage_server import TriageServer

pytestmark = pytest.mark.integration

NOW = datetime.now(UTC)
SOURCES = [
    SourceConfig(name="Feed A", url="https://a.test/rss"),
    SourceConfig(name="Feed B", url="https://b.test/rss"),
    SourceConfig(name="Quiet", url="https://quiet.test/rss"),
]


def _articles(source: str, host: str, count: int = 8) -> list[Article]:
    return [
        Article(
            id=f"{host}-{n}",
            title=f"{source} story {n}",
            url=f"https://{host}.test/{n}",
            content=f"<p>Body of {n}</p>",
            published_at=NOW - timedelta(hours=12 * n + 1),
            source_name=source,
            source_lane=Lane.SKIM,
            source_tags=["news"],
        )
        for n in range(count)
    ]


FETCHED = _articles("Feed A", "a") + _articles("Feed B", "b")


class FakeSource:
    """The run's RSS edge, answering every read with the same feed content."""

    def __init__(self, articles: list[Article]) -> None:
        self.articles = articles
        self.reads = 0

    async def fetch_articles(self, after, before, sources) -> list[Article]:
        self.reads += 1
        return [a for a in self.articles if a.source_name in sources and after <= a.published_at]

    async def health_check(self) -> bool:
        return True


class Request:
    """What a handler reads of a request: its JSON body."""

    def __init__(self, body) -> None:
        self._body = body

    async def json(self):
        if self._body is ValueError:
            raise ValueError("not JSON")
        return self._body


class Deployment:
    def __init__(self, *, sources=SOURCES, articles=FETCHED) -> None:
        self.db = SqliteD1()
        self.source_store = D1SourceStore(self.db)
        for source in sources:
            self.source_store.upsert(source)
        self.store = D1ArticleStore(self.db)
        self.settings = D1Settings(self.db)
        self.polls: list[int] = []
        self.source = FakeSource(articles)

        async def poll(feeds: int) -> None:
            self.polls.append(feeds)

        self.server = TriageServer(
            settings=self.settings,
            source_store=self.source_store,
            article_store=self.store,
            onboarding_articles=OnboardingArticles(self.source, poll),
        )

    def rows(self) -> dict[str, dict]:
        return {
            row["url"]: dict(row)
            for row in self.db.query(
                "SELECT url, state, triaged_at, rejection_reason, source_name, title "
                "FROM stored_articles"
            ).rows
        }


async def _call(handler, body) -> tuple[int, dict]:
    response = await handler(Request(body))
    return response.status, json.loads(response.text)


async def _cards(deployment: Deployment) -> tuple[int, dict]:
    return await _call(deployment.server._handle_post_onboarding_cards, {})


async def _vote(deployment: Deployment, votes) -> tuple[int, dict]:
    return await _call(deployment.server._handle_post_onboarding_votes, {"votes": votes})


class TestTheCards:
    async def test_the_cards_are_drawn_from_every_feed_and_write_nothing(self) -> None:
        deployment = Deployment()

        status, body = await _cards(deployment)

        assert status == 200 and body["ok"]
        assert body["sources"] == 3
        assert len(body["cards"]) == 2 * CARDS_PER_SOURCE
        assert {card["source"] for card in body["cards"]} == {"Feed A", "Feed B"}
        assert set(body["cards"][0]) == {"url", "title", "source", "excerpt", "published_at"}
        assert body["cards"][0]["excerpt"] == "Body of 7"
        assert deployment.rows() == {}

    async def test_a_feed_with_no_article_is_named(self) -> None:
        deployment = Deployment()

        _, body = await _cards(deployment)

        assert body["empty"] == ["Quiet"]

    async def test_the_worker_is_polled_for_every_feed_first(self) -> None:
        deployment = Deployment()

        await _cards(deployment)

        assert deployment.polls == [3]

    async def test_a_table_with_no_feed_polls_nothing_and_deals_nothing(self) -> None:
        deployment = Deployment(sources=[])

        status, body = await _cards(deployment)

        assert (status, body["sources"], body["cards"], body["empty"]) == (200, 0, [], [])
        assert deployment.polls == [] and deployment.source.reads == 0

    async def test_a_json_deployment_has_no_cards(self) -> None:
        status, body = await _call(TriageServer()._handle_post_onboarding_cards, {})

        assert status == 409 and not body["ok"]


class TestTheVotes:
    async def test_each_voted_article_is_stored_with_its_verdict_and_stamp(self) -> None:
        deployment = Deployment()
        _, dealt = await _cards(deployment)
        up, down, untouched = (card["url"] for card in dealt["cards"][:3])

        status, body = await _vote(
            deployment, [{"url": up, "vote": "up"}, {"url": down, "vote": "down"}]
        )

        assert status == 200, body
        assert (body["accepted"], body["rejected"]) == ([up], [down])
        rows = deployment.rows()
        assert set(rows) == {up, down}
        assert rows[up]["state"] == ArticleState.ACCEPTED
        assert rows[down]["state"] == ArticleState.REJECTED
        assert rows[down]["rejection_reason"] is not None
        assert all(row["triaged_at"] for row in rows.values())
        # The card nobody pressed: no row, so no stamp, and the first run still reads it.
        assert untouched not in rows

    async def test_an_article_is_stored_before_it_is_voted_on(self) -> None:
        """`accept` updates existing rows only: a vote first would land on nothing."""
        deployment = Deployment()
        url = FETCHED[-1].url

        await _vote(deployment, [{"url": url, "vote": "up"}])

        row = deployment.rows()[url]
        assert (row["state"], row["source_name"], row["title"]) == (
            ArticleState.ACCEPTED,
            FETCHED[-1].source_name,
            FETCHED[-1].title,
        )

    async def test_a_vote_reads_the_feeds_again_and_polls_nothing(self) -> None:
        """The container may have slept since the cards were dealt; the read holds no state."""
        deployment = Deployment()

        await _vote(deployment, [{"url": FETCHED[-1].url, "vote": "down"}])

        assert deployment.source.reads == 1 and deployment.polls == []

    async def test_an_article_no_longer_in_the_feeds_drops_alone(self) -> None:
        """The cards are each feed's oldest: one aging out must not cost the other votes."""
        deployment = Deployment()
        kept, gone = FETCHED[0].url, "https://a.test/rolled-off"

        status, body = await _vote(
            deployment, [{"url": kept, "vote": "up"}, {"url": gone, "vote": "down"}]
        )

        assert status == 200, body
        assert (body["accepted"], body["rejected"], body["missing"]) == ([kept], [], [gone])
        assert set(deployment.rows()) == {kept}

    async def test_votes_whose_articles_all_left_the_feeds_write_nothing(self) -> None:
        deployment = Deployment()
        gone = "https://a.test/rolled-off"

        status, body = await _vote(deployment, [{"url": gone, "vote": "down"}])

        assert status == 409 and body["missing"] == [gone]
        assert deployment.rows() == {}

    @pytest.mark.parametrize(
        "body",
        [
            ValueError,
            {},
            {"votes": []},
            {"votes": [{"url": FETCHED[0].url, "vote": "skip"}]},
            {"votes": [{"url": 3, "vote": "up"}]},
            {
                "votes": [
                    {"url": FETCHED[0].url, "vote": "up"},
                    {"url": FETCHED[0].url, "vote": "down"},
                ]
            },
        ],
    )
    async def test_a_body_that_is_not_votes_writes_nothing(self, body) -> None:
        deployment = Deployment()

        response = await deployment.server._handle_post_onboarding_votes(Request(body))

        assert response.status == 400
        assert deployment.rows() == {}

    async def test_a_json_deployment_takes_no_vote(self) -> None:
        status, _ = await _call(
            TriageServer()._handle_post_onboarding_votes,
            {"votes": [{"url": FETCHED[0].url, "vote": "up"}]},
        )

        assert status == 409


# What GET /api/settings answers the page with on a Cloudflare deployment's
# shipped defaults: the Workers AI embedder, its own model, and vote similarity off.
STORED = {
    "vote_similarity.enabled": False,
    "vote_similarity.provider": "workers_ai",
    "vote_similarity.model": "",
    "vote_similarity.max_seeds": 200,
}


class TestVoteSimilarity:
    async def test_the_wizards_body_turns_it_on_with_the_stored_embedder(self) -> None:
        """saveVotes in onboarding.js sends the stored provider, model and seeds, enabled."""
        deployment = Deployment()
        deployment.settings.set(STORED)
        body = {
            "enabled": True,
            "provider": STORED["vote_similarity.provider"],
            "model": STORED["vote_similarity.model"],
            "max_seeds": STORED["vote_similarity.max_seeds"],
        }
        answered = Check(name="embedding probe", status="ok", detail="bge-m3 answered")

        with mock.patch(
            "cyris.entrypoints.triage_server.probe_embedder", mock.AsyncMock(return_value=answered)
        ) as probe:
            status, _ = await _call(deployment.server._handle_post_vote, body)

        assert status == 200
        probe.assert_awaited_once_with("workers_ai", "", "")
        assert deployment.settings.all() == {**STORED, "vote_similarity.enabled": True}
