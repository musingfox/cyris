"""Direct tests for the run_digest use case."""

import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fakes import FakeLLM

from cyris.adapters.output.html_digest import HtmlDigestWriter
from cyris.adapters.store import ArticleStore
from cyris.bootstrap import Deps
from cyris.config import (
    AgentVaultConfig,
    AppConfig,
    Config,
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
    is_degraded_run,
)
from cyris.service_layer.run_digest import RunOptions, _render_site, run_digest


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
) -> tuple[Deps, list]:
    html_dir = tmp_path / "html"
    agent_vault = tmp_path / "agent-vault"
    agent_vault.mkdir(parents=True)

    cfg = Config(
        app=AppConfig(
            agent_vault=AgentVaultConfig(path=agent_vault),
        ),
        sources={},
    )

    notifications: list[str] = []

    async def fake_discord(webhook_url, content, digest_url="", publish_failed=False):
        notifications.append("discord")
        if discord_contents is not None:
            discord_contents.append(content)

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


async def test_publish_outcome_reaches_discord(tmp_path: Path) -> None:
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
        def write(self, content, raw_page: bool = False) -> Path:
            path = tmp_path / f"{content.date}-{content.period}.html"
            path.write_text("<html></html>")
            return path

    async def run_with(publish_ok: bool, run_dir: Path) -> dict:
        deps, _ = make_deps(run_dir, _llm(), FakeSource([_article()]))
        deps.cfg.app.promote.pages_project = "cyris-digest"
        sent: dict = {}

        async def capture(webhook_url, content, digest_url="", publish_failed=False):
            sent["digest_url"] = digest_url
            sent["publish_failed"] = publish_failed

        deps = replace(
            deps,
            html_writer=StubHtmlWriter(),
            publish=lambda _slug: publish_ok,
            send_discord=capture,
        )
        await run_digest(deps, RunOptions())
        return sent

    failed = await run_with(False, tmp_path / "failed")
    assert failed == {"digest_url": "", "publish_failed": True}

    ok = await run_with(True, tmp_path / "ok")
    assert ok["publish_failed"] is False
    assert ok["digest_url"].startswith("https://cyris-digest.pages.dev/")


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

    pages = _render_site(deps, content, [_stored_article()] if collected else [])

    index = pages["/index.html"].decode("utf-8")
    assert ('href="2026-04-15-evening-raw.html">All articles</a>' in index) is linked
    assert (">All articles</a>" in index) is linked
    digest = pages["/2026-04-15-evening.html"].decode("utf-8")
    assert ('<a href="2026-04-15-evening-raw.html">All articles</a>' in digest) is linked
    assert (">All articles</a>" in digest) is linked


def _published_index(deps, content: DigestContent) -> str:
    return _render_site(deps, content, [])["/index.html"].decode("utf-8")


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
    assert "<h2>Run lead</h2>" in card
    assert '<span class="data">7 articles</span>' in card
    panels = index[index.index('<section class="panel">') :]
    assert 'href="2026-04-15-evening.html"' in panels


def test_the_published_archive_rows_carry_the_recorded_counts(tmp_path: Path) -> None:
    deps = SimpleNamespace(
        html_writer=HtmlDigestWriter(tmp_path),
        site_filenames=lambda: ["2026-04-15-evening.html"],
        archive_counts=lambda: {("2026-04-15", "evening"): 12},
    )

    index = _published_index(deps, _run_content("2026-04-16", "morning", "Lead", 3))

    row = index[index.index('<div class="archive-row">') :].split("</div>", 1)[0]
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
    rows = re.findall(r'<div class="archive-row[^"]*">.*?</div>', index, re.S)
    assert rows
    assert [row for row in rows if 'class="small"' in row] == []
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert "d1 down" in errors[0].getMessage()


async def test_run_digest_warns_when_no_llm_provider_is_configured(tmp_path: Path) -> None:
    """A fresh deploy publishes excerpts until a provider is chosen; say so.

    This is the first-run failure a deploy is least likely to notice: the digest
    goes out, looks thin, and nothing reports a cause.
    """
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
    deps, _ = make_deps(tmp_path, llm=None, source=FakeSource([article]))
    messages: list[str] = []
    deps = replace(deps, on_progress=messages.append)

    await run_digest(deps, RunOptions(period="morning"))

    assert any("no LLM provider configured" in m for m in messages), messages


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
    deps, _ = make_deps(
        tmp_path,
        FakeLLM(error=RuntimeError("quota"), model="test-model"),
        FakeSource([_notify_article()]),
        discord_contents=contents,
    )
    deps.cfg.app.notify.discord_webhook_url = "https://discord.com/api/webhooks/1/quota"
    deps.cfg.app.llm_provider.model = ""

    await run_digest(deps, RunOptions())

    assert contents[0].usage.model == "test-model"
    assert contents[0].usage.input_tokens == 0
    assert is_degraded_run(contents[0].usage)


