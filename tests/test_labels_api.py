"""/labels and /api/labels: one sampled item at a time, blind, answered as a vote."""

import json
from datetime import UTC, datetime

import pytest
from aiohttp.test_utils import TestClient, TestServer
from fakes import SqliteD1

from cyris.adapters import promotions
from cyris.adapters.promotions import PromotedArticle, sync_promotions
from cyris.adapters.store.blind_labels import D1BlindLabels, SampleRow
from cyris.adapters.store.d1_store import D1ArticleStore
from cyris.domain.models import ArticleState
from cyris.entrypoints.triage_server import TriageServer

pytestmark = pytest.mark.integration

SEEN = "2026-09-19T00:00:00.000000+00:00"
DRAWN = datetime(2026, 10, 8, tzinfo=UTC)
# Everything the pipeline decided about the first item, none of which may reach the page.
VERDICT_WORDS = ("rejected", "filtered", "0.37", "news", "filter", "pending", "accepted")


def put(db: SqliteD1, url: str, title: str, content: str = "") -> None:
    db.query(
        "INSERT INTO stored_articles (url, original_id, title, content, published_at, "
        "source_name, source_tier, source_tags, state, first_seen_at, rejection_reason, score) "
        "VALUES (?, ?, ?, ?, ?, 'Wire', 'filter', '[\"news\"]', 'rejected', ?, 'filtered', 0.37)",
        [url, url, title, content, SEEN, SEEN],
    )


def deployment(*urls: str) -> SqliteD1:
    db = SqliteD1()
    for i, url in enumerate(urls):
        put(db, url, f"Title {i}", f"<p>Body &amp; {i}</p>")
    D1BlindLabels(db).replace_sample(
        [
            SampleRow(
                url=url,
                position=i,
                news=True,
                pipeline_state=ArticleState.REJECTED,
                stratum_size=9,
            )
            for i, url in enumerate(urls)
        ],
        seed=1,
        since=DRAWN,
        drawn_at=DRAWN,
    )
    return db


@pytest.fixture
async def serve():
    clients: list[TestClient] = []

    async def start(db: SqliteD1 | None, *, store=None) -> TestClient:
        server = TriageServer(
            blind_labels=D1BlindLabels(db) if db else None,
            article_store=store or (D1ArticleStore(db) if db else None),
        )
        client = TestClient(TestServer(server._app))
        await client.start_server()
        clients.append(client)
        return client

    yield start
    for client in clients:
        await client.close()


def article(db: SqliteD1, url: str) -> dict:
    return db.query("SELECT * FROM stored_articles WHERE url = ?", [url]).rows[0]


def label_of(db: SqliteD1, url: str) -> str | None:
    return db.query("SELECT label FROM blind_labels WHERE url = ?", [url]).rows[0]["label"]


class TestRead:
    async def test_the_page_is_served(self, serve) -> None:
        client = await serve(None)

        response = await client.get("/labels")

        assert response.status == 200
        assert response.content_type == "text/html"

    async def test_the_first_item_is_its_title_source_excerpt_and_link(self, serve) -> None:
        client = await serve(deployment("https://a.test/1", "https://a.test/2"))

        body = await (await client.get("/api/labels")).json()

        assert body == {
            "answered": 0,
            "total": 2,
            "item": {
                "url": "https://a.test/1",
                "title": "Title 0",
                "source": "Wire",
                "excerpt": "Body & 0",
            },
        }

    async def test_nothing_the_pipeline_decided_reaches_the_answer(self, serve) -> None:
        client = await serve(deployment("https://a.test/1"))

        text = await (await client.get("/api/labels")).text()

        assert [word for word in VERDICT_WORDS if word in text] == []

    async def test_with_every_item_answered_there_is_no_item(self, serve) -> None:
        db = deployment("https://a.test/1")
        D1BlindLabels(db).record("https://a.test/1", "skip", DRAWN)
        client = await serve(db)

        assert await (await client.get("/api/labels")).json() == {
            "answered": 1,
            "total": 1,
            "item": None,
        }

    async def test_a_deployment_without_d1_has_no_sample_to_read(self, serve) -> None:
        client = await serve(None)

        response = await client.get("/api/labels")

        assert response.status == 409
        assert "D1" in (await response.json())["error"]


async def answer(client: TestClient, url: str, label: str):
    return await client.post("/api/labels", json={"url": url, "label": label})


