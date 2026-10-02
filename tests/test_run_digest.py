"""Direct tests for the run_digest use case."""

import asyncio
import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from fakes import FakeLLM, make_config

from cyris.adapters.fetch.rss_worker_source import CloudflareRssSource
from cyris.adapters.output.html_digest import FAVICON, HtmlDigestWriter
from cyris.adapters.store import ArticleStore
from cyris.bootstrap import Deps
from cyris.config import (
    AgentVaultConfig,
)
from cyris.domain.models import (
    NO_LLM_MODEL,
    Article,
    ArticleState,
    DigestContent,
    DigestItem,
    DigestSection,
    StoredArticle,
    Tier,
    UsageStats,
)
from cyris.service_layer.run_digest import RunOptions, _render_site, run_digest
from cyris.utils.timezone import now_in_timezone

pytestmark = pytest.mark.integration


class FakeSource:
    """In-memory FetchSource."""

    def __init__(self, articles: list[Article]) -> None:
        self._articles = articles

    async def fetch_articles(self, **kwargs) -> list[Article]:
        return self._articles

    async def health_check(self) -> bool:
        return True


def make_deps(
    tmp_path: Path,
    llm: FakeLLM,
    source: FakeSource,
    *,
    discord_contents: list | None = None,
    discord_degraded: list | None = None,
) -> tuple[Deps, list]:
    html_dir = tmp_path / "html"
    agent_vault = tmp_path / "agent-vault"
    agent_vault.mkdir(parents=True)

    cfg = make_config(agent_vault=AgentVaultConfig(path=agent_vault))

    notifications: list[str] = []

    async def fake_discord(
        webhook_url, content, digest_url="", publish_failed=False, degraded=False
    ):
        notifications.append("discord")
        if discord_contents is not None:
            discord_contents.append(content)
        if discord_degraded is not None:
            discord_degraded.append(degraded)

    deps = Deps(
        cfg=cfg,
        store=ArticleStore(agent_vault),
        llm=llm,
        fetch_sources=[source],
        html_writer=HtmlDigestWriter(html_dir),
        publish=None,
        sync_promotions=None,
        log_usage=lambda content: None,
        send_discord=fake_discord,
    )
    return deps, notifications


async def test_run_digest_happy_path(tmp_path: Path) -> None:
    article = Article(
        id=1,
        title="Enterprise AI Adoption",
        url="https://example.com/ai",
        content="Enterprises accelerate AI adoption.",
        published_at=datetime.now(UTC) - timedelta(hours=1),
        source_name="TechSource",
        source_tier=Tier.SUMMARIZE,
        source_tags=["tech"],
    )
    # Call order: score batch, then summarize (no news, empty filter pool)
    llm = FakeLLM(
        [
            json.dumps({"scores": [{"id": 1, "score": 85, "language": "en"}]}),
            json.dumps(
                {
                    "sections": [
                        {
                            "heading": "AI 趨勢",
                            "summary": "企業加速導入 AI",
                            "articles": [
                                {
                                    "id": "0",
                                    "title": "Enterprise AI Adoption",
                                    "source": "TechSource",
                                }
                            ],
                        }
                    ]
                }
            ),
        ]
    )
    source = FakeSource([article])
    deps, notifications = make_deps(tmp_path, llm, source)

    # No clock mocking: articles saved within this run must be picked up by
    # the same run's reload (regression test for the exclusive end bound).
    report = await run_digest(deps, RunOptions())

    assert report.status == "ok"
    assert report.html_path is not None and report.html_path.exists()
    assert "Enterprise AI Adoption" in report.html_path.read_text()

    # Article saved, scored, and accepted
    stored = deps.store.get_by_urls(["https://example.com/ai"])
    assert stored[0].state == ArticleState.ACCEPTED
    assert stored[0].score == 85.0


async def test_run_digest_no_articles(tmp_path: Path) -> None:
    source = FakeSource([])
    deps, notifications = make_deps(tmp_path, FakeLLM(), source)

    report = await run_digest(deps, RunOptions())

    assert report.status == "no_articles"
    assert report.html_path is None


async def test_run_digest_dry_run_renders_without_writing(tmp_path: Path) -> None:
    article = Article(
        id=2,
        title="Cloud Security",
        url="https://example.com/cloud",
        content="Cloud security update.",
        published_at=datetime.now(UTC) - timedelta(hours=1),
        source_name="TechSource",
        source_tier=Tier.SUMMARIZE,
        source_tags=["tech"],
    )
    llm = FakeLLM(
        [
            json.dumps({"scores": [{"id": 2, "score": 60, "language": "en"}]}),
            json.dumps({"sections": []}),
        ]
    )
    source = FakeSource([article])
    deps, _ = make_deps(tmp_path, llm, source)

    # Dry run skips saving, so it only previews articles already in the store
    deps.store.save([article])

    report = await run_digest(deps, RunOptions(dry_run=True))

    assert report.status == "ok"
    assert report.rendered is not None
    assert deps.store.get_by_urls(["https://example.com/cloud"])[0].state == ArticleState.PENDING


