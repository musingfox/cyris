"""Tests for triage web server API endpoints."""

from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from fakes import SqliteD1

from cyris.adapters.store.article_store import ArticleStore
from cyris.entrypoints.triage_server import TriageServer

pytestmark = pytest.mark.integration


@pytest.fixture
async def client() -> TestClient:
    """Create an aiohttp test client for the settings server."""
    server = TriageServer()
    test_server = TestServer(server._app)
    test_client = TestClient(test_server)
    await test_client.start_server()
    yield test_client
    await test_client.close()


class TestNoArticleStore:
    def test_a_positional_argument_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(TypeError):
            TriageServer(ArticleStore(tmp_path))


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

    def test_no_article_route_is_registered(self) -> None:
        # A POST to a deleted route and one for an unknown article both answer 404,
        # so only the route table tells the deck's write paths are really gone.
        paths = [r.canonical for r in TriageServer()._app.router.resources()]
        assert [p for p in paths if p.startswith("/api/articles")] == []

    @pytest.mark.parametrize("path", ["/settings", "/static/style.css", "/static/settings.js"])
    async def test_the_settings_page_and_its_files_still_answer(
        self, client: TestClient, path: str
    ) -> None:
        assert (await client.get(path)).status == 200


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

    async def test_lists_sources_without_an_origin(self) -> None:
        from cyris.domain.models import SourceConfig, Tier

        server = TriageServer(
            sources={
                "feed": SourceConfig(name="feed", url="https://e.com/rss", tier=Tier.SUMMARIZE),
                "letter": SourceConfig(
                    name="letter", type="newsletter", email_match="from:a@b.com"
                ),
            },
        )
        test_client = TestClient(TestServer(server._app))
        await test_client.start_server()
        try:
            data = await (await test_client.get("/api/sources")).json()
        finally:
            await test_client.close()

        assert set(data) == {"writable", "sources"}
        by_name = {s["name"]: s for s in data["sources"]}
        assert by_name["feed"]["tier"] == "summarize"
        assert by_name["letter"]["email_match"] == "from:a@b.com"

    async def test_no_sources_wired_is_empty_not_an_error(self, client: TestClient) -> None:
        data = await (await client.get("/api/sources")).json()
        assert data == {"writable": False, "sources": []}

    async def test_writes_are_refused_without_a_writable_table(self, client: TestClient) -> None:
        """A `backend = "json"` deployment has nowhere to put a source."""
        resp = await client.post("/api/sources", json={"name": "x", "url": "https://e.com/rss"})
        assert resp.status == 409
        assert (await client.delete("/api/sources/x")).status == 409


class TestSourcesWriteSurface:
    """§7 #15: add, retire and re-tier a source over the existing D1 row."""

    @pytest.fixture
    async def client(self) -> TestClient:
        from cyris.adapters.store.source_store import D1SourceStore
        from cyris.domain.models import SourceConfig

        server = TriageServer(
            sources={"From File": SourceConfig(name="From File", url="https://file.test/feed")},
            source_store=D1SourceStore(SqliteD1()),
        )
        self.sources = server._source_store
        test_client = TestClient(TestServer(server._app))
        await test_client.start_server()
        yield test_client
        await test_client.close()

    async def test_the_first_write_stores_that_source_alone(
        self, client: TestClient, monkeypatch
    ) -> None:
        """An empty table is no sources, not sources.yaml: nothing is seeded from the file."""
        from cyris.adapters.store.source_store import D1SourceStore

        def refuse(self, sources):
            raise AssertionError("the first write replaced the table")

        monkeypatch.setattr(D1SourceStore, "replace_all", refuse)
        resp = await client.post(
            "/api/sources",
            json={"name": "New", "type": "rss", "tier": "filter", "url": "https://n.test/feed"},
        )
        assert resp.status == 200

        assert set(self.sources.list_sources()) == {"New"}

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


class TestLastSource:
    """A deployment with no source stops running, so the page cannot retire the last one."""

    async def _delete(self, names: list[str], retire: str):
        from cyris.adapters.store.source_store import D1SourceStore
        from cyris.domain.models import SourceConfig

        store = D1SourceStore(SqliteD1())
        for name in names:
            store.upsert(SourceConfig(name=name, url=f"https://{name.lower()}.test/feed"))
        test_client = TestClient(TestServer(TriageServer(source_store=store)._app))
        await test_client.start_server()
        try:
            resp = await test_client.delete(f"/api/sources/{retire}")
            return resp.status, await resp.json(), set(store.list_sources())
        finally:
            await test_client.close()

    async def test_the_last_source_is_kept(self) -> None:
        status, body, left = await self._delete(["Only"], "Only")

        assert status == 409
        assert body == {
            "ok": False,
            "error": (
                "Only is the last source. A run with none stops, so add another before retiring it."
            ),
        }
        assert left == {"Only"}

    async def test_one_of_two_is_retired(self) -> None:
        status, _, left = await self._delete(["A", "B"], "A")

        assert status == 200
        assert left == {"B"}


def test_the_page_names_no_sources_origin() -> None:
    from cyris.entrypoints.triage_server import render_settings_page

    assert "sources-origin" not in render_settings_page()


def test_the_digest_category_offers_the_language_list_and_a_style_box() -> None:
    from cyris.entrypoints.triage_server import render_settings_page

    page = render_settings_page()

    assert '<datalist id="languages">' in page
    assert '<textarea class="input" id="style-prompt" rows="4">' in page


async def test_an_emptied_table_is_listed_empty_not_as_the_startup_list() -> None:
    from cyris.adapters.store.source_store import D1SourceStore
    from cyris.domain.models import SourceConfig

    server = TriageServer(
        sources={"Old": SourceConfig(name="Old", url="https://old.test/feed")},
        source_store=D1SourceStore(SqliteD1()),
    )
    test_client = TestClient(TestServer(server._app))
    await test_client.start_server()
    try:
        data = await (await test_client.get("/api/sources")).json()
    finally:
        await test_client.close()

    assert data == {"writable": True, "sources": []}
