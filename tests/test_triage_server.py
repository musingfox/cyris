"""Tests for triage web server API endpoints."""

from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from fakes import SqliteD1

from cyris.adapters.output.html_digest import FAVICON
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

    async def test_the_favicon_the_settings_page_links_is_served(self, client: TestClient) -> None:
        response = await client.get("/favicon.svg")

        assert response.status == 200
        assert response.content_type == "image/svg+xml"
        assert await response.read() == FAVICON.read_bytes()


class TestBuildEndpoint:
    """The only surface that can say which image a deployment starts."""

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
    """The settings page's source list and its write surface."""

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
    """Add, retire and re-tier a source over the existing D1 row."""

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
        assert await resp.json() == {
            "ok": False,
            "error": "tier: Input should be 'filter', 'summarize' or 'fan'",
            "field": "tier",
        }
        assert self.sources.list_sources() == {}

    async def test_a_body_that_is_not_an_object_is_one_line(self, client: TestClient) -> None:
        resp = await client.post("/api/sources", json=["x"])
        assert resp.status == 400
        assert await resp.json() == {
            "ok": False,
            "error": "body: Input should be a valid dictionary or instance of SourceConfig",
            "field": "",
        }

    async def test_a_blank_name_is_a_400_naming_the_field(self, client: TestClient) -> None:
        resp = await client.post("/api/sources", json={"name": " ", "url": "https://n.test/rss"})
        assert resp.status == 400
        assert await resp.json() == {
            "ok": False,
            "error": "A source needs a name.",
            "field": "name",
        }

    @pytest.mark.parametrize(
        ("body", "error", "field"),
        [
            ({"type": "rss"}, "An RSS source needs a Feed URL.", "url"),
            ({"type": "rss", "url": "  "}, "An RSS source needs a Feed URL.", "url"),
            ({"type": "newsletter"}, "A newsletter source needs a Sender match.", "email_match"),
            (
                {"type": "newsletter", "url": "https://n.test/feed"},
                "A newsletter source needs a Sender match.",
                "email_match",
            ),
            (
                {"type": "web", "url": "https://n.test/feed"},
                "A source's type is rss or newsletter, not 'web'.",
                "type",
            ),
        ],
    )
    async def test_a_source_no_fetcher_reads_is_a_400_not_a_row(
        self, client: TestClient, body: dict, error: str, field: str
    ) -> None:
        resp = await client.post("/api/sources", json={"name": "x", **body})

        assert resp.status == 400
        assert await resp.json() == {"ok": False, "error": error, "field": field}
        assert self.sources.list_sources() == {}

    async def test_a_newsletter_with_a_sender_match_is_stored(self, client: TestClient) -> None:
        resp = await client.post(
            "/api/sources",
            json={"name": "Letter", "type": "newsletter", "email_match": "from:hi@letter.test"},
        )

        assert resp.status == 200
        assert self.sources.list_sources()["Letter"].email_match == "from:hi@letter.test"


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


class TestSourcesHealth:
    """Each RSS source carries the health `cyris doctor` judges, read from the same tables."""

    @staticmethod
    async def _get(db, feed_health) -> dict:
        from cyris.adapters.store.source_store import D1SourceStore
        from cyris.domain.models import SourceConfig

        store = D1SourceStore(db)
        store.upsert(SourceConfig(name="failing", url="https://failing.test/feed"))
        store.upsert(SourceConfig(name="never", url="https://never.test/feed"))
        store.upsert(
            SourceConfig(name="letter", type="newsletter", email_match="from:a@letter.test")
        )
        server = TriageServer(source_store=store, feed_health=feed_health)
        test_client = TestClient(TestServer(server._app))
        await test_client.start_server()
        try:
            response = await test_client.get("/api/sources")
            assert response.status == 200
            data = await response.json()
        finally:
            await test_client.close()
        return {s["name"]: s for s in data["sources"]}

    async def test_each_rss_source_carries_its_health_and_its_problems(self) -> None:
        from datetime import UTC, datetime

        from cyris.adapters.store.feed_health import D1FeedHealth

        db = SqliteD1()
        seen = datetime.now(UTC).isoformat()
        db.query(
            "INSERT INTO stored_articles (url, published_at, source_name, source_tier, "
            "first_seen_at) VALUES ('https://failing.test/1', ?, 'failing', 'filter', ?)",
            [seen, seen],
        )
        db.query(
            "INSERT INTO feed_health (name, consecutive_failures, last_error, last_failed_at, "
            "last_ok_at) VALUES ('failing', 3, 'HTTP 503', '2026-10-08T11:00:00.000Z', NULL)"
        )

        by_name = await self._get(db, D1FeedHealth(db))

        assert by_name["failing"]["health"] == {
            "consecutive_failures": 3,
            "last_error": "HTTP 503",
            "last_failed_at": "2026-10-08T11:00:00.000Z",
            "last_ok_at": None,
            "newest_article_at": seen,
            "problems": ["3 failures in a row · HTTP 503"],
        }
        assert by_name["never"]["health"]["problems"] == ["no article stored yet"]
        assert by_name["letter"]["health"] is None

    async def test_without_a_health_reader_no_source_carries_health(self) -> None:
        by_name = await self._get(SqliteD1(), None)
        assert {s["health"] for s in by_name.values()} == {None}

    async def test_an_unreadable_health_table_still_lists_the_sources(self) -> None:
        class Down:
            def read(self):
                raise RuntimeError("D1 is down")

        by_name = await self._get(SqliteD1(), Down())

        assert set(by_name) == {"failing", "never", "letter"}
        assert {s["health"] for s in by_name.values()} == {None}


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