async def test_publish_outcome_reaches_discord(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A dead publish must announce itself; silently dropping the link is what
    made the missing 2026-08-18/2026-08-20 digest links look like normal runs."""

    def _llm() -> FakeLLM:
        return FakeLLM(
            [
                json.dumps({"scores": [{"id": 3, "score": 85, "language": "en"}]}),
                json.dumps(
                    {
                        "sections": [
                            {
                                "heading": "AI 趨勢",
                                "summary": "企業加速導入 AI",
                                "articles": [
                                    {"id": 3, "title": "Publish Path", "source": "TechSource"}
                                ],
                            }
                        ]
                    }
                ),
            ]
        )

    def _article() -> Article:
        return Article(
            id=3,
            title="Publish Path",
            url="https://example.com/publish",
            content="Enterprises accelerate AI adoption.",
            published_at=datetime.now(UTC) - timedelta(hours=1),
            source_name="TechSource",
            source_tier=Tier.SUMMARIZE,
            source_tags=["tech"],
        )

    class StubHtmlWriter:
        # A name no other code spells, so the digest URL can only come from here.
        @staticmethod
        def digest_filename(date: str, period: str) -> str:
            return f"{date}-{period}-v2.html"

        def write(self, content, raw_page: bool = False, degraded: bool = False) -> Path:
            path = tmp_path / self.digest_filename(content.date, content.period)
            path.write_text("<html></html>")
            return path

    async def run_with(
        publish_ok: bool, run_dir: Path, custom_domain: str = "", worker_domains=None
    ) -> dict:
        deps, _ = make_deps(run_dir, _llm(), FakeSource([_article()]))
        deps.cfg.app.promote.pages_project = "cyris-digest"
        deps.cfg.app.promote.custom_domain = custom_domain
        sent: dict = {}

        async def capture(
            webhook_url, content, digest_url="", publish_failed=False, degraded=False
        ):
            sent["digest_url"] = digest_url
            sent["publish_failed"] = publish_failed

        deps = replace(
            deps,
            html_writer=StubHtmlWriter(),
            publish=lambda _slug: publish_ok,
            send_discord=capture,
            worker_domains=worker_domains,
        )
        await run_digest(deps, RunOptions())
        return sent

    failed = await run_with(False, tmp_path / "failed")
    assert failed == {"digest_url": "", "publish_failed": True}

    ok = await run_with(True, tmp_path / "ok")
    assert ok["publish_failed"] is False
    assert ok["digest_url"].startswith("https://cyris-digest.pages.dev/")
    assert ok["digest_url"].endswith("-v2")

    def never_asked() -> list[str]:
        raise AssertionError("an explicit custom domain needs no lookup")

    domain = await run_with(
        True, tmp_path / "domain", custom_domain="digest.example.org", worker_domains=never_asked
    )
    assert domain["digest_url"].startswith("https://digest.example.org/")
    assert domain["digest_url"].endswith("-v2")

    detected = await run_with(
        True, tmp_path / "detected", worker_domains=lambda: ["a.example.org", "b.example.org"]
    )
    assert detected["digest_url"].startswith("https://a.example.org/")

    none = await run_with(True, tmp_path / "none", worker_domains=lambda: [])
    assert none["digest_url"].startswith("https://cyris-digest.pages.dev/")

    # The fallback is right, but a silent one hides a token missing its permission.
    def denied() -> list[str]:
        raise RuntimeError("Authentication error")

    with caplog.at_level("WARNING"):
        failed_lookup = await run_with(True, tmp_path / "denied", worker_domains=denied)
    assert failed_lookup["digest_url"].startswith("https://cyris-digest.pages.dev/")
    assert "Authentication error" in caplog.text


def _fan_article(*, article_id: int, title: str, url: str) -> Article:
    return Article(
        id=article_id,
        title=title,
        url=url,
        content="Newsletter body.",
        published_at=datetime.now(UTC) - timedelta(hours=1),
        source_name="NL",
        source_tier=Tier.FAN,
        source_tags=[],
    )


def _with_progress(deps: Deps) -> tuple[Deps, list[str]]:
    messages: list[str] = []
    return replace(deps, on_progress=messages.append), messages


async def test_run_digest_empty_content_has_no_dead_link_progress(tmp_path: Path) -> None:
    source = FakeSource([])
    deps, _ = make_deps(tmp_path, FakeLLM(), source)
    deps, messages = _with_progress(deps)

    report = await run_digest(deps, RunOptions())

    assert report.status == "no_articles"
    assert not any("dead" in m.lower() for m in messages)


async def test_run_digest_reports_dead_link_count(tmp_path: Path) -> None:
    source = FakeSource(
        [
            _fan_article(article_id=1, title="Dead NL", url="newsletter:x"),
            _fan_article(article_id=2, title="Live", url="https://a.com/1"),
        ]
    )
    contents: list = []
    deps, _ = make_deps(tmp_path, FakeLLM(), source, discord_contents=contents)
    deps, messages = _with_progress(deps)

    report = await run_digest(deps, RunOptions())

    assert report.status == "ok"
    assert any("1" in m for m in messages)
    assert contents
    assert contents[0].dead_link_count == 1


async def test_run_digest_reports_synthetic_newsletter_url_count(tmp_path: Path) -> None:
    source = FakeSource(
        [
            _fan_article(article_id=1, title="Synth", url="newsletter:abc"),
            _fan_article(article_id=2, title="Live", url="https://example.com/a"),
        ]
    )
    contents: list = []
    deps, _ = make_deps(tmp_path, FakeLLM(), source, discord_contents=contents)
    deps, messages = _with_progress(deps)

    report = await run_digest(deps, RunOptions())

    assert report.status == "ok"
    assert any("1" in m and "newsletter" in m.lower() for m in messages)
    assert contents
    assert contents[0].synthetic_url_count == 1


async def test_run_digest_omits_synthetic_url_progress_when_all_http(tmp_path: Path) -> None:
    source = FakeSource(
        [
            _fan_article(article_id=1, title="A", url="https://example.com/a"),
            _fan_article(article_id=2, title="B", url="http://example.com/b"),
        ]
    )
    contents: list = []
    deps, _ = make_deps(tmp_path, FakeLLM(), source, discord_contents=contents)
    deps, messages = _with_progress(deps)

    report = await run_digest(deps, RunOptions())

    assert report.status == "ok"
    assert not any("newsletter" in m.lower() for m in messages)
    assert contents
    assert contents[0].synthetic_url_count == 0


async def test_scoring_tag_write_failure_does_not_stop_the_run(tmp_path: Path) -> None:
    """Scoring tags are saved once after all batches; that write failing costs only the tags."""
    articles = [
        Article(
            id=i,
            title=f"Article {i}",
            url=f"https://example.com/{i}",
            content="Content",
            published_at=datetime.now(UTC) - timedelta(hours=1),
            source_name="Source",
            source_tier=Tier.FILTER,
        )
        for i in range(1, 22)
    ]
    first_scores = [
        {"id": i, "score": 80, "language": "en", "tags": ["First"]} for i in range(1, 21)
    ]
    llm = FakeLLM(
        [
            json.dumps({"scores": first_scores}),
            json.dumps(
                {
                    "scores": [
                        {
                            "id": 21,
                            "score": 81,
                            "language": "en",
                            "tags": ["Second"],
                        }
                    ]
                }
            ),
            json.dumps({"selected": []}),
        ]
    )
    deps, _ = make_deps(tmp_path, llm, FakeSource(articles))

    class ExplodingTagStore:
        def __init__(self) -> None:
            self.calls = 0
            self.seen: dict[str, list[str]] = {}

        def save(self, url_to_tags) -> None:
            self.calls += 1
            self.seen = dict(url_to_tags)
            raise RuntimeError("D1 unavailable")

    tag_store = ExplodingTagStore()
    deps = replace(deps, tag_store=tag_store)

    report = await run_digest(deps, RunOptions())

    assert report.status == "ok"
    # One accumulated write for both scoring batches, not one per batch.
    assert tag_store.calls == 1
    assert tag_store.seen["https://example.com/1"] == ["first"]
    assert tag_store.seen["https://example.com/21"] == ["second"]
    # Both batches' scores persisted despite the failed tag write.
    stored = deps.store.get_by_urls(["https://example.com/21"])
    assert stored[0].score == 81


async def test_story_store_failure_does_not_stop_the_run(tmp_path: Path) -> None:
    articles = [
        Article(
            id=i,
            title=f"News {i}",
            url=f"https://news.example/{i}",
            content="News content",
            published_at=datetime.now(UTC) - timedelta(hours=1),
            source_name="Wire",
            source_tier=Tier.FILTER,
            source_tags=["news"],
        )
        for i in (1, 2)
    ]
    # News skips scoring; the single call is the cluster response, and the
    # emptied filter pool never reaches the LLM.
    llm = FakeLLM(
        json.dumps(
            {"clusters": [{"heading": "H", "summary": "S", "article_ids": [1, 2], "tags": []}]}
        )
    )
    deps, _ = make_deps(tmp_path, llm, FakeSource(articles))

    class ExplodingStoryStore:
        def __init__(self) -> None:
            self.calls = 0

        def save(self, digest_date, period, records) -> None:
            self.calls += 1
            raise RuntimeError("D1 unavailable")

    story_store = ExplodingStoryStore()
    deps = replace(deps, story_store=story_store)

    report = await run_digest(deps, RunOptions())

    assert story_store.calls == 1  # the write was attempted, not skipped
    assert report.status == "ok"
    assert report.html_path is not None and report.html_path.exists()


def _fresh_and_earlier_deps(tmp_path: Path) -> Deps:
    """One article fetched this run, plus one the previous run already accepted."""
    fetched = Article(
        id=1,
        title="Fresh This Run",
        url="https://example.com/fresh",
        content="Enterprises accelerate AI adoption.",
        published_at=datetime.now(UTC) - timedelta(hours=1),
        source_name="TechSource",
        source_tier=Tier.SUMMARIZE,
        source_tags=["tech"],
    )
    earlier = fetched.model_copy(
        update={"id": 2, "title": "Judged Last Run", "url": "https://example.com/earlier"}
    )
    llm = FakeLLM(
        [
            json.dumps({"scores": [{"id": 1, "score": 85, "language": "en"}]}),
            json.dumps(
                {
                    "sections": [
                        {
                            "heading": "AI 趨勢",
                            "summary": "企業加速導入 AI",
                            "articles": [
                                {"id": "0", "title": "Fresh This Run", "source": "TechSource"}
                            ],
                        }
                    ]
                }
            ),
        ]
    )
    deps, _ = make_deps(tmp_path, llm, FakeSource([fetched]))
    # The overlapping window still holds a row the previous run accepted.
    deps.store.save([earlier])
    deps.store.update_states({earlier.url: (ArticleState.ACCEPTED, None)}, digest_date="2026-01-01")
    return deps


async def test_raw_page_skips_rows_an_earlier_run_judged(tmp_path: Path) -> None:
    report = await run_digest(_fresh_and_earlier_deps(tmp_path), RunOptions())

    assert report.status == "ok"
    raw = next(report.html_path.parent.glob("*-raw.html")).read_text()
    assert "Fresh This Run" in raw
    assert "Judged Last Run" not in raw


async def test_the_local_archive_links_this_runs_raw_page(tmp_path: Path) -> None:
    report = await run_digest(_fresh_and_earlier_deps(tmp_path), RunOptions())

    assert report.status == "ok"
    index = (report.html_path.parent / "index.html").read_text()
    assert '-raw.html">All articles</a>' in index


@pytest.mark.parametrize("raw_written", [True, False])
async def test_the_local_digest_links_all_articles_only_when_its_raw_page_was_written(
    tmp_path: Path, raw_written: bool
) -> None:
    deps = _fresh_and_earlier_deps(tmp_path)
    if not raw_written:

        def refuse(*_args):
            raise OSError("disk full")

        deps.html_writer.write_raw = refuse

    report = await run_digest(deps, RunOptions())

    digest = report.html_path.read_text()
    assert ('-raw.html">All articles</a>' in digest) is raw_written


def _stored_article() -> StoredArticle:
    now = datetime.now(UTC)
    return StoredArticle(
        url="https://example.com/collected",
        original_id="collected",
        title="Collected",
        content="",
        published_at=now,
        source_name="Src",
        source_tier=Tier.FILTER,
        state=ArticleState.PENDING,
        first_seen_at=now,
    )


@pytest.mark.parametrize(("collected", "linked"), [(True, True), (False, False)])
def test_the_published_archive_links_this_runs_raw_page_when_there_is_one(
    tmp_path: Path, collected: bool, linked: bool
) -> None:
    deps = SimpleNamespace(
        html_writer=HtmlDigestWriter(tmp_path),
        site_filenames=lambda: [],
        archive_counts=lambda: {},
    )
    content = DigestContent(
        date="2026-04-15",
        period="evening",
        sources_processed=1,
        articles_received=1,
        articles_included=0,
        usage=UsageStats(),
    )

    pages = _render_site(
        deps, content, [_stored_article()] if collected else [], raw_page=collected, degraded=False
    )

    index = pages["/index.html"].decode("utf-8")
    assert ('href="2026-04-15-evening-raw.html">All articles</a>' in index) is linked
    assert (">All articles</a>" in index) is linked
    digest = pages["/2026-04-15-evening.html"].decode("utf-8")
    assert ('<a href="2026-04-15-evening-raw.html">All articles</a>' in digest) is linked
    assert (">All articles</a>" in digest) is linked


def _published_index(deps, content: DigestContent) -> str:
    return _render_site(deps, content, [], raw_page=False, degraded=False)["/index.html"].decode(
        "utf-8"
    )


def _run_content(date: str, period: str, lead: str, included: int) -> DigestContent:
    return DigestContent(
        date=date,
        period=period,
        sources_processed=1,
        articles_received=included,
        articles_included=included,
        usage=UsageStats(),
        featured_articles=[
            DigestSection(
                heading="F",
                items=[
                    DigestItem(title=lead, summary="S", sources=["Src"], urls=["https://x.test"])
                ],
            )
        ],
    )


def test_the_published_archive_leads_with_this_runs_issue(tmp_path: Path) -> None:
    deps = SimpleNamespace(
        html_writer=HtmlDigestWriter(tmp_path),
        site_filenames=lambda: ["2026-04-15-evening.html"],
        archive_counts=lambda: {},
    )

    index = _published_index(deps, _run_content("2026-04-16", "evening", "Run lead", 7))

    card = index[index.index('<article class="front-card">') :].split("</article>", 1)[0]
    assert '<span class="date">2026-04-16</span>' in card
    assert '<h2 lang="">Run lead</h2>' in card
    assert '<span class="data">7 articles</span>' in card
    panels = index[index.index('<section class="panel">') :]
    assert 'href="2026-04-15-evening.html"' in panels


def test_the_published_digest_is_keyed_by_the_writers_file_name(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        HtmlDigestWriter, "digest_filename", staticmethod(lambda d, p: f"{d}-{p}-v2.html")
    )
    deps = SimpleNamespace(
        html_writer=HtmlDigestWriter(tmp_path),
        site_filenames=lambda: [],
        archive_counts=lambda: {},
    )

    pages = _render_site(
        deps, _run_content("2026-04-16", "evening", "Lead", 1), [], raw_page=False, degraded=False
    )

    assert "/2026-04-16-evening-v2.html" in pages
    assert "/2026-04-16-evening.html" not in pages


def test_every_published_site_carries_the_favicon(tmp_path: Path) -> None:
    deps = SimpleNamespace(
        html_writer=HtmlDigestWriter(tmp_path),
        site_filenames=lambda: ["2026-04-15-evening.html", "favicon.svg"],
        archive_counts=lambda: {},
    )

    pages = _render_site(
        deps, _run_content("2026-04-16", "evening", "Lead", 1), [], raw_page=False, degraded=False
    )

    assert pages["/favicon.svg"] == FAVICON.read_bytes()


def test_the_published_archive_rows_carry_the_recorded_counts(tmp_path: Path) -> None:
    deps = SimpleNamespace(
        html_writer=HtmlDigestWriter(tmp_path),
        site_filenames=lambda: ["2026-04-15-evening.html"],
        archive_counts=lambda: {("2026-04-15", "evening"): 12},
    )

    index = _published_index(deps, _run_content("2026-04-16", "morning", "Lead", 3))

    row = index[index.index('<div class="list-row archive-row">') :].split("</div>", 1)[0]
    assert "2026-04-15" in row
    assert '<span class="small">12 articles</span>' in row


def test_a_failed_count_read_still_publishes_every_issue(tmp_path: Path, caplog) -> None:
    from cyris.adapters.output.publish import _parse_archive_anchors

    def down():
        raise RuntimeError("d1 down")

    deps = SimpleNamespace(
        html_writer=HtmlDigestWriter(tmp_path),
        site_filenames=lambda: ["2026-04-15-evening.html"],
        archive_counts=down,
    )

    with caplog.at_level("ERROR"):
        index = _published_index(deps, _run_content("2026-04-16", "morning", "Lead", 3))

    assert _parse_archive_anchors(index) == {"/2026-04-15-evening.html", "/2026-04-16-morning.html"}
    rows = re.findall(r'<div class="list-row archive-row[^"]*">.*?</div>', index, re.S)
    assert rows
    assert [row for row in rows if 'class="small"' in row] == []
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert "d1 down" in errors[0].getMessage()


def _one_article_source() -> FakeSource:
    return FakeSource(
        [
            Article(
                id=1,
                title="Enterprise AI Adoption",
                url="https://example.com/ai",
                content="Enterprises accelerate AI adoption.",
                published_at=datetime.now(UTC) - timedelta(hours=1),
                source_name="TechSource",
                source_tier=Tier.SUMMARIZE,
                source_tags=["tech"],
            )
        ]
    )


async def test_run_digest_warns_when_a_chosen_provider_builds_no_client(tmp_path: Path) -> None:
    """A provider whose key is missing publishes excerpts; say so.

    The digest goes out, looks thin, and nothing else reports a cause.
    """
    deps, _ = make_deps(tmp_path, llm=None, source=_one_article_source())
    cfg = make_config(
        agent_vault=deps.cfg.app.agent_vault,
        llm_provider={"provider": "gemini", "model": "gemini-3.8-flash"},
    )
    messages: list[str] = []
    deps = replace(deps, cfg=cfg, on_progress=messages.append)

    await run_digest(deps, RunOptions(period="morning"))

    assert any(m.startswith("WARNING: no LLM client for gemini") for m in messages), messages


async def test_a_provider_none_run_reports_plain_excerpts_without_a_warning(
    tmp_path: Path,
) -> None:
    """Provider none is a choice: report it, never as something to fix."""
    deps, _ = make_deps(tmp_path, llm=None, source=_one_article_source())
    assert deps.cfg.app.llm_provider.provider == "none"
    messages: list[str] = []
    deps = replace(deps, on_progress=messages.append)

    await run_digest(deps, RunOptions(period="morning"))

    assert not [m for m in messages if "WARNING" in m], messages
    assert any("provider none" in m and "plain excerpts" in m for m in messages), messages


def _run_summary(caplog) -> dict:
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("run_summary ")]
    assert len(lines) == 1, f"expected one run_summary line, got {len(lines)}"
    return json.loads(lines[0].removeprefix("run_summary "))


async def test_every_run_leaves_one_summary_line(tmp_path: Path, caplog) -> None:
    """The seven-day record: Workers Logs keeps the container's stdout, nothing else does."""
    source = FakeSource([])
    deps, _ = make_deps(tmp_path, FakeLLM(), source)

    with caplog.at_level("INFO", logger="cyris.service_layer.run_digest"):
        await run_digest(deps, RunOptions())

    summary = _run_summary(caplog)
    assert summary["status"] == "no_articles"
    assert summary["fetched"] == 0
    assert "wall_seconds" in summary


async def test_a_run_that_raises_still_says_so(tmp_path: Path, caplog) -> None:
    """A crashed run is the one whose numbers are worth reading.

    `status` starts at "error" and every returning path overwrites it, so the
    exception path needs no handler of its own — which is what keeps a crash
    visible in the same query as a successful run. A failing *source* is not the
    way in: `fetch_all_articles` turns that into `failed_sources` on purpose.
    """
    article = Article(
        id=1,
        title="T",
        url="https://example.com/t",
        content="c",
        published_at=datetime.now(UTC) - timedelta(hours=1),
        source_name="TechSource",
        source_tier=Tier.SUMMARIZE,
    )
    deps, _ = make_deps(tmp_path, FakeLLM(), FakeSource([article]))

    def explode(*args, **kwargs):
        raise RuntimeError("the store is gone")

    deps.store.save = explode

    with (
        caplog.at_level("INFO", logger="cyris.service_layer.run_digest"),
        pytest.raises(RuntimeError),
    ):
        await run_digest(deps, RunOptions())

    summary = _run_summary(caplog)
    assert summary["status"] == "error"
    assert summary["fetched"] == 1


def _notify_llm(**kwargs) -> FakeLLM:
    return FakeLLM(
        [
            json.dumps({"scores": [{"id": 7, "score": 85, "language": "en"}]}),
            json.dumps(
                {
                    "sections": [
                        {
                            "heading": "AI 趨勢",
                            "summary": "企業加速導入 AI",
                            "articles": [
                                {"id": 7, "title": "Notify Path", "source": "NotifySource"}
                            ],
                        }
                    ]
                }
            ),
        ],
        **kwargs,
    )


def _notify_article() -> Article:
    return Article(
        id=7,
        title="Notify Path",
        url="https://example.com/notify",
        content="Enterprises accelerate AI adoption.",
        published_at=datetime.now(UTC) - timedelta(hours=1),
        source_name="NotifySource",
        source_tier=Tier.SUMMARIZE,
        source_tags=["tech"],
    )


def _skip_records(caplog) -> list:
    return [r for r in caplog.records if "Discord webhook" in r.getMessage()]


async def test_notification_marks_a_quota_exhausted_configured_llm_as_degraded(
    tmp_path: Path,
) -> None:
    contents: list = []
    degraded: list = []
    deps, _ = make_deps(
        tmp_path,
        FakeLLM(error=RuntimeError("quota"), model="test-model"),
        FakeSource([_notify_article()]),
        discord_contents=contents,
        discord_degraded=degraded,
    )
    deps.cfg.app.notify.discord_webhook_url = "https://discord.com/api/webhooks/1/quota"
    deps.cfg.app.llm_provider.provider = "gemini"
    deps.cfg.app.llm_provider.model = ""

    await run_digest(deps, RunOptions())

    assert contents[0].usage.model == "test-model"
    assert contents[0].usage.input_tokens == 0
    assert degraded == [True]


async def test_notification_trusts_an_llm_that_answered_with_zero_tokens(tmp_path: Path) -> None:
    degraded: list = []
    deps, _ = make_deps(
        tmp_path,
        _notify_llm(input_tokens=0, model="test-model"),
        FakeSource([_notify_article()]),
        discord_degraded=degraded,
    )
    deps.cfg.app.notify.discord_webhook_url = "https://discord.com/api/webhooks/1/zero"
    deps.cfg.app.llm_provider.provider = "gemini"
    deps.cfg.app.llm_provider.model = ""

    await run_digest(deps, RunOptions())

    assert degraded == [False]


async def test_notification_keeps_actual_llm_model_when_config_model_is_empty(
    tmp_path: Path,
) -> None:
    contents: list = []
    degraded: list = []
    deps, _ = make_deps(
        tmp_path,
        _notify_llm(model="test-model"),
        FakeSource([_notify_article()]),
        discord_contents=contents,
        discord_degraded=degraded,
    )
    deps.cfg.app.notify.discord_webhook_url = "https://discord.com/api/webhooks/1/used"
    deps.cfg.app.llm_provider.provider = "gemini"
    deps.cfg.app.llm_provider.model = ""

    await run_digest(deps, RunOptions())

    assert contents[0].usage.model == "test-model"
    assert degraded == [False]


async def test_notification_marks_no_llm_as_not_degraded(tmp_path: Path) -> None:
    contents: list = []
    degraded: list = []
    deps, _ = make_deps(
        tmp_path,
        llm=None,
        source=FakeSource([_notify_article()]),
        discord_contents=contents,
        discord_degraded=degraded,
    )
    deps.cfg.app.notify.discord_webhook_url = "https://discord.com/api/webhooks/1/no-llm"
    assert deps.cfg.app.llm_provider.chooses_no_llm

    await run_digest(deps, RunOptions())

    assert contents[0].usage.model == NO_LLM_MODEL
    assert degraded == [False]


async def test_a_run_with_no_webhook_says_it_is_skipping_the_notification(
    tmp_path: Path, caplog, monkeypatch
) -> None:
    deps, _ = make_deps(tmp_path, _notify_llm(), FakeSource([_notify_article()]))
    assert deps.cfg.app.notify.discord_webhook_url == ""

    with caplog.at_level("INFO", logger="cyris.service_layer.run_digest"):
        await run_digest(deps, RunOptions())

    records = _skip_records(caplog)
    assert len(records) == 1
    assert "skipping" in records[0].getMessage()
    assert records[0].levelname == "INFO"


async def test_a_run_with_a_webhook_stays_quiet_and_still_notifies(
    tmp_path: Path, caplog, monkeypatch
) -> None:
    deps, _ = make_deps(tmp_path, _notify_llm(), FakeSource([_notify_article()]))
    deps.cfg.app.notify.discord_webhook_url = "https://discord.com/api/webhooks/1/run-log"
    sent: dict = {}

    async def capture(webhook_url, content, digest_url="", publish_failed=False, degraded=False):
        sent["webhook_url"] = webhook_url

    deps = replace(deps, send_discord=capture)

    with caplog.at_level("INFO", logger="cyris.service_layer.run_digest"):
        await run_digest(deps, RunOptions())

    assert _skip_records(caplog) == []
    assert sent["webhook_url"] == "https://discord.com/api/webhooks/1/run-log"


async def test_a_run_mails_the_digest_beside_discord(tmp_path: Path) -> None:
    deps, notifications = make_deps(tmp_path, _notify_llm(), FakeSource([_notify_article()]))
    deps.cfg.app.notify.email_to = "me@example.org"
    deps.cfg.app.notify.email_from = "digest@example.org"
    mailed: list[dict] = []

    async def capture(
        recipient,
        sender,
        content,
        digest_url="",
        publish_failed=False,
        raw_page=False,
        degraded=False,
    ):
        mailed.append({"recipient": recipient, "sender": sender, "digest_url": digest_url})

    deps = replace(deps, send_email=capture)
    await run_digest(deps, RunOptions())

    assert notifications == ["discord"]
    assert mailed == [
        {"recipient": "me@example.org", "sender": "digest@example.org", "digest_url": ""}
    ]


async def test_a_dry_run_mails_like_it_notifies_discord(tmp_path: Path) -> None:
    deps, notifications = make_deps(tmp_path, _notify_llm(), FakeSource([_notify_article()]))
    deps.cfg.app.notify.email_to = "me@example.org"
    deps.cfg.app.notify.email_from = "digest@example.org"
    mailed: list[bool] = []

    async def capture(
        recipient,
        sender,
        content,
        digest_url="",
        publish_failed=False,
        raw_page=False,
        degraded=False,
    ):
        mailed.append(raw_page)

    deps = replace(deps, send_email=capture)
    # A preview saves nothing it fetches, so it digests only what is already pending.
    deps.store.save([_notify_article()])
    await run_digest(deps, RunOptions(dry_run=True))

    assert notifications == ["discord"]
    assert mailed == [False]


async def test_a_run_with_no_email_address_mails_nothing_and_says_so(
    tmp_path: Path, caplog
) -> None:
    deps, _ = make_deps(tmp_path, _notify_llm(), FakeSource([_notify_article()]))
    assert deps.cfg.app.notify.email_to == ""

    async def never(*args, **kwargs):
        raise AssertionError("no address, no mail")

    deps = replace(deps, send_email=never)
    with caplog.at_level("INFO", logger="cyris.service_layer.run_digest"):
        await run_digest(deps, RunOptions())

    assert "No email address configured" in caplog.text


async def test_an_address_without_a_sender_to_mail_it_is_a_warning(tmp_path: Path, caplog) -> None:
    deps, _ = make_deps(tmp_path, _notify_llm(), FakeSource([_notify_article()]))
    deps.cfg.app.notify.email_to = "me@example.org"
    deps.cfg.app.notify.email_from = "digest@example.org"
    assert deps.send_email is None

    with caplog.at_level("WARNING", logger="cyris.service_layer.run_digest"):
        report = await run_digest(deps, RunOptions())

    assert report.status == "ok"
    assert "CLOUDFLARE_ACCOUNT_ID" in caplog.text


def _recording(deps: Deps) -> tuple[Deps, list[dict]]:
    recorded: list[dict] = []
    return replace(deps, record_run=recorded.append), recorded


async def test_an_empty_window_hands_its_summary_to_the_recorder(tmp_path: Path) -> None:
    deps, _ = make_deps(tmp_path, FakeLLM(), FakeSource([]))
    deps, recorded = _recording(deps)

    await run_digest(deps, RunOptions())

    [summary] = recorded
    assert summary["status"] == "no_articles"
    assert summary["fetched"] == 0
    assert "wall_seconds" in summary


async def test_a_run_with_nothing_pending_is_recorded(tmp_path: Path) -> None:
    # A preview saves nothing, so an article the store never held is not pending.
    deps, _ = make_deps(tmp_path, FakeLLM(), FakeSource([_notify_article()]))
    deps, recorded = _recording(deps)

    await run_digest(deps, RunOptions(dry_run=True))

    [summary] = recorded
    assert summary["status"] == "no_pending"


async def test_a_finished_digest_is_recorded(tmp_path: Path) -> None:
    deps, _ = make_deps(tmp_path, _notify_llm(), FakeSource([_notify_article()]))
    deps, recorded = _recording(deps)

    await run_digest(deps, RunOptions())

    [summary] = recorded
    assert summary["status"] == "ok"


def _exploding_store(deps: Deps) -> None:
    def explode(*args, **kwargs):
        raise RuntimeError("the store is gone")

    deps.store.save = explode


async def test_a_run_that_raises_is_recorded_as_an_error(tmp_path: Path) -> None:
    deps, _ = make_deps(tmp_path, FakeLLM(), FakeSource([_notify_article()]))
    deps, recorded = _recording(deps)
    _exploding_store(deps)

    with pytest.raises(RuntimeError):
        await run_digest(deps, RunOptions())

    [summary] = recorded
    assert summary["status"] == "error"
    assert summary["fetched"] == 1


async def test_a_run_stopped_by_sigterm_is_recorded_as_an_error(tmp_path: Path) -> None:
    # `cyris run` answers SIGTERM by cancelling the task, and CancelledError is not
    # an Exception: only a `finally` still records the run it cut short.
    fetching = asyncio.Event()

    class HangingSource(FakeSource):
        async def fetch_articles(self, **kwargs) -> list[Article]:
            fetching.set()
            await asyncio.Event().wait()
            return []

    deps, _ = make_deps(tmp_path, FakeLLM(), HangingSource([]))
    deps, recorded = _recording(deps)
    task = asyncio.create_task(run_digest(deps, RunOptions()))
    await fetching.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    [summary] = recorded
    assert summary["status"] == "error"
    assert "wall_seconds" in summary


async def test_a_sigterm_while_the_run_is_recorded_keeps_the_record(tmp_path: Path) -> None:
    deps, _ = make_deps(tmp_path, FakeLLM(), FakeSource([_notify_article()]))
    recorded: list[dict] = []

    def record_run(summary: dict) -> None:
        asyncio.current_task().cancel()
        recorded.append(summary)

    deps = replace(deps, record_run=record_run)
    task = asyncio.create_task(run_digest(deps, RunOptions()))

    with pytest.raises(asyncio.CancelledError):
        await task

    [summary] = recorded
    assert "wall_seconds" in summary


async def test_a_run_that_dies_before_fetching_is_recorded(tmp_path: Path, monkeypatch) -> None:
    async def refuse(**kwargs):
        raise RuntimeError("no network")

    monkeypatch.setattr("cyris.service_layer.run_digest.fetch_all_articles", refuse)
    deps, _ = make_deps(tmp_path, FakeLLM(), FakeSource([]))
    deps, recorded = _recording(deps)

    with pytest.raises(RuntimeError):
        await run_digest(deps, RunOptions())

    [summary] = recorded
    assert summary["status"] == "error"
    assert "fetched" not in summary


async def test_a_preview_is_recorded_as_one(tmp_path: Path) -> None:
    deps, _ = make_deps(tmp_path, FakeLLM(), FakeSource([]))
    deps, recorded = _recording(deps)

    await run_digest(deps, RunOptions(dry_run=True))

    [summary] = recorded
    assert summary["dry_run"] is True


async def test_no_recorder_is_no_error(tmp_path: Path) -> None:
    deps, _ = make_deps(tmp_path, FakeLLM(), FakeSource([]))

    report = await run_digest(deps, RunOptions())

    assert report.status == "no_articles"


async def test_an_empty_window_leaves_a_d1_row(tmp_path: Path) -> None:
    from fakes import SqliteD1

    from cyris.adapters.store.runs import D1RunLog

    db = SqliteD1()
    deps, _ = make_deps(tmp_path, FakeLLM(), FakeSource([]))
    deps = replace(deps, record_run=D1RunLog(db, "sha1").record)

    await run_digest(deps, RunOptions())

    [row] = db.query("SELECT status, build_sha FROM digest_runs").rows
    assert row == {"status": "no_articles", "build_sha": "sha1"}


def _down(_summary: dict) -> None:
    from cyris.adapters.store.d1 import D1Error

    raise D1Error("down")


async def test_a_failed_run_row_write_leaves_the_result_and_the_log_line(
    tmp_path: Path, caplog
) -> None:
    deps, _ = make_deps(tmp_path, FakeLLM(), FakeSource([]))
    deps = replace(deps, record_run=_down)

    with caplog.at_level("INFO", logger="cyris.service_layer.run_digest"):
        report = await run_digest(deps, RunOptions())

    assert report.status == "no_articles"
    assert _run_summary(caplog)["status"] == "no_articles"


async def test_a_failed_run_row_write_does_not_replace_the_runs_exception(
    tmp_path: Path,
) -> None:
    deps, _ = make_deps(tmp_path, FakeLLM(), FakeSource([_notify_article()]))
    deps = replace(deps, record_run=_down)
    _exploding_store(deps)

    with pytest.raises(RuntimeError, match="the store is gone"):
        await run_digest(deps, RunOptions())


async def test_a_failed_run_row_write_is_logged_as_an_error(tmp_path: Path, caplog) -> None:
    deps, _ = make_deps(tmp_path, FakeLLM(), FakeSource([]))
    deps = replace(deps, record_run=_down)

    with caplog.at_level("INFO", logger="cyris.service_layer.run_digest"):
        await run_digest(deps, RunOptions())

    assert any(
        r.levelname == "ERROR" and r.name == "cyris.service_layer.run_digest"
        for r in caplog.records
    )


async def test_a_zero_token_run_that_was_answered_records_what_discord_shows(
    tmp_path: Path,
) -> None:
    degraded: list = []
    deps, _ = make_deps(
        tmp_path,
        _notify_llm(input_tokens=0, model="test-model"),
        FakeSource([_notify_article()]),
        discord_degraded=degraded,
    )
    deps, recorded = _recording(deps)
    deps.cfg.app.notify.discord_webhook_url = "https://discord.com/api/webhooks/1/zero"
    deps.cfg.app.llm_provider.provider = "gemini"
    deps.cfg.app.llm_provider.model = ""

    await run_digest(deps, RunOptions())

    assert recorded[0]["degraded"] is False
    assert degraded == [False]


async def test_a_run_that_used_its_llm_records_not_degraded(tmp_path: Path) -> None:
    degraded: list = []
    deps, _ = make_deps(
        tmp_path,
        _notify_llm(model="test-model"),
        FakeSource([_notify_article()]),
        discord_degraded=degraded,
    )
    deps, recorded = _recording(deps)
    deps.cfg.app.notify.discord_webhook_url = "https://discord.com/api/webhooks/1/used"
    deps.cfg.app.llm_provider.provider = "gemini"
    deps.cfg.app.llm_provider.model = ""

    await run_digest(deps, RunOptions())

    assert recorded[0]["degraded"] is False
    assert degraded == [False]


async def test_a_run_with_no_llm_records_not_degraded(tmp_path: Path) -> None:
    deps, _ = make_deps(tmp_path, llm=None, source=FakeSource([_notify_article()]))
    deps, recorded = _recording(deps)
    deps.cfg.app.notify.discord_webhook_url = "https://discord.com/api/webhooks/1/no-llm"

    await run_digest(deps, RunOptions())

    assert recorded[0]["degraded"] is False


async def test_a_run_without_content_records_no_degraded_verdict(tmp_path: Path) -> None:
    deps, _ = make_deps(tmp_path, FakeLLM(), FakeSource([]))
    deps, recorded = _recording(deps)

    await run_digest(deps, RunOptions())

    assert "degraded" not in recorded[0]


def _second_notify_article() -> Article:
    article = _notify_article()
    article.id = 8
    article.url = "https://example.com/notify-2"
    article.title = "Notify Path Two"
    return article


async def test_a_provider_none_run_is_not_degraded(tmp_path: Path) -> None:
    deps, _ = make_deps(
        tmp_path, llm=None, source=FakeSource([_notify_article(), _second_notify_article()])
    )
    deps, recorded = _recording(deps)
    deps.cfg.app.llm_provider.provider = "none"
    deps.cfg.app.llm_provider.model = ""

    await run_digest(deps, RunOptions())

    assert recorded[0]["degraded"] is False


async def test_a_provider_none_run_ignores_a_leftover_model(tmp_path: Path) -> None:
    """A model left behind by the provider before "none" must not name the run's usage."""
    contents: list = []
    degraded: list = []
    deps, _ = make_deps(
        tmp_path,
        llm=None,
        source=FakeSource([_notify_article(), _second_notify_article()]),
        discord_contents=contents,
        discord_degraded=degraded,
    )
    deps, recorded = _recording(deps)
    deps.cfg.app.notify.discord_webhook_url = "https://discord.com/api/webhooks/1/none"
    deps.cfg.app.llm_provider.provider = "none"
    deps.cfg.app.llm_provider.model = "gemini-3.8-flash"

    await run_digest(deps, RunOptions())

    assert contents[0].usage.model == NO_LLM_MODEL
    assert recorded[0]["degraded"] is False
    assert degraded == [False]


def _quiet_window_article() -> Article:
    article = _notify_article()
    article.source_name = "FanSource"
    article.source_tier = Tier.FAN
    return article


class _ScoringFails(FakeLLM):
    """Raises on the first call, which is scoring's, and answers every later one."""

    async def complete(self, prompt: str, **kwargs):
        if not self.calls:
            self.calls.append({"prompt": prompt, **kwargs})
            raise RuntimeError("quota")
        return await super().complete(prompt, **kwargs)


@pytest.mark.parametrize(
    ("llm", "provider", "article", "degraded", "spent"),
    [
        (None, "gemini", _notify_article, True, False),
        (FakeLLM(error=RuntimeError("quota")), "gemini", _notify_article, True, False),
        (_ScoringFails(), "gemini", _notify_article, True, True),
        (FakeLLM(), "gemini", _quiet_window_article, False, False),
        (None, "none", _notify_article, False, False),
    ],
    ids=["missing-key", "call-failed", "scoring-failed", "quiet-window", "provider-none"],
)
async def test_every_channel_agrees_on_whether_a_run_fell_back(
    tmp_path: Path, llm, provider: str, article, degraded: bool, spent: bool
) -> None:
    """A quiet window spends no tokens with a healthy client; only a fallback degrades a run."""
    discord: list = []
    mailed: list = []

    async def mail(recipient, sender, content, **kwargs):
        mailed.append(kwargs["degraded"])

    deps, _ = make_deps(tmp_path, llm=llm, source=FakeSource([article()]), discord_degraded=discord)
    deps, recorded = _recording(replace(deps, send_email=mail))
    deps.cfg.app.llm_provider.provider = provider
    deps.cfg.app.notify.email_to = "me@example.org"

    report = await run_digest(deps, RunOptions())

    assert (recorded[0]["llm"]["input_tokens"] > 0) is spent
    assert recorded[0]["degraded"] is degraded
    assert discord == [degraded]
    assert mailed == [degraded]
    page = report.html_path.read_text(encoding="utf-8")
    assert ('class="notice err issue-note"' in page) is degraded


async def test_a_run_without_a_digest_store_writes_its_pages_as_before(
    tmp_path: Path, caplog
) -> None:
    deps, _ = make_deps(tmp_path, _notify_llm(), FakeSource([_notify_article()]))
    assert deps.digest_store is None

    with caplog.at_level("INFO", logger="cyris.service_layer.run_digest"):
        report = await run_digest(deps, RunOptions())

    assert report.status == "ok"
    assert "digest_store_error" not in _run_summary(caplog)
    assert report.html_path is not None and report.html_path.exists()
    assert list(report.html_path.parent.glob("*-raw.html"))


def _stored_digests(deps: Deps):
    from fakes import SqliteD1

    from cyris.adapters.store.digests import D1DigestStore

    db = SqliteD1()
    store = D1DigestStore(db)
    return replace(deps, digest_store=store), store, db


def _digest_count(db) -> int:
    return db.query("SELECT COUNT(*) AS n FROM digests").rows[0]["n"]


async def test_the_stored_digest_is_the_final_content(tmp_path: Path, caplog) -> None:
    source = FakeSource(
        [_notify_article(), _fan_article(article_id=8, title="Synth", url="newsletter:abc")]
    )
    deps, _ = make_deps(tmp_path, _notify_llm(), source)
    deps, store, db = _stored_digests(deps)

    with caplog.at_level("INFO", logger="cyris.service_layer.run_digest"):
        report = await run_digest(deps, RunOptions())

    assert report.status == "ok"
    assert _digest_count(db) == 1
    [row] = db.query("SELECT date, period FROM digests").rows
    loaded = store.load(row["date"], row["period"]).content
    assert loaded.synthetic_url_count == 1
    assert loaded.dead_link_count is not None
    assert loaded.usage.api_calls == _run_summary(caplog)["llm"]["api_calls"]


class _ExplodingDigestStore:
    def __init__(self) -> None:
        self.calls = 0

    def save(self, content, *, raw_page) -> None:
        from cyris.adapters.store.d1 import D1Error

        self.calls += 1
        raise D1Error("D1 unavailable")


async def test_a_failed_digest_write_leaves_the_run_as_it_was(tmp_path: Path) -> None:
    deps, _ = make_deps(tmp_path, _notify_llm(), FakeSource([_notify_article()]))
    digest_store = _ExplodingDigestStore()
    deps = replace(deps, digest_store=digest_store)

    report = await run_digest(deps, RunOptions())

    assert digest_store.calls == 1
    assert report.status == "ok"
    assert report.html_path is not None and report.html_path.exists()


async def test_a_failed_digest_write_is_named_in_the_summary_and_the_run_row(
    tmp_path: Path, caplog
) -> None:
    from fakes import SqliteD1

    from cyris.adapters.store.runs import D1RunLog

    db = SqliteD1()
    deps, _ = make_deps(tmp_path, _notify_llm(), FakeSource([_notify_article()]))
    deps = replace(
        deps, digest_store=_ExplodingDigestStore(), record_run=D1RunLog(db, "sha1").record
    )

    with caplog.at_level("INFO", logger="cyris.service_layer.run_digest"):
        await run_digest(deps, RunOptions())

    summary = _run_summary(caplog)
    assert summary["digest_store_error"] == "D1 unavailable"
    assert summary["status"] == "ok"
    [row] = db.query("SELECT summary FROM digest_runs").rows
    assert json.loads(row["summary"])["digest_store_error"] == "D1 unavailable"


async def test_a_stored_digest_leaves_no_error_in_the_summary(tmp_path: Path, caplog) -> None:
    deps, _ = make_deps(tmp_path, _notify_llm(), FakeSource([_notify_article()]))
    deps, _store, db = _stored_digests(deps)

    with caplog.at_level("INFO", logger="cyris.service_layer.run_digest"):
        await run_digest(deps, RunOptions())

    assert "digest_store_error" not in _run_summary(caplog)
    assert _digest_count(db) == 1


async def test_an_empty_window_stores_no_digest_and_names_no_error(tmp_path: Path, caplog) -> None:
    deps, _ = make_deps(tmp_path, FakeLLM(), FakeSource([]))
    deps, _store, db = _stored_digests(deps)

    with caplog.at_level("INFO", logger="cyris.service_layer.run_digest"):
        report = await run_digest(deps, RunOptions())

    assert report.status == "no_articles"
    assert "digest_store_error" not in _run_summary(caplog)
    assert _digest_count(db) == 0


async def test_a_preview_leaves_the_stored_digest_untouched(tmp_path: Path) -> None:
    from cyris.utils.timezone import now_in_timezone

    contents: list = []
    deps, _ = make_deps(
        tmp_path, _notify_llm(), FakeSource([_notify_article()]), discord_contents=contents
    )
    deps, store, db = _stored_digests(deps)
    # A preview saves no articles, so it can only digest what the store already holds.
    deps.store.save([_notify_article()])
    today = now_in_timezone(deps.cfg.app.general.timezone).strftime("%Y-%m-%d")
    published = DigestContent(
        date=today,
        period="morning",
        sources_processed=1,
        articles_received=99,
        articles_included=99,
        usage=UsageStats(),
    )
    store.save(published, raw_page=True)

    report = await run_digest(deps, RunOptions(dry_run=True))

    assert report.status == "ok"
    assert contents and contents[0].date == today
    assert _digest_count(db) == 1
    assert store.load(today, "morning").content.articles_included == 99


async def test_a_run_without_html_output_still_stores_its_digest(tmp_path: Path) -> None:
    deps, _ = make_deps(tmp_path, _notify_llm(), FakeSource([_notify_article()]))
    deps, store, db = _stored_digests(replace(deps, html_writer=None))

    report = await run_digest(deps, RunOptions())

    assert report.status == "ok"
    assert _digest_count(db) == 1
    [row] = db.query("SELECT date, period FROM digests").rows
    assert store.load(row["date"], row["period"]).raw_page is False


async def test_a_failed_publish_keeps_the_stored_digest(tmp_path: Path, caplog) -> None:
    deps, _ = make_deps(tmp_path, _notify_llm(), FakeSource([_notify_article()]))
    deps.cfg.app.promote.pages_project = "cyris-digest"
    deps, _store, db = _stored_digests(replace(deps, publish_site=lambda _pages, _slug: False))

    with caplog.at_level("INFO", logger="cyris.service_layer.run_digest"):
        await run_digest(deps, RunOptions())

    assert _run_summary(caplog)["status"] == "publish_failed"
    assert _digest_count(db) == 1


class _StoreLosingTheWindowAfterVerdicts:
    """The real store, except the window can no longer be read once states are written."""

    def __init__(self, store) -> None:
        self._store = store
        self._judged = False

    def __getattr__(self, name):
        return getattr(self._store, name)

    def update_states(self, *args, **kwargs):
        self._judged = True
        return self._store.update_states(*args, **kwargs)

    def load_by_time_range(self, *args, **kwargs):
        if self._judged:
            raise RuntimeError("window unreadable")
        return self._store.load_by_time_range(*args, **kwargs)


@pytest.mark.parametrize("collected", [True, False])
async def test_the_stored_digest_renders_the_published_page(
    tmp_path: Path, collected: bool
) -> None:
    deps, _ = make_deps(tmp_path, _notify_llm(), FakeSource([_notify_article()]))
    deps.cfg.app.promote.pages_project = "cyris-digest"
    published: dict[str, bytes] = {}

    def capture(pages: dict[str, bytes], _slug: str) -> bool:
        published.update(pages)
        return True

    deps, store, db = _stored_digests(replace(deps, publish_site=capture))
    if not collected:
        deps = replace(deps, store=_StoreLosingTheWindowAfterVerdicts(deps.store))

    await run_digest(deps, RunOptions())

    [key] = db.query("SELECT date, period FROM digests").rows
    row = store.load(key["date"], key["period"])
    assert row.raw_page is collected
    writer = deps.html_writer
    rerendered = writer.render(row.content, raw_page=row.raw_page).encode("utf-8")
    assert rerendered == published["/" + writer.digest_filename(key["date"], key["period"])]
    assert ("/" + writer.raw_filename(key["date"], key["period"]) in published) is collected


@pytest.mark.parametrize("raw_written", [True, False])
async def test_the_stored_digest_renders_the_written_page(
    tmp_path: Path, raw_written: bool
) -> None:
    deps, _ = make_deps(tmp_path, _notify_llm(), FakeSource([_notify_article()]))
    deps, store, db = _stored_digests(deps)
    if not raw_written:

        def refuse(*_args):
            raise OSError("disk full")

        deps.html_writer.write_raw = refuse

    report = await run_digest(deps, RunOptions())

    [key] = db.query("SELECT date, period FROM digests").rows
    row = store.load(key["date"], key["period"])
    assert row.raw_page is raw_written
    on_disk = report.html_path.read_text(encoding="utf-8")
    assert deps.html_writer.render(row.content, raw_page=row.raw_page) == on_disk
    if not raw_written:
        assert on_disk != deps.html_writer.render(row.content, raw_page=True)


_FAILED_SUBJECT = re.compile(
    r"^Digest run failed: morning, \d{4}-\d{2}-\d{2} \d{2}:\d{2} Asia/Taipei$"
)
_ALERT_WEBHOOK = "https://discord.com/api/webhooks/1/alert"


def _with_alert_channels(deps: Deps, discord, mail) -> Deps:
    deps.cfg.app.notify.discord_webhook_url = _ALERT_WEBHOOK
    deps.cfg.app.notify.email_to = "me@example.org"
    deps.cfg.app.notify.email_from = "d@example.org"
    return replace(deps, send_discord_alert=discord, send_email_alert=mail)


def _subject_minute(subject: str) -> datetime:
    found = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", subject)
    assert found is not None
    return datetime.strptime(found.group(1), "%Y-%m-%d %H:%M").replace(
        tzinfo=ZoneInfo("Asia/Taipei")
    )


def _recording_alert_fakes() -> tuple[list, list, object, object]:
    discord_calls: list[tuple] = []
    mail_calls: list[tuple] = []

    async def discord(webhook_url, subject, text):
        discord_calls.append((webhook_url, subject, text))

    async def mail(recipient, sender, subject, text):
        mail_calls.append((recipient, sender, subject, text))

    return discord_calls, mail_calls, discord, mail


async def test_a_raised_run_names_the_error_on_discord_and_in_mail(tmp_path: Path) -> None:
    discord_calls, mail_calls, discord, mail = _recording_alert_fakes()

    deps, _ = make_deps(tmp_path, FakeLLM(), FakeSource([_notify_article()]))
    deps = _with_alert_channels(deps, discord, mail)
    _exploding_store(deps)

    with pytest.raises(RuntimeError, match="the store is gone"):
        await run_digest(deps, RunOptions())

    assert len(discord_calls) == 1
    webhook, subject, text = discord_calls[0]
    assert webhook == _ALERT_WEBHOOK
    assert _FAILED_SUBJECT.match(subject)
    assert text == "RuntimeError: the store is gone"
    assert mail_calls == [("me@example.org", "d@example.org", subject, text)]


async def test_the_failure_alert_names_the_minute_the_run_started(tmp_path: Path) -> None:
    discord_calls, _, discord, mail = _recording_alert_fakes()

    deps, _ = make_deps(tmp_path, FakeLLM(), FakeSource([_notify_article()]))
    deps = _with_alert_channels(deps, discord, mail)
    _exploding_store(deps)
    before = now_in_timezone("Asia/Taipei").replace(second=0, microsecond=0)

    with pytest.raises(RuntimeError, match="the store is gone"):
        await run_digest(deps, RunOptions())

    after = now_in_timezone("Asia/Taipei")
    when = _subject_minute(discord_calls[0][1])
    assert before <= when <= after


async def test_a_run_that_fails_before_reading_its_clock_still_alerts_with_the_time(
    tmp_path: Path, monkeypatch
) -> None:
    discord_calls, _, discord, mail = _recording_alert_fakes()
    deps, _ = make_deps(tmp_path, FakeLLM(), FakeSource([_notify_article()]))
    deps = _with_alert_channels(deps, discord, mail)

    def no_clock(tz_name: str) -> datetime:
        raise ValueError("no clock")

    monkeypatch.setattr("cyris.service_layer.run_digest.now_in_timezone", no_clock)

    with pytest.raises(ValueError, match="no clock"):
        await run_digest(deps, RunOptions())

    assert _FAILED_SUBJECT.match(discord_calls[0][1])


async def test_the_failure_alert_is_sent_after_the_run_is_recorded(tmp_path: Path) -> None:
    events: list[str] = []

    def record(summary: dict) -> None:
        events.append("record")

    async def discord(webhook_url, subject, text):
        events.append("discord")

    async def mail(recipient, sender, subject, text):
        events.append("mail")

    deps, _ = make_deps(tmp_path, FakeLLM(), FakeSource([_notify_article()]))
    deps = _with_alert_channels(deps, discord, mail)
    deps = replace(deps, record_run=record)
    _exploding_store(deps)

    with pytest.raises(RuntimeError, match="the store is gone"):
        await run_digest(deps, RunOptions())

    assert events == ["record", "discord", "mail"]


async def test_a_failed_run_record_still_alerts_and_keeps_the_store_error(
    tmp_path: Path,
) -> None:
    discord_calls, mail_calls, discord, mail = _recording_alert_fakes()

    def record(summary: dict) -> None:
        raise RuntimeError("d1 down")

    deps, _ = make_deps(tmp_path, FakeLLM(), FakeSource([_notify_article()]))
    deps = _with_alert_channels(deps, discord, mail)
    deps = replace(deps, record_run=record)
    _exploding_store(deps)

    with pytest.raises(RuntimeError, match="the store is gone"):
        await run_digest(deps, RunOptions())

    assert len(discord_calls) == 1
    assert len(mail_calls) == 1


async def test_an_error_with_no_message_is_named_by_its_type_alone(tmp_path: Path) -> None:
    discord_calls, _, discord, mail = _recording_alert_fakes()

    deps, _ = make_deps(tmp_path, FakeLLM(), FakeSource([_notify_article()]))
    deps = _with_alert_channels(deps, discord, mail)

    def explode(*args, **kwargs):
        raise RuntimeError()

    deps.store.save = explode

    with pytest.raises(RuntimeError):
        await run_digest(deps, RunOptions())

    assert discord_calls[0][2] == "RuntimeError"


_FETCH_SUBJECT = re.compile(
    r"^Digest run fetched nothing: morning, \d{4}-\d{2}-\d{2} \d{2}:\d{2} Asia/Taipei$"
)


class BrokenSource(FakeSource):
    def __init__(self) -> None:
        super().__init__([])

    async def fetch_articles(self, **kwargs) -> list[Article]:
        raise RuntimeError("down")


class OtherBrokenSource(BrokenSource):
    pass


async def _fetch_failure_alerts(tmp_path: Path, sources: list) -> tuple[object, list, list]:
    discord_calls, mail_calls, discord, mail = _recording_alert_fakes()

    deps, _ = make_deps(tmp_path, FakeLLM(), FakeSource([]))
    deps = replace(deps, fetch_sources=sources)
    deps = _with_alert_channels(deps, discord, mail)
    report = await run_digest(deps, RunOptions())
    return report, discord_calls, mail_calls


@respx.mock
async def test_a_dead_rss_buffer_alerts_as_a_failed_fetch(tmp_path: Path) -> None:
    worker = "https://cyris-rss.test"
    respx.get(f"{worker}/articles").mock(return_value=httpx.Response(503))

    report, discord_calls, mail_calls = await _fetch_failure_alerts(
        tmp_path, [CloudflareRssSource(worker, "tok")]
    )

    assert report.status == "no_articles"
    assert report.failed_sources == ["CloudflareRssSource"]
    assert discord_calls[0][2] == "Failed sources: CloudflareRssSource"
    assert len(mail_calls) == 1


async def test_a_run_that_fetched_nothing_names_the_failed_source(tmp_path: Path) -> None:
    report, discord_calls, mail_calls = await _fetch_failure_alerts(tmp_path, [BrokenSource()])

    assert report.status == "no_articles"
    assert report.failed_sources == ["BrokenSource"]
    assert len(discord_calls) == 1
    assert len(mail_calls) == 1
    assert discord_calls[0][2] == "Failed sources: BrokenSource"
    assert mail_calls[0][3] == "Failed sources: BrokenSource"
    assert _FETCH_SUBJECT.match(discord_calls[0][1])
    assert mail_calls[0][2] == discord_calls[0][1]


async def test_a_run_that_fetched_nothing_names_every_failed_source(tmp_path: Path) -> None:
    _, discord_calls, mail_calls = await _fetch_failure_alerts(
        tmp_path, [BrokenSource(), OtherBrokenSource()]
    )

    assert discord_calls[0][2] == "Failed sources: BrokenSource, OtherBrokenSource"
    assert mail_calls[0][3] == "Failed sources: BrokenSource, OtherBrokenSource"


async def test_a_healthy_empty_source_is_left_off_the_failed_fetch_alert(tmp_path: Path) -> None:
    _, discord_calls, _ = await _fetch_failure_alerts(tmp_path, [BrokenSource(), FakeSource([])])

    assert discord_calls[0][2] == "Failed sources: BrokenSource"


async def test_an_empty_healthy_window_sends_no_failure_alert(tmp_path: Path) -> None:
    discord_calls, mail_calls, discord, mail = _recording_alert_fakes()
    deps, _ = make_deps(tmp_path, FakeLLM(), FakeSource([]))
    deps = _with_alert_channels(deps, discord, mail)

    report = await run_digest(deps, RunOptions())

    assert report.status == "no_articles"
    assert discord_calls == []
    assert mail_calls == []


async def test_nothing_pending_sends_no_failure_alert_even_if_a_source_failed(
    tmp_path: Path,
) -> None:
    article = _notify_article()
    discord_calls, mail_calls, discord, mail = _recording_alert_fakes()
    deps, _ = make_deps(tmp_path, FakeLLM(), FakeSource([article]))
    deps.store.save([article])
    assert deps.store.accept([article.url]) == 1
    deps = replace(deps, fetch_sources=[BrokenSource(), FakeSource([article])])
    deps = _with_alert_channels(deps, discord, mail)

    report = await run_digest(deps, RunOptions())

    assert report.status == "no_pending"
    assert report.failed_sources == ["BrokenSource"]
    assert discord_calls == []
    assert mail_calls == []


async def test_a_finished_digest_sends_the_digest_and_no_failure_alert(tmp_path: Path) -> None:
    discord_calls, mail_calls, discord, mail = _recording_alert_fakes()
    deps, notifications = make_deps(tmp_path, _notify_llm(), FakeSource([_notify_article()]))
    deps = _with_alert_channels(deps, discord, mail)

    report = await run_digest(deps, RunOptions())

    assert report.status == "ok"
    assert notifications == ["discord"]
    assert discord_calls == []
    assert mail_calls == []


async def test_a_dry_run_that_raises_sends_no_failure_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(**kwargs):
        raise RuntimeError("no network")

    monkeypatch.setattr("cyris.service_layer.run_digest.fetch_all_articles", refuse)
    discord_calls, mail_calls, discord, mail = _recording_alert_fakes()
    deps, _ = make_deps(tmp_path, FakeLLM(), FakeSource([]))
    deps = _with_alert_channels(deps, discord, mail)

    with pytest.raises(RuntimeError, match="no network"):
        await run_digest(deps, RunOptions(dry_run=True))

    assert discord_calls == []
    assert mail_calls == []


async def test_a_dry_run_that_fetched_nothing_sends_no_failure_alert(tmp_path: Path) -> None:
    discord_calls, mail_calls, discord, mail = _recording_alert_fakes()
    deps, _ = make_deps(tmp_path, FakeLLM(), FakeSource([]))
    deps = replace(deps, fetch_sources=[BrokenSource()])
    deps = _with_alert_channels(deps, discord, mail)

    report = await run_digest(deps, RunOptions(dry_run=True))

    assert report.status == "no_articles"
    assert discord_calls == []
    assert mail_calls == []


async def test_a_run_cancelled_by_sigterm_sends_no_failure_alert(tmp_path: Path) -> None:
    fetching = asyncio.Event()

    class HangingSource(FakeSource):
        async def fetch_articles(self, **kwargs) -> list[Article]:
            fetching.set()
            await asyncio.Event().wait()
            return []

    discord_calls, mail_calls, discord, mail = _recording_alert_fakes()
    deps, _ = make_deps(tmp_path, FakeLLM(), HangingSource([]))
    deps = _with_alert_channels(deps, discord, mail)
    deps, recorded = _recording(deps)
    task = asyncio.create_task(run_digest(deps, RunOptions()))
    await fetching.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert discord_calls == []
    assert mail_calls == []
    [summary] = recorded
    assert summary["status"] == "error"


async def test_a_broken_discord_alert_still_sends_the_mail(tmp_path: Path) -> None:
    mail_calls: list[tuple] = []

    async def discord(webhook_url, subject, text):
        raise ValueError("discord broke")

    async def mail(recipient, sender, subject, text):
        mail_calls.append((recipient, sender, subject, text))

    deps, _ = make_deps(tmp_path, FakeLLM(), FakeSource([_notify_article()]))
    deps = _with_alert_channels(deps, discord, mail)
    _exploding_store(deps)

    with pytest.raises(RuntimeError, match="the store is gone"):
        await run_digest(deps, RunOptions())

    assert len(mail_calls) == 1
    recipient, sender, subject, text = mail_calls[0]
    assert recipient == "me@example.org"
    assert sender == "d@example.org"
    assert text == "RuntimeError: the store is gone"
    assert _FAILED_SUBJECT.match(subject)


class StrlessError(Exception):
    """An exception whose message cannot be rendered."""

    def __str__(self) -> str:
        raise ValueError("no string")


async def _broken_alert(*_args, **_kwargs):
    raise ValueError("alert broke")


async def test_a_broken_alert_does_not_replace_the_runs_exception(tmp_path: Path, caplog) -> None:
    deps, _ = make_deps(tmp_path, FakeLLM(), FakeSource([_notify_article()]))
    deps = _with_alert_channels(deps, _broken_alert, _broken_alert)
    _exploding_store(deps)

    with pytest.raises(RuntimeError, match="the store is gone") as caught:
        await run_digest(deps, RunOptions())

    assert type(caught.value) is RuntimeError
    errors = [
        r.getMessage()
        for r in caplog.records
        if r.levelname == "ERROR" and r.name == "cyris.service_layer.run_digest"
    ]
    assert sum("alert broke" in m for m in errors) == 2


async def test_a_broken_alert_does_not_fail_a_run_that_fetched_nothing(tmp_path: Path) -> None:
    deps, _ = make_deps(tmp_path, FakeLLM(), FakeSource([]))
    deps = replace(deps, fetch_sources=[BrokenSource()])
    deps = _with_alert_channels(deps, _broken_alert, _broken_alert)

    report = await run_digest(deps, RunOptions())

    assert report.status == "no_articles"


async def test_an_unprintable_run_error_is_not_replaced_by_its_alert(tmp_path: Path) -> None:
    deps, _ = make_deps(tmp_path, FakeLLM(), FakeSource([_notify_article()]))
    deps = _with_alert_channels(deps, _broken_alert, _broken_alert)
    deps, recorded = _recording(deps)

    def explode(*args, **kwargs):
        raise StrlessError()

    deps.store.save = explode

    with pytest.raises(StrlessError):
        await run_digest(deps, RunOptions())

    [summary] = recorded
    assert summary["status"] == "error"


def _info(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelname == "INFO" and r.name == "cyris.service_layer.run_digest"
    ]


async def test_a_due_alert_with_no_channel_configured_says_which_it_skipped(
    tmp_path: Path, caplog
) -> None:
    discord_calls, mail_calls, discord, mail = _recording_alert_fakes()
    deps, _ = make_deps(tmp_path, FakeLLM(), FakeSource([_notify_article()]))
    deps = replace(deps, send_discord_alert=discord, send_email_alert=mail)
    assert deps.cfg.app.notify.discord_webhook_url == ""
    assert deps.cfg.app.notify.email_to == ""
    _exploding_store(deps)

    with (
        caplog.at_level("INFO", logger="cyris.service_layer.run_digest"),
        pytest.raises(RuntimeError, match="the store is gone"),
    ):
        await run_digest(deps, RunOptions())

    messages = _info(caplog)
    assert "Failure alert: no webhook set, skipping Discord" in messages
    assert "Failure alert: no email address set, skipping mail" in messages
    assert discord_calls == []
    assert mail_calls == []


async def test_a_due_mail_alert_without_api_credentials_is_a_warning(
    tmp_path: Path, caplog
) -> None:
    deps, _ = make_deps(tmp_path, FakeLLM(), FakeSource([_notify_article()]))
    deps.cfg.app.notify.email_to = "me@example.org"
    assert deps.send_email_alert is None
    _exploding_store(deps)

    with (
        caplog.at_level("WARNING", logger="cyris.service_layer.run_digest"),
        pytest.raises(RuntimeError, match="the store is gone"),
    ):
        await run_digest(deps, RunOptions())

    assert any(
        r.levelname == "WARNING" and "CLOUDFLARE_ACCOUNT_ID" in r.getMessage()
        for r in caplog.records
    )


async def test_a_finished_run_with_no_webhook_logs_no_failure_alert(tmp_path: Path, caplog) -> None:
    deps, _ = make_deps(tmp_path, _notify_llm(), FakeSource([_notify_article()]))
    assert deps.cfg.app.notify.discord_webhook_url == ""

    with caplog.at_level("INFO", logger="cyris.service_layer.run_digest"):
        await run_digest(deps, RunOptions())

    assert not any("Failure alert:" in r.getMessage() for r in caplog.records)
