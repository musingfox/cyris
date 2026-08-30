"""Tests for triage web server API endpoints."""

import shutil
import subprocess
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


# Shared node scaffold for executable UI contract tests: a minimal DOM stub
# for app.js. Each test appends its own scenario (timers, articles, fetch
# stub, assertions) before handing the script to node.
_UI_HARNESS_SCAFFOLD = r"""
const fs = require("fs");
const vm = require("vm");

class ClassList {
  constructor(...names) { this.names = new Set(names); }
  add(...names) { names.forEach((name) => this.names.add(name)); }
  remove(...names) { names.forEach((name) => this.names.delete(name)); }
  toggle(name, force) {
    if (force === undefined ? !this.names.has(name) : force) this.names.add(name);
    else this.names.delete(name);
  }
  contains(name) { return this.names.has(name); }
}

class Element {
  constructor(id = "") {
    this.id = id;
    this.children = [];
    this.dataset = {};
    this.listeners = {};
    this.classList = new ClassList();
    this.style = {};
    this.textContent = "";
  }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  appendChild(child) { this.children.push(child); }
  replaceChildren(...children) { this.children = children; }
  querySelector(selector) {
    return selector === ".card" ? this.children[0] || null : null;
  }
}

const ids = [
  "deck", "empty-state", "counter", "toast", "undo-toast", "undo-message",
  "undo-button", "swipe-footer", "history-footer", "prev-btn", "next-btn",
  "count-pending", "count-accepted", "count-rejected", "count-all",
];
const elements = Object.fromEntries(ids.map((id) => [id, new Element(id)]));
["empty-state", "toast", "undo-toast", "history-footer"].forEach(
  (id) => elements[id].classList.add("hidden")
);
const docListeners = {};
const tabs = ["pending", "accepted", "rejected", "all"].map((state) => {
  const tab = new Element();
  tab.dataset.state = state;
  return tab;
});

global.document = {
  getElementById: (id) => elements[id],
  createElement: () => new Element(),
  querySelectorAll: () => tabs,
  addEventListener: (name, callback) => { docListeners[name] = callback; },
};
"""


