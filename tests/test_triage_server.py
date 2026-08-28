"""Tests for triage web server API endpoints."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from fakes import SqliteD1

from cyris.adapters.store.article_store import ArticleStore
from cyris.adapters.store.d1_store import D1ArticleStore
from cyris.domain.models import Article, ArticleState, Tier
from cyris.entrypoints.triage_server import TriageServer


@pytest.fixture(params=["json", "d1"])
def store_with_articles(request, tmp_path: Path):
    """A store with scored pending articles, once per backend.

    The triage UI is the knowledge gate and the only source of real-human
    training signal, so it has to work identically on both.
    """
    store = ArticleStore(tmp_path) if request.param == "json" else D1ArticleStore(SqliteD1())
    now = datetime.now(UTC)
    articles = [
        Article(
            id=1,
            title="High Score Chinese",
            url="https://example.com/1",
            content="這是一篇高分中文文章，內容非常豐富且深入探討了人工智慧的最新發展",
            published_at=now,
            source_name="iThome",
            source_tier=Tier.SUMMARIZE,
        ),
        Article(
            id=2,
            title="High Score English",
            url="https://example.com/2",
            content="A high-quality English article about cloud computing trends",
            published_at=now,
            source_name="TechCrunch",
            source_tier=Tier.FILTER,
            source_tags=["tech"],
        ),
        Article(
            id=3,
            title="Low Score Article",
            url="https://example.com/3",
            content="Short filler content",
            published_at=now,
            source_name="Random Blog",
            source_tier=Tier.FILTER,
        ),
    ]
    store.save(articles, now=now)
    store.update_scores(
        {
            "https://example.com/1": (85.0, "zh"),
            "https://example.com/2": (85.0, "en"),
            "https://example.com/3": (30.0, "en"),
        }
    )
    return store


@pytest.fixture
async def client(store_with_articles: ArticleStore) -> TestClient:
    """Create an aiohttp test client for the triage server."""
    server = TriageServer(store_with_articles)
    test_server = TestServer(server._app)
    test_client = TestClient(test_server)
    await test_client.start_server()
    yield test_client
    await test_client.close()


class TestListArticles:
    async def test_list_returns_pending_articles(self, client: TestClient) -> None:
        resp = await client.get("/api/articles?limit=10")
        assert resp.status == 200
        data = await resp.json()
        assert "articles" in data
        assert "total" in data
        assert data["total"] == 3

    async def test_list_sorted_by_score_desc(self, client: TestClient) -> None:
        resp = await client.get("/api/articles?limit=10")
        data = await resp.json()
        articles = data["articles"]
        scores = [a["score"] for a in articles]
        # High scores first, low score last
        assert scores[0] >= scores[-1]

    async def test_list_chinese_first_on_tie(self, client: TestClient) -> None:
        resp = await client.get("/api/articles?limit=10")
        data = await resp.json()
        articles = data["articles"]
        # First two articles have same score (85), Chinese should come first
        tied = [a for a in articles if a["score"] == 85.0]
        assert len(tied) == 2
        assert tied[0]["language"] == "zh"
        assert tied[1]["language"] == "en"

    async def test_list_with_limit(self, client: TestClient) -> None:
        resp = await client.get("/api/articles?limit=1")
        data = await resp.json()
        assert len(data["articles"]) == 1

    async def test_list_invalid_limit(self, client: TestClient) -> None:
        resp = await client.get("/api/articles?limit=999")
        assert resp.status == 400

    async def test_list_invalid_limit_string(self, client: TestClient) -> None:
        resp = await client.get("/api/articles?limit=abc")
        assert resp.status == 400


class TestAcceptArticle:
    async def test_accept_success(
        self, client: TestClient, store_with_articles: ArticleStore
    ) -> None:
        resp = await client.post(
            "/api/articles/accept",
            json={"url": "https://example.com/1"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True

        # Verify state changed
        articles = store_with_articles.list_articles(state=ArticleState.ACCEPTED)
        urls = [a.url for a in articles]
        assert "https://example.com/1" in urls

    async def test_accept_not_found(self, client: TestClient) -> None:
        resp = await client.post(
            "/api/articles/accept",
            json={"url": "https://nonexistent.com"},
        )
        assert resp.status == 404
        data = await resp.json()
        assert data["ok"] is False

    async def test_accept_missing_url(self, client: TestClient) -> None:
        resp = await client.post(
            "/api/articles/accept",
            json={},
        )
        assert resp.status == 400

    async def test_accept_invalid_json(self, client: TestClient) -> None:
        resp = await client.post(
            "/api/articles/accept",
            data=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400


class TestRejectArticle:
    async def test_reject_with_explicit_reason(
        self, client: TestClient, store_with_articles: ArticleStore
    ) -> None:
        resp = await client.post(
            "/api/articles/reject",
            json={"url": "https://example.com/3", "reason": "already_known"},
        )
        assert resp.status == 200
        assert await resp.json() == {"ok": True}

        [article] = store_with_articles.get_by_urls(["https://example.com/3"])
        assert article.rejection_reason == "already_known"
        assert article.triaged_at is not None

    async def test_reject_defaults_to_not_interested(
        self, client: TestClient, store_with_articles: ArticleStore
    ) -> None:
        resp = await client.post(
            "/api/articles/reject",
            json={"url": "https://example.com/3"},
        )
        assert resp.status == 200

        [article] = store_with_articles.get_by_urls(["https://example.com/3"])
        assert article.rejection_reason == "not_interested"

    async def test_reject_invalid_reason_leaves_article_pending(
        self, client: TestClient, store_with_articles: ArticleStore
    ) -> None:
        resp = await client.post(
            "/api/articles/reject",
            json={"url": "https://example.com/3", "reason": "banana"},
        )
        assert resp.status == 400
        assert (await resp.json())["ok"] is False

        [article] = store_with_articles.get_by_urls(["https://example.com/3"])
        assert article.state == ArticleState.PENDING
        assert article.rejection_reason is None
        assert article.triaged_at is None

    async def test_reject_not_found(self, client: TestClient) -> None:
        resp = await client.post(
            "/api/articles/reject",
            json={"url": "https://nope"},
        )
        assert resp.status == 404

    async def test_reject_removes_from_pending(
        self, client: TestClient, store_with_articles: ArticleStore
    ) -> None:
        await client.post(
            "/api/articles/reject",
            json={"url": "https://example.com/3"},
        )
        resp = await client.get("/api/articles?limit=10")
        data = await resp.json()
        urls = [a["url"] for a in data["articles"]]
        assert "https://example.com/3" not in urls


class TestTriageStamps:
    """triaged_at is what separates a human label from the pipeline's own verdict."""

    async def test_accept_and_reject_stamp_triaged_at(
        self, client: TestClient, store_with_articles: ArticleStore
    ) -> None:
        await client.post("/api/articles/accept", json={"url": "https://example.com/1"})
        await client.post("/api/articles/reject", json={"url": "https://example.com/3"})

        [accepted] = store_with_articles.get_by_urls(["https://example.com/1"])
        [rejected] = store_with_articles.get_by_urls(["https://example.com/3"])
        assert accepted.triaged_at is not None
        assert rejected.triaged_at is not None

    async def test_undo_clears_the_stamp(
        self, client: TestClient, store_with_articles: ArticleStore
    ) -> None:
        await client.post("/api/articles/reject", json={"url": "https://example.com/3"})
        await client.post("/api/articles/undo", json={"url": "https://example.com/3"})

        [article] = store_with_articles.get_by_urls(["https://example.com/3"])
        assert article.state == ArticleState.PENDING
        assert article.triaged_at is None


