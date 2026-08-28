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


class TestRejectActionsUI:
    static_dir = Path(__file__).parents[1] / "src/cyris/entrypoints/static"

    def test_swipe_footer_has_both_reject_buttons(self) -> None:
        html = (self.static_dir / "index.html").read_text()
        footer = html[html.index('<footer id="swipe-footer">') : html.index("</footer>")]

        assert 'data-reason="not_interested"' in footer
        assert 'data-reason="already_known"' in footer
        assert "沒興趣" in footer
        assert "已知道" in footer

    def test_reject_paths_send_their_reason(self) -> None:
        source = (self.static_dir / "app.js").read_text()

        assert "JSON.stringify({ url: url, reason: reason })" in source
        assert 'postAction("reject", article.url, "not_interested")' in source
        assert 'e.key === "k"' in source
        assert 'postAction("reject", article.url, "already_known")' in source

    @pytest.mark.skipif(
        shutil.which("node") is None,
        reason="Node.js not installed; the executable UI contract test needs it",
    )
    def test_button_responses_preserve_or_record_verdict(self) -> None:
        node = shutil.which("node")
        app_path = self.static_dir / "app.js"
        harness = r"""
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
const rejectButtons = ["not_interested", "already_known"].map((reason) => {
  const button = new Element();
  button.dataset.reason = reason;
  return button;
});
const tabs = ["pending", "accepted", "rejected", "all"].map((state) => {
  const tab = new Element();
  tab.dataset.state = state;
  return tab;
});

global.document = {
  getElementById: (id) => elements[id],
  createElement: () => new Element(),
  querySelectorAll: (selector) =>
    selector === ".reject-button" ? rejectButtons : tabs,
  addEventListener: () => {},
};
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
  rejectButtons[1].listeners.click();
  await new Promise(setImmediate);

  if (rejectBody.reason !== "already_known") throw new Error("reason not sent");
  if (elements.toast.textContent !== "Error: denied") throw new Error("toast not shown");
  if (elements.toast.classList.contains("hidden")) throw new Error("toast hidden");
  if (elements.deck.children[0] === originalCard) throw new Error("card not re-rendered");
  if (elements.counter.textContent !== "1 remaining") throw new Error("verdict advanced");
  if (!elements["undo-toast"].classList.contains("hidden")) throw new Error("undo recorded");

  rejectOk = true;
  rejectButtons[1].listeners.click();
  await new Promise(setImmediate);
  if (elements["undo-message"].textContent !== "Rejected: 已知道") {
    throw new Error("undo reason not shown");
  }
  if (elements["undo-toast"].classList.contains("hidden")) {
    throw new Error("undo not shown");
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
        subprocess.run([node, "-e", harness, str(app_path)], check=True)