async def test_notification_marks_zero_token_llm_usage_as_degraded(tmp_path: Path) -> None:
    contents: list = []
    deps, _ = make_deps(
        tmp_path,
        _notify_llm(input_tokens=0, model="test-model"),
        FakeSource([_notify_article()]),
        discord_contents=contents,
    )
    deps.cfg.app.notify.discord_webhook_url = "https://discord.com/api/webhooks/1/zero"
    deps.cfg.app.llm_provider.model = ""

    await run_digest(deps, RunOptions())

    assert is_degraded_run(contents[0].usage)


async def test_notification_keeps_actual_llm_model_when_config_model_is_empty(
    tmp_path: Path,
) -> None:
    contents: list = []
    deps, _ = make_deps(
        tmp_path,
        _notify_llm(model="test-model"),
        FakeSource([_notify_article()]),
        discord_contents=contents,
    )
    deps.cfg.app.notify.discord_webhook_url = "https://discord.com/api/webhooks/1/used"
    deps.cfg.app.llm_provider.model = ""

    await run_digest(deps, RunOptions())

    assert contents[0].usage.model == "test-model"
    assert not is_degraded_run(contents[0].usage)


async def test_notification_marks_no_llm_as_not_degraded(tmp_path: Path) -> None:
    contents: list = []
    deps, _ = make_deps(
        tmp_path,
        llm=None,
        source=FakeSource([_notify_article()]),
        discord_contents=contents,
    )
    deps.cfg.app.notify.discord_webhook_url = "https://discord.com/api/webhooks/1/no-llm"

    await run_digest(deps, RunOptions())

    assert contents[0].usage.model == NO_LLM_MODEL
    assert not is_degraded_run(contents[0].usage)


async def test_a_run_with_no_webhook_says_it_is_skipping_the_notification(
    tmp_path: Path, caplog, monkeypatch
) -> None:
    monkeypatch.delenv("CYRIS_DISCORD_WEBHOOK_URL", raising=False)
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
    monkeypatch.delenv("CYRIS_DISCORD_WEBHOOK_URL", raising=False)
    deps, _ = make_deps(tmp_path, _notify_llm(), FakeSource([_notify_article()]))
    deps.cfg.app.notify.discord_webhook_url = "https://discord.com/api/webhooks/1/run-log"
    sent: dict = {}

    async def capture(webhook_url, content, digest_url="", publish_failed=False):
        sent["webhook_url"] = webhook_url

    deps = replace(deps, send_discord=capture)

    with caplog.at_level("INFO", logger="cyris.service_layer.run_digest"):
        await run_digest(deps, RunOptions())

    assert _skip_records(caplog) == []
    assert sent["webhook_url"] == "https://discord.com/api/webhooks/1/run-log"


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


async def test_a_zero_token_run_records_the_degraded_verdict_discord_shows(
    tmp_path: Path,
) -> None:
    contents: list = []
    deps, _ = make_deps(
        tmp_path,
        _notify_llm(input_tokens=0, model="test-model"),
        FakeSource([_notify_article()]),
        discord_contents=contents,
    )
    deps, recorded = _recording(deps)
    deps.cfg.app.notify.discord_webhook_url = "https://discord.com/api/webhooks/1/zero"
    deps.cfg.app.llm_provider.model = ""

    await run_digest(deps, RunOptions())

    assert recorded[0]["degraded"] is True
    assert recorded[0]["degraded"] == is_degraded_run(contents[0].usage)


async def test_a_run_that_used_its_llm_records_not_degraded(tmp_path: Path) -> None:
    contents: list = []
    deps, _ = make_deps(
        tmp_path,
        _notify_llm(model="test-model"),
        FakeSource([_notify_article()]),
        discord_contents=contents,
    )
    deps, recorded = _recording(deps)
    deps.cfg.app.notify.discord_webhook_url = "https://discord.com/api/webhooks/1/used"
    deps.cfg.app.llm_provider.model = ""

    await run_digest(deps, RunOptions())

    assert recorded[0]["degraded"] is False
    assert recorded[0]["degraded"] == is_degraded_run(contents[0].usage)


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