class TestRejectActionsUI:
    static_dir = Path(__file__).parents[1] / "src/cyris/entrypoints/static"

    def test_swipe_footer_is_swipe_only(self) -> None:
        html = (self.static_dir / "index.html").read_text()
        footer = html[html.index('<footer id="swipe-footer">') : html.index("</footer>")]

        assert "reject" in footer and "accept" in footer
        assert "data-reason" not in footer
        assert "已知道" not in footer

    def test_reject_paths_send_their_reason(self) -> None:
        source = (self.static_dir / "app.js").read_text()

        assert "JSON.stringify({ url: url, reason: reason })" in source
        assert 'postAction("reject", article.url, "not_interested")' in source
        assert 'e.key === "k"' not in source
        assert "already_known" not in source

    @pytest.mark.skipif(
        shutil.which("node") is None,
        reason="Node.js not installed; the executable UI contract test needs it",
    )
    def test_reject_responses_preserve_or_record_verdict(self) -> None:
        node = shutil.which("node")
        app_path = self.static_dir / "app.js"
        harness = (
            _UI_HARNESS_SCAFFOLD
            + r"""
global.setTimeout = () => 1;
global.clearTimeout = () => {};

const article = {
  url: "https://example.com/1",
  title: "Article",
  content: "content",
  published_at: "2026-08-28T00:00:00Z",
};
let rejectBody;
let rejectOk = false;
global.fetch = async (url, options = {}) => {
  if (url.startsWith("/api/articles?")) {
    return {json: async () => ({articles: [article]})};
  }
  if (url === "/api/stats") {
    return {json: async () => ({pending: 1, accepted: 0, rejected: 0, total: 1})};
  }
  if (url === "/api/articles/reject") {
    rejectBody = JSON.parse(options.body);
    return {json: async () => rejectOk ? {ok: true} : {ok: false, error: "denied"}};
  }
  throw new Error("unexpected fetch " + url);
};

vm.runInThisContext(fs.readFileSync(process.argv[1], "utf8"));

(async () => {
  await new Promise(setImmediate);
  const originalCard = elements.deck.children[0];
  docListeners.keydown({key: "ArrowLeft"});
  await new Promise(setImmediate);

  if (rejectBody.reason !== "not_interested") throw new Error("reason not sent");
  if (elements.toast.textContent !== "Error: denied") throw new Error("toast not shown");
  if (elements.toast.classList.contains("hidden")) throw new Error("toast hidden");
  if (elements.deck.children[0] === originalCard) throw new Error("card not re-rendered");
  if (elements.counter.textContent !== "1 remaining") throw new Error("verdict advanced");
  if (!elements["undo-toast"].classList.contains("hidden")) throw new Error("undo recorded");

  rejectOk = true;
  docListeners.keydown({key: "ArrowLeft"});
  await new Promise(setImmediate);
  if (elements["undo-message"].textContent !== "Rejected") {
    throw new Error("undo not shown as rejected");
  }
  if (elements["undo-toast"].classList.contains("hidden")) {
    throw new Error("undo not shown");
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
        )
        subprocess.run([node, "-e", harness, str(app_path)], check=True)

    @pytest.mark.skipif(
        shutil.which("node") is None,
        reason="Node.js not installed; the executable UI contract test needs it",
    )
    def test_double_press_rejects_only_the_seen_article(self) -> None:
        """A second reject before the 300ms re-render must not hit the next card.

        postAction advances currentIndex as soon as the fetch resolves but keeps
        the old card on screen for 300ms, so without the in-flight guard a
        double-press stamps a human rejection on an article the user never saw.
        """
        node = shutil.which("node")
        app_path = self.static_dir / "app.js"
        harness = (
            _UI_HARNESS_SCAFFOLD
            + r"""
const timers = [];
global.setTimeout = (fn) => timers.push(fn);
global.clearTimeout = () => {};

const articles = [1, 2].map((n) => ({
  url: "https://example.com/" + n,
  title: "Article " + n,
  content: "content",
  published_at: "2026-08-28T00:00:00Z",
}));
const rejected = [];
global.fetch = async (url, options = {}) => {
  if (url.startsWith("/api/articles?")) {
    return {json: async () => ({articles: articles})};
  }
  if (url === "/api/stats") {
    return {json: async () => ({pending: 2, accepted: 0, rejected: 0, total: 2})};
  }
  if (url === "/api/articles/reject") {
    rejected.push(JSON.parse(options.body).url);
    return {json: async () => ({ok: true})};
  }
  throw new Error("unexpected fetch " + url);
};

vm.runInThisContext(fs.readFileSync(process.argv[1], "utf8"));

(async () => {
  await new Promise(setImmediate);

  // Double-press: the first verdict is in flight / its card still shown.
  docListeners.keydown({key: "ArrowLeft"});
  docListeners.keydown({key: "ArrowLeft"});
  await new Promise(setImmediate);
  docListeners.keydown({key: "ArrowLeft"});  // fetch resolved, re-render still pending
  await new Promise(setImmediate);

  if (rejected.length !== 1 || rejected[0] !== "https://example.com/1") {
    throw new Error("extra clicks leaked a verdict: " + JSON.stringify(rejected));
  }

  // After the delayed re-render the next card is visible and clickable again.
  timers.splice(0).forEach((fn) => fn());
  docListeners.keydown({key: "ArrowLeft"});
  await new Promise(setImmediate);

  if (rejected.length !== 2 || rejected[1] !== "https://example.com/2") {
    throw new Error("guard stuck after re-render: " + JSON.stringify(rejected));
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
        )
        subprocess.run([node, "-e", harness, str(app_path)], check=True)
