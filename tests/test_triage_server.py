"""Tests for triage web server API endpoints."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from cyris.adapters.store.article_store import ArticleStore
from cyris.domain.models import Article, ArticleState, Tier
from cyris.entrypoints.triage_server import TriageServer


@pytest.fixture
def store_with_articles(tmp_path: Path) -> ArticleStore:
    """Create a store with scored pending articles."""
    store = ArticleStore(tmp_path)
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
    async def test_reject_success(
        self, client: TestClient, store_with_articles: ArticleStore
    ) -> None:
        resp = await client.post(
            "/api/articles/reject",
            json={"url": "https://example.com/3"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True

        # Verify state changed to REJECTED with reason
        articles = store_with_articles.list_articles(state=ArticleState.REJECTED)
        rejected = [a for a in articles if a.url == "https://example.com/3"]
        assert len(rejected) == 1
        assert rejected[0].rejection_reason == "manual_triage"

    async def test_reject_removes_from_pending(
        self, client: TestClient, store_with_articles: ArticleStore
    ) -> None:
        await client.post(
            "/api/articles/reject",
            json={"url": "https://example.com/3"},
        )
        # Article should no longer appear in pending list
        resp = await client.get("/api/articles?limit=10")
        data = await resp.json()
        urls = [a["url"] for a in data["articles"]]
        assert "https://example.com/3" not in urls


class TestIndexPage:
    async def test_index_serves_html(self, client: TestClient) -> None:
        resp = await client.get("/")
        assert resp.status == 200
        text = await resp.text()
        assert "Cyris Triage" in text


class TestAcceptWithVaultExport:
    async def test_accept_exports_to_vault(
        self, store_with_articles: ArticleStore, tmp_path: Path
    ) -> None:
        """Accept should export to vault when vault_path is configured."""
        vault_path = tmp_path / "vault"
        vault_path.mkdir()

        server = TriageServer(store_with_articles, vault_path=vault_path)
        test_server = TestServer(server._app)
        test_client = TestClient(test_server)
        await test_client.start_server()

        try:
            resp = await test_client.post(
                "/api/articles/accept",
                json={"url": "https://example.com/1"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["exported"] is True

            # Verify file exists in vault
            reading_folder = vault_path / "Reading"
            assert reading_folder.exists()
            md_files = list(reading_folder.glob("*.md"))
            assert len(md_files) == 1

            # Verify article state is ACCEPTED
            articles = store_with_articles.list_articles(state=ArticleState.ACCEPTED)
            urls = [a.url for a in articles]
            assert "https://example.com/1" in urls
        finally:
            await test_client.close()

    async def test_accept_without_vault_configured(self, store_with_articles: ArticleStore) -> None:
        """Accept should still work when vault_path is None."""
        server = TriageServer(store_with_articles, vault_path=None)
        test_server = TestServer(server._app)
        test_client = TestClient(test_server)
        await test_client.start_server()

        try:
            resp = await test_client.post(
                "/api/articles/accept",
                json={"url": "https://example.com/1"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["exported"] is False

            # Verify article state is ACCEPTED
            articles = store_with_articles.list_articles(state=ArticleState.ACCEPTED)
            urls = [a.url for a in articles]
            assert "https://example.com/1" in urls
        finally:
            await test_client.close()

    async def test_accept_with_export_failure_still_updates_state(
        self, store_with_articles: ArticleStore, tmp_path: Path
    ) -> None:
        """Accept should update state even if export fails."""
        vault_path = tmp_path / "nonexistent"
        # Don't create the directory to trigger export failure

        server = TriageServer(store_with_articles, vault_path=vault_path)
        test_server = TestServer(server._app)
        test_client = TestClient(test_server)
        await test_client.start_server()

        try:
            resp = await test_client.post(
                "/api/articles/accept",
                json={"url": "https://example.com/1"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["exported"] is False

            # Verify article state is ACCEPTED (state update succeeded despite export failure)
            articles = store_with_articles.list_articles(state=ArticleState.ACCEPTED)
            urls = [a.url for a in articles]
            assert "https://example.com/1" in urls
        finally:
            await test_client.close()


class TestRejectWithMiniflux:
    """Tests for reject endpoint with Miniflux integration."""

    @pytest.fixture
    def mock_miniflux_client(self) -> Mock:
        """Mock MinifluxClient with async mark_as_read."""
        client = Mock()
        client.mark_as_read = AsyncMock()
        return client

    @pytest.fixture
    def store_with_miniflux_articles(self, tmp_path: Path) -> ArticleStore:
        """Create store with articles having int (Miniflux) and str (newsletter) original_id."""
        store = ArticleStore(tmp_path)
        now = datetime.now(UTC)
        articles = [
            Article(
                id=12345,  # Miniflux article with int ID
                title="Miniflux Article",
                url="https://example.com/miniflux",
                content="Content from Miniflux feed",
                published_at=now,
                source_name="RSS Feed",
                source_tier=Tier.FILTER,
            ),
            Article(
                id="newsletter-abc",  # Newsletter article with str ID
                title="Newsletter Article",
                url="https://example.com/newsletter",
                content="Content from newsletter",
                published_at=now,
                source_name="Newsletter",
                source_tier=Tier.SUMMARIZE,
            ),
        ]
        store.save(articles, now=now)
        return store

    async def test_reject_marks_miniflux_read(
        self, store_with_miniflux_articles: ArticleStore, mock_miniflux_client: Mock
    ) -> None:
        """Reject should mark Miniflux entry as read when article has int original_id."""
        server = TriageServer(store_with_miniflux_articles, miniflux_client=mock_miniflux_client)
        test_server = TestServer(server._app)
        test_client = TestClient(test_server)
        await test_client.start_server()

        try:
            resp = await test_client.post(
                "/api/articles/reject",
                json={"url": "https://example.com/miniflux"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["marked_read"] is True

            # Verify mark_as_read was called with correct ID
            mock_miniflux_client.mark_as_read.assert_awaited_once_with([12345])

            # Verify article state is REJECTED
            articles = store_with_miniflux_articles.list_articles(state=ArticleState.REJECTED)
            urls = [a.url for a in articles]
            assert "https://example.com/miniflux" in urls
        finally:
            await test_client.close()

    async def test_reject_skips_non_miniflux_article(
        self, store_with_miniflux_articles: ArticleStore, mock_miniflux_client: Mock
    ) -> None:
        """Reject should skip Miniflux marking for articles with str original_id."""
        server = TriageServer(store_with_miniflux_articles, miniflux_client=mock_miniflux_client)
        test_server = TestServer(server._app)
        test_client = TestClient(test_server)
        await test_client.start_server()

        try:
            resp = await test_client.post(
                "/api/articles/reject",
                json={"url": "https://example.com/newsletter"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["marked_read"] is False

            # Verify mark_as_read was NOT called
            mock_miniflux_client.mark_as_read.assert_not_awaited()

            # Verify article state is REJECTED
            articles = store_with_miniflux_articles.list_articles(state=ArticleState.REJECTED)
            urls = [a.url for a in articles]
            assert "https://example.com/newsletter" in urls
        finally:
            await test_client.close()

    async def test_reject_without_miniflux_client(
        self, store_with_miniflux_articles: ArticleStore
    ) -> None:
        """Reject should work when miniflux_client is None."""
        server = TriageServer(store_with_miniflux_articles, miniflux_client=None)
        test_server = TestServer(server._app)
        test_client = TestClient(test_server)
        await test_client.start_server()

        try:
            resp = await test_client.post(
                "/api/articles/reject",
                json={"url": "https://example.com/miniflux"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["marked_read"] is False

            # Verify article state is REJECTED
            articles = store_with_miniflux_articles.list_articles(state=ArticleState.REJECTED)
            urls = [a.url for a in articles]
            assert "https://example.com/miniflux" in urls
        finally:
            await test_client.close()

    async def test_reject_miniflux_api_failure_still_rejects(
        self, store_with_miniflux_articles: ArticleStore, mock_miniflux_client: Mock
    ) -> None:
        """Reject should update state even if Miniflux API fails."""
        mock_miniflux_client.mark_as_read.side_effect = Exception("API error")

        server = TriageServer(store_with_miniflux_articles, miniflux_client=mock_miniflux_client)
        test_server = TestServer(server._app)
        test_client = TestClient(test_server)
        await test_client.start_server()

        try:
            resp = await test_client.post(
                "/api/articles/reject",
                json={"url": "https://example.com/miniflux"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["marked_read"] is False

            # Verify article state is REJECTED (state update succeeded)
            articles = store_with_miniflux_articles.list_articles(state=ArticleState.REJECTED)
            urls = [a.url for a in articles]
            assert "https://example.com/miniflux" in urls
        finally:
            await test_client.close()


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
