"""Tests for triage web server API endpoints."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from fakes import SqliteD1

from cyris.adapters.store.article_store import ArticleStore
from cyris.adapters.store.d1_store import D1ArticleStore
from cyris.domain.models import Article, Tier
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


class TestTheDeckIsNotServed:
    """The swipe deck retired for raw's triage view; `/settings` is all that is left."""

    @pytest.mark.parametrize(
        "path",
        [
            "/triage",
            "/",
            "/static/index.html",
            "/static/app.js",
            "/static/deck.css",
            "/api/articles?limit=50&state=pending",
            "/api/stats",
        ],
    )
    async def test_a_deck_read_is_a_404(self, client: TestClient, path: str) -> None:
        assert (await client.get(path)).status == 404

    @pytest.mark.parametrize("action", ["accept", "reject", "undo"])
    async def test_a_deck_write_is_a_404(self, client: TestClient, action: str) -> None:
        resp = await client.post(f"/api/articles/{action}", json={"url": "https://example.com/1"})
        assert resp.status == 404

    @pytest.mark.parametrize("path", ["/settings", "/static/style.css", "/static/settings.js"])
    async def test_the_settings_page_and_its_files_still_answer(
        self, client: TestClient, path: str
    ) -> None:
        assert (await client.get(path)).status == 200

    async def test_the_build_endpoint_still_answers(self, client: TestClient) -> None:
        resp = await client.get("/api/build")
        assert resp.status == 200
        assert "git_sha" in await resp.json()


class TestBuildEndpoint:
    """The only surface that can say which image a deployment starts (§7 #33)."""

    async def test_reports_the_sha_baked_into_the_image(
        self, client: TestClient, monkeypatch
    ) -> None:
        monkeypatch.setenv("CYRIS_GIT_SHA", "0123456789abcdef0123456789abcdef01234567")
        resp = await client.get("/api/build")
        assert resp.status == 200
        assert (await resp.json())["git_sha"] == "0123456789abcdef0123456789abcdef01234567"

    async def test_an_unbaked_image_answers_with_an_empty_sha(
        self, client: TestClient, monkeypatch
    ) -> None:
        """A local `docker build` with no --build-arg is a legitimate image.

        The endpoint must still answer — `doctor` reads the empty string as the
        finding it is, and cannot do that against a 404 or a 500.
        """
        monkeypatch.delenv("CYRIS_GIT_SHA", raising=False)
        resp = await client.get("/api/build")
        assert resp.status == 200
        assert (await resp.json())["git_sha"] == ""


class TestSourcesEndpoint:
    """The settings page's source list and its write surface (§7 #15)."""

    async def test_lists_sources_with_origin(self, store_with_articles: ArticleStore) -> None:
        from cyris.domain.models import SourceConfig, Tier

        server = TriageServer(
            store_with_articles,
            sources={
                "feed": SourceConfig(name="feed", url="https://e.com/rss", tier=Tier.SUMMARIZE),
                "letter": SourceConfig(
                    name="letter", type="newsletter", email_match="from:a@b.com"
                ),
            },
            sources_origin="d1",
        )
        test_client = TestClient(TestServer(server._app))
        await test_client.start_server()
        try:
            data = await (await test_client.get("/api/sources")).json()
        finally:
            await test_client.close()

        assert data["origin"] == "d1"
        by_name = {s["name"]: s for s in data["sources"]}
        assert by_name["feed"]["tier"] == "summarize"
        assert by_name["letter"]["email_match"] == "from:a@b.com"

    async def test_no_sources_wired_is_empty_not_an_error(self, client: TestClient) -> None:
        data = await (await client.get("/api/sources")).json()
        assert data == {"origin": "unknown", "writable": False, "sources": []}

    async def test_writes_are_refused_without_a_writable_table(self, client: TestClient) -> None:
        """A `backend = "json"` deployment has nowhere to put a source."""
        resp = await client.post("/api/sources", json={"name": "x", "url": "https://e.com/rss"})
        assert resp.status == 409
        assert (await client.delete("/api/sources/x")).status == 409


class TestSourcesWriteSurface:
    """§7 #15: add, retire and re-tier a source over the existing D1 row."""

    @pytest.fixture
    async def client(self, store_with_articles: ArticleStore) -> TestClient:
        from cyris.adapters.store.source_store import D1SourceStore
        from cyris.domain.models import SourceConfig

        server = TriageServer(
            store_with_articles,
            sources={"From File": SourceConfig(name="From File", url="https://file.test/feed")},
            sources_origin="sources.yaml",
            source_store=D1SourceStore(SqliteD1()),
        )
        self.sources = server._source_store
        test_client = TestClient(TestServer(server._app))
        await test_client.start_server()
        yield test_client
        await test_client.close()

    async def test_first_write_seeds_the_table_before_adding(self, client: TestClient) -> None:
        """An empty `sources` table means "use sources.yaml".

        Writing one source into one would flip the pipeline to D1 with that
        source alone, and every feed the file serves would silently stop.
        """
        resp = await client.post(
            "/api/sources",
            json={"name": "New Feed", "url": "https://new.test/rss", "tier": "summarize"},
        )
        assert resp.status == 200

        stored = self.sources.list_sources()
        assert set(stored) == {"From File", "New Feed"}
        assert stored["New Feed"].tier.value == "summarize"

        data = await (await client.get("/api/sources")).json()
        assert data["origin"] == "d1"
        assert data["writable"] is True

    async def test_retiring_a_source_removes_its_row(self, client: TestClient) -> None:
        await client.post("/api/sources", json={"name": "New Feed", "url": "https://n.test/rss"})
        assert (await client.delete("/api/sources/From File")).status == 200
        assert set(self.sources.list_sources()) == {"New Feed"}

    async def test_re_tiering_replaces_the_row_it_owns(self, client: TestClient) -> None:
        await client.post(
            "/api/sources",
            json={"name": "From File", "url": "https://file.test/feed", "tier": "summarize"},
        )
        stored = self.sources.list_sources()
        assert len(stored) == 1
        assert stored["From File"].tier.value == "summarize"

    async def test_an_invalid_tier_is_a_400_not_a_row(self, client: TestClient) -> None:
        resp = await client.post("/api/sources", json={"name": "x", "tier": "nonsense"})
        assert resp.status == 400
        assert self.sources.list_sources() == {}