class TestIndexPage:
    async def test_index_serves_html(self, client: TestClient) -> None:
        resp = await client.get("/")
        assert resp.status == 200
        text = await resp.text()
        assert "Cyris Triage" in text


class TestStatsAndFilter:
    """Tests for stats endpoint and state filtering."""

    @pytest.fixture
    def store_with_mixed_states(self, tmp_path: Path) -> ArticleStore:
        """Create store with articles in different states."""
        store = ArticleStore(tmp_path)
        now = datetime.now(UTC)
        articles = [
            Article(
                id=1,
                title="Pending 1",
                url="https://example.com/pending1",
                content="Content",
                published_at=now,
                source_name="Source",
                source_tier=Tier.FILTER,
            ),
            Article(
                id=2,
                title="Pending 2",
                url="https://example.com/pending2",
                content="Content",
                published_at=now,
                source_name="Source",
                source_tier=Tier.FILTER,
            ),
            Article(
                id=3,
                title="To Accept",
                url="https://example.com/accept",
                content="Content",
                published_at=now,
                source_name="Source",
                source_tier=Tier.FILTER,
            ),
            Article(
                id=4,
                title="To Reject",
                url="https://example.com/reject",
                content="Content",
                published_at=now,
                source_name="Source",
                source_tier=Tier.FILTER,
            ),
        ]
        store.save(articles, now=now)
        store.update_states(
            {
                "https://example.com/accept": (ArticleState.ACCEPTED, None),
                "https://example.com/reject": (ArticleState.REJECTED, "noise"),
            },
            digest_date="2026-03-30",
        )
        return store

    async def test_stats_returns_counts(
        self, store_with_mixed_states: ArticleStore, tmp_path: Path
    ) -> None:
        """GET /api/stats returns correct state counts."""
        server = TriageServer(store_with_mixed_states)
        test_server = TestServer(server._app)
        test_client = TestClient(test_server)
        await test_client.start_server()

        try:
            resp = await test_client.get("/api/stats")
            assert resp.status == 200
            data = await resp.json()
            assert data["pending"] == 2
            assert data["accepted"] == 1
            assert data["rejected"] == 1
            assert data["total"] == 4
        finally:
            await test_client.close()

    async def test_list_filter_pending(
        self, store_with_mixed_states: ArticleStore, tmp_path: Path
    ) -> None:
        """GET /api/articles?state=pending returns only pending articles."""
        server = TriageServer(store_with_mixed_states)
        test_server = TestServer(server._app)
        test_client = TestClient(test_server)
        await test_client.start_server()

        try:
            resp = await test_client.get("/api/articles?state=pending&limit=10")
            assert resp.status == 200
            data = await resp.json()
            assert len(data["articles"]) == 2
            assert data["total"] == 2
            for article in data["articles"]:
                assert article["state"] == "pending"
        finally:
            await test_client.close()

    async def test_list_filter_accepted(
        self, store_with_mixed_states: ArticleStore, tmp_path: Path
    ) -> None:
        """GET /api/articles?state=accepted returns only accepted articles."""
        server = TriageServer(store_with_mixed_states)
        test_server = TestServer(server._app)
        test_client = TestClient(test_server)
        await test_client.start_server()

        try:
            resp = await test_client.get("/api/articles?state=accepted&limit=10")
            assert resp.status == 200
            data = await resp.json()
            assert len(data["articles"]) == 1
            assert data["total"] == 1
            assert data["articles"][0]["state"] == "accepted"
        finally:
            await test_client.close()

    async def test_list_filter_all(
        self, store_with_mixed_states: ArticleStore, tmp_path: Path
    ) -> None:
        """GET /api/articles?state=all returns all articles."""
        server = TriageServer(store_with_mixed_states)
        test_server = TestServer(server._app)
        test_client = TestClient(test_server)
        await test_client.start_server()

        try:
            resp = await test_client.get("/api/articles?state=all&limit=10")
            assert resp.status == 200
            data = await resp.json()
            assert len(data["articles"]) == 4
            assert data["total"] == 4
            states = {article["state"] for article in data["articles"]}
            assert states == {"pending", "accepted", "rejected"}
        finally:
            await test_client.close()

    async def test_undo_reverts_to_pending(
        self, store_with_mixed_states: ArticleStore, tmp_path: Path
    ) -> None:
        """POST /api/articles/undo reverts article to PENDING."""
        server = TriageServer(store_with_mixed_states)
        test_server = TestServer(server._app)
        test_client = TestClient(test_server)
        await test_client.start_server()

        try:
            # Undo accepted article
            resp = await test_client.post(
                "/api/articles/undo",
                json={"url": "https://example.com/accept"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True

            # Verify state is now PENDING
            articles = store_with_mixed_states.list_articles(state=ArticleState.PENDING)
            urls = [a.url for a in articles]
            assert "https://example.com/accept" in urls

            # Verify no longer in ACCEPTED
            accepted = store_with_mixed_states.list_articles(state=ArticleState.ACCEPTED)
            urls = [a.url for a in accepted]
            assert "https://example.com/accept" not in urls
        finally:
            await test_client.close()

    async def test_undo_not_found(self, store_with_mixed_states: ArticleStore) -> None:
        """POST /api/articles/undo for non-existent URL returns 404."""
        server = TriageServer(store_with_mixed_states)
        test_server = TestServer(server._app)
        test_client = TestClient(test_server)
        await test_client.start_server()

        try:
            resp = await test_client.post(
                "/api/articles/undo",
                json={"url": "https://nonexistent.com"},
            )
            assert resp.status == 404
            data = await resp.json()
            assert data["ok"] is False
        finally:
            await test_client.close()

    async def test_undo_missing_url(self, store_with_mixed_states: ArticleStore) -> None:
        """POST /api/articles/undo without url returns 400."""
        server = TriageServer(store_with_mixed_states)
        test_server = TestServer(server._app)
        test_client = TestClient(test_server)
        await test_client.start_server()

        try:
            resp = await test_client.post(
                "/api/articles/undo",
                json={},
            )
            assert resp.status == 400
        finally:
            await test_client.close()
