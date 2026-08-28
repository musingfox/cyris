"""Direct tests for the run_digest use case."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fakes import FakeLLM

from cyris.adapters.output.html_digest import HtmlDigestWriter
from cyris.adapters.store import ArticleStore
from cyris.bootstrap import Deps
from cyris.config import (
    AgentVaultConfig,
    AppConfig,
    Config,
)
from cyris.domain.models import Article, ArticleState, Tier
from cyris.service_layer.run_digest import RunOptions, run_digest


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
        def write(self, content) -> Path:
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


async def test_scoring_tag_write_failure_does_not_stop_later_batches(tmp_path: Path) -> None:
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
        {"id": i, "score": 80, "language": "en", "tags": ["First"]}
        for i in range(1, 21)
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

    class FlakyTagStore:
        def __init__(self) -> None:
            self.calls = 0

        def save(self, url_to_tags) -> None:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("D1 unavailable")

    tag_store = FlakyTagStore()
    deps = replace(deps, tag_store=tag_store)

    report = await run_digest(deps, RunOptions())

    assert report.status == "ok"
    assert tag_store.calls == 2
    stored = deps.store.get_by_urls(["https://example.com/21"])
    assert stored[0].score == 81