class TestAnswer:
    @pytest.mark.parametrize("label", ["up", "down"])
    async def test_a_label_is_written_exactly_as_a_digest_vote(
        self, serve, monkeypatch, label: str
    ) -> None:
        url = "https://a.test/1"
        labeled = deployment(url)
        voted = deployment(url)
        edge: list[str] = []

        def pull(*_):
            edge.append("pull")
            return [PromotedArticle(url=url, vote=label)]

        monkeypatch.setattr(promotions, "pull_promotions", pull)
        monkeypatch.setattr(promotions, "ack_promotions", lambda *_: edge.append("ack"))
        sync_promotions("https://promote.test", "t", D1ArticleStore(voted))
        assert edge == ["pull", "ack"]
        client = await serve(labeled)

        response = await answer(client, url, label)

        assert response.status == 200
        by_label, by_vote = article(labeled, url), article(voted, url)
        assert by_label["triaged_at"] and by_vote["triaged_at"]
        del by_label["triaged_at"], by_vote["triaged_at"]
        assert by_label == by_vote
        assert by_label["state"] == ("accepted" if label == "up" else "rejected")
        assert label_of(labeled, url) == label

    async def test_an_answer_returns_the_next_item(self, serve) -> None:
        client = await serve(deployment("https://a.test/1", "https://a.test/2"))

        body = await (await answer(client, "https://a.test/1", "up")).json()

        assert body["ok"] is True
        assert (body["answered"], body["total"]) == (1, 2)
        assert body["item"]["url"] == "https://a.test/2"

    async def test_a_skip_records_nothing_to_the_article(self, serve) -> None:
        url = "https://a.test/1"
        db = deployment(url)
        before = article(db, url)
        client = await serve(db)

        response = await answer(client, url, "skip")

        assert response.status == 200
        assert article(db, url) == before
        assert label_of(db, url) == "skip"

    async def test_an_answered_item_is_not_answered_twice(self, serve) -> None:
        url = "https://a.test/1"
        db = deployment(url)
        client = await serve(db)
        await answer(client, url, "up")

        response = await answer(client, url, "down")

        assert response.status == 409
        assert article(db, url)["state"] == "accepted"
        assert label_of(db, url) == "up"

    async def test_a_url_outside_the_sample_is_refused_untouched(self, serve) -> None:
        db = deployment("https://a.test/1")
        put(db, "https://a.test/other", "Other")
        before = article(db, "https://a.test/other")
        client = await serve(db)

        response = await answer(client, "https://a.test/other", "up")

        assert response.status == 409
        assert article(db, "https://a.test/other") == before

    @pytest.mark.parametrize(
        "body", [{"url": "https://a.test/1", "label": "maybe"}, {"label": "up"}, ["up"]]
    )
    async def test_a_malformed_answer_is_refused_untouched(self, serve, body) -> None:
        url = "https://a.test/1"
        db = deployment(url)
        before = article(db, url)
        client = await serve(db)

        response = await client.post("/api/labels", data=json.dumps(body))

        assert response.status == 400
        assert article(db, url) == before
        assert label_of(db, url) is None

    async def test_a_deployment_without_d1_takes_no_answer(self, serve) -> None:
        client = await serve(None)

        response = await answer(client, "https://a.test/1", "up")

        assert response.status == 409


class BrokenStore(D1ArticleStore):
    """An article store whose vote write fails, as D1 does when it is unreachable."""

    def accept(self, urls: list[str]) -> int:
        raise RuntimeError("D1 is unreachable")


class CheckingStore(D1ArticleStore):
    """An article store that records whether the sample held the answer before the vote."""

    def __init__(self, db: SqliteD1) -> None:
        super().__init__(db)
        self.db = db
        self.held_first: list[bool] = []

    def accept(self, urls: list[str]) -> int:
        self.held_first += [label_of(self.db, url) == "up" for url in urls]
        return super().accept(urls)


class TestTheSampleDecides:
    async def test_the_vote_is_written_only_once_the_sample_holds_the_answer(self, serve) -> None:
        url = "https://a.test/1"
        db = deployment(url)
        store = CheckingStore(db)
        client = await serve(db, store=store)

        response = await answer(client, url, "up")

        assert response.status == 200
        assert store.held_first == [True]

    async def test_the_losing_answer_of_a_race_writes_no_vote(self, serve) -> None:
        url = "https://a.test/1"
        db = deployment(url)
        client = await serve(db)
        await answer(client, url, "up")
        first = article(db, url)

        response = await answer(client, url, "down")

        assert response.status == 409
        assert "already answered" in (await response.json())["error"]
        assert article(db, url) == first
        assert label_of(db, url) == "up"

    async def test_a_vote_that_fails_to_write_leaves_the_item_open(self, serve) -> None:
        url = "https://a.test/1"
        db = deployment(url)
        before = article(db, url)
        client = await serve(db, store=BrokenStore(db))

        response = await answer(client, url, "up")

        assert response.status == 500
        assert "D1 is unreachable" in (await response.json())["error"]
        assert label_of(db, url) is None
        assert article(db, url) == before
        assert (await (await client.get("/api/labels")).json())["item"]["url"] == url
