"""Direct tests for the run_digest use case."""

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from fakes import FakeLLM

from cyris.adapters.output.digest import DigestWriter
from cyris.adapters.store import ArticleStore
from cyris.adapters.store.event_store import EventStore
from cyris.bootstrap import Deps, build_deps
from cyris.config import (
    AgentVaultConfig,
    AppConfig,
    Config,
    MinifluxConfig,
    ObsidianConfig,
    VaultConfigSource,
)
from cyris.domain.models import Article, ArticleState, Tier
from cyris.service_layer.run_digest import RunOptions, run_digest
from cyris.utils.timezone import now_in_timezone


def _tracked_article() -> Article:
    return Article(
        id=1,
        title="EU AI regulation passes",
        url="https://example.com/reg",
        content="The EU parliament passed the AI act.",
        published_at=datetime.now(UTC) - timedelta(hours=1),
        source_name="Reuters",
        source_tier=Tier.SUMMARIZE,
        source_tags=["tech"],
    )


def _write_tracking_yaml(agent_vault: Path) -> None:
    topics = [
        {
            "name": "AI 監管",
            "keywords": ["regulation"],
            "created": "2026-07-01",
            "status": "active",
        }
    ]
    (agent_vault / "tracking.yaml").write_text(
        yaml.safe_dump({"topics": topics}, allow_unicode=True)
    )


_TRACKED_SCORING_RESP = json.dumps({"scores": [{"id": 1, "score": 85, "language": "en"}]})
_TRACKED_CONFIRM_RESP = json.dumps(
    {"matches": [{"id": 1, "topic": "AI 監管", "note": "歐盟法案通過"}]}
)
_TRACKED_SECTIONS_RESP = json.dumps(
    {
        "sections": [
            {
                "heading": "EU AI regulation passes",
                "summary": "歐盟通過 AI 法案",
                "articles": [{"id": 1, "title": "EU AI regulation passes", "source": "Reuters"}],
            }
        ]
    }
)


class FakeSource:
    """In-memory FetchSource."""

    def __init__(self, articles: list[Article]) -> None:
        self._articles = articles
        self.marked_read: list[list] = []

    async def fetch_articles(self, **kwargs) -> list[Article]:
        return self._articles

    async def mark_as_read(self, article_ids: list) -> None:
        self.marked_read.append(article_ids)

    async def health_check(self) -> bool:
        return True


def make_deps(
    tmp_path: Path,
    llm: FakeLLM,
    source: FakeSource,
    *,
    with_tracking: bool = False,
    discord_contents: list | None = None,
) -> tuple[Deps, list]:
    user_vault = tmp_path / "user-vault"
    (user_vault / "Digests").mkdir(parents=True)
    agent_vault = tmp_path / "agent-vault"
    agent_vault.mkdir()

    cfg = Config(
        app=AppConfig(
            obsidian=ObsidianConfig(user_vault_path=user_vault),
            agent_vault=AgentVaultConfig(path=agent_vault),
        ),
        sources={},
        aliases={},
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
        writer=DigestWriter(user_vault, "Digests"),
        html_writer=None,
        publish=None,
        sync_promotions=None,
        load_cookies=lambda: None,
        log_usage=lambda content: None,
        send_discord=fake_discord,
        tracking=VaultConfigSource(agent_vault / "tracking.yaml") if with_tracking else None,
        event_store=EventStore(agent_vault / "events") if with_tracking else None,
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
                                {"id": 1, "title": "Enterprise AI Adoption", "source": "TechSource"}
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
    report = await run_digest(deps, RunOptions(enable_learning=False))

    assert report.status == "ok"
    assert report.digest_path is not None and report.digest_path.exists()
    assert "AI 趨勢" in report.digest_path.read_text()

    # Article saved, marked read, scored, and accepted
    assert source.marked_read == [[1]]
    stored = deps.store.get_by_urls(["https://example.com/ai"])
    assert stored[0].state == ArticleState.ACCEPTED
    assert stored[0].score == 85.0


async def test_run_digest_no_articles(tmp_path: Path) -> None:
    source = FakeSource([])
    deps, notifications = make_deps(tmp_path, FakeLLM(), source)

    report = await run_digest(deps, RunOptions(enable_learning=False))

    assert report.status == "no_articles"
    assert report.digest_path is None


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

    report = await run_digest(deps, RunOptions(dry_run=True, enable_learning=False))

    assert report.status == "ok"
    assert report.rendered is not None


def test_tracking_path_resolution_build_deps(tmp_path: Path) -> None:
    """T1: build_deps anchors tracking+event_store to agent_vault.path."""
    av = tmp_path / "av"
    cfg = Config(
        app=AppConfig(
            obsidian=ObsidianConfig(user_vault_path=tmp_path / "u"),
            agent_vault=AgentVaultConfig(path=av),
            miniflux=MinifluxConfig(api_key="test-key"),
        ),
        sources={},
        aliases={},
    )
    deps = build_deps(cfg)
    assert isinstance(deps.tracking, VaultConfigSource)
    assert deps.tracking.tracking_file == av / "tracking.yaml"
    assert isinstance(deps.event_store, EventStore)
    assert deps.event_store.events_dir == av / "events"


def test_tracking_path_resolution_deps_backward_compat(tmp_path: Path) -> None:
    """T2: manual Deps() from make_deps fields works; tracking/event_store default to None."""
    deps, _ = make_deps(tmp_path, FakeLLM(), FakeSource([]))
    assert deps.tracking is None
    assert deps.event_store is None


async def test_tracked_pass_in_run_happy_path(tmp_path: Path) -> None:
    """T1: tracked block carries article title + note; article also stays in sections block."""
    llm = FakeLLM([_TRACKED_SCORING_RESP, _TRACKED_CONFIRM_RESP, _TRACKED_SECTIONS_RESP])
    source = FakeSource([_tracked_article()])
    contents: list = []
    deps, _ = make_deps(tmp_path, llm, source, with_tracking=True, discord_contents=contents)
    _write_tracking_yaml(deps.cfg.app.agent_vault.path)

    report = await run_digest(deps, RunOptions(enable_learning=False))

    assert report.status == "ok"
    # Call order contract: scoring -> topic-confirm -> pipeline sections
    assert len(llm.calls) == 3
    assert "Score the following" in llm.calls[0]["prompt"]
    assert "AI 監管" in llm.calls[1]["prompt"]

    assert report.digest_path is not None
    text = report.digest_path.read_text()
    assert "## 追蹤主題更新" in text
    tracked_block = text.split("## 追蹤主題更新")[1].split("---")[0]
    assert "EU AI regulation passes" in tracked_block  # article title
    assert "歐盟法案通過" in tracked_block  # LLM note
    assert "(Reuters)" in tracked_block  # source name
    assert "[[AI 監管]]" in tracked_block  # event wiki-link
    # No dedup: the article title still appears in its normal section after the block
    rest = text.split("## 追蹤主題更新")[1].split("---", 1)[1]
    assert "EU AI regulation passes" in rest

    # Event file created with today's (cfg timezone) timeline line
    event_file = deps.cfg.app.agent_vault.path / "events" / "AI 監管.md"
    assert event_file.exists()
    today_iso = now_in_timezone(deps.cfg.app.general.timezone).date().isoformat()
    assert f"- **{today_iso}**" in event_file.read_text()

    # Confirm-call tokens merged into content.usage (3 FakeLLM calls x 500/100)
    assert contents and contents[0].usage.input_tokens == 1500
    assert contents[0].usage.output_tokens == 300


async def test_event_write_dry_run_gate(tmp_path: Path) -> None:
    """Dry run previews the tracked block without event files or wiki-links."""
    llm = FakeLLM([_TRACKED_SCORING_RESP, _TRACKED_CONFIRM_RESP, _TRACKED_SECTIONS_RESP])
    article = _tracked_article()
    source = FakeSource([article])
    contents: list = []
    deps, _ = make_deps(tmp_path, llm, source, with_tracking=True, discord_contents=contents)
    _write_tracking_yaml(deps.cfg.app.agent_vault.path)
    # Dry run skips saving, so preview articles must already be in the store
    deps.store.save([article])

    report = await run_digest(deps, RunOptions(dry_run=True, enable_learning=False))

    assert report.status == "ok"
    assert report.rendered is not None
    assert "追蹤主題更新" in report.rendered
    assert "歐盟法案通過" in report.rendered
    assert "[[" not in report.rendered  # no event ref without an actual write

    events_dir = deps.cfg.app.agent_vault.path / "events"
    assert not events_dir.exists() or not list(events_dir.glob("*.md"))

    # Discord still receives the tracked content on dry run
    assert contents
    assert contents[0].tracked_updates is not None
    assert contents[0].tracked_updates.items


async def test_match_failure_isolation_bad_confirm_json(tmp_path: Path, caplog) -> None:
    """Confirm-call garbage JSON degrades to no tracked block; digest still written."""
    llm = FakeLLM([_TRACKED_SCORING_RESP, "not-json", json.dumps({"sections": []})])
    source = FakeSource([_tracked_article()])
    deps, _ = make_deps(tmp_path, llm, source, with_tracking=True)
    _write_tracking_yaml(deps.cfg.app.agent_vault.path)

    with caplog.at_level(logging.WARNING):
        report = await run_digest(deps, RunOptions(enable_learning=False))

    assert report.status == "ok"
    assert report.digest_path is not None and report.digest_path.exists()
    assert "追蹤主題更新" not in report.digest_path.read_text()
    assert any("tracked topic matching failed" in r.message for r in caplog.records)


async def test_no_topics_noop_inactive_only(tmp_path: Path):
    """T2: tracking only inactive -> 2 calls, no tracked block."""
    article = Article(
        id=1,
        title="TSMC news",
        url="https://ex/tsmc",
        content="TSMC update",
        published_at=datetime.now(UTC) - timedelta(hours=1),
        source_name="Tech",
        source_tier=Tier.SUMMARIZE,
        source_tags=["tech"],
    )
    llm = FakeLLM(
        [
            json.dumps({"scores": [{"id": 1, "score": 80, "language": "en"}]}),
            json.dumps({"sections": []}),
        ]
    )
    source = FakeSource([article])
    deps, _ = make_deps(tmp_path, llm, source, with_tracking=True)
    av = deps.cfg.app.agent_vault.path
    tracking_p = av / "tracking.yaml"
    inactive = [
        {
            "name": "台積電",
            "keywords": ["TSMC"],
            "created": "2026-01-01",
            "status": "inactive",
        }
    ]
    tracking_p.write_text(yaml.safe_dump({"topics": inactive}, allow_unicode=True))
    report = await run_digest(deps, RunOptions(enable_learning=False))
    assert report.status == "ok"
    assert len(llm.calls) == 2
    text = report.rendered or (report.digest_path.read_text() if report.digest_path else "")
    assert "追蹤主題更新" not in text


async def test_no_topics_noop_active_but_no_match(tmp_path: Path):
    """T3: active topic no prescreen hit -> 2 calls, no confirm."""
    article = Article(
        id=2,
        title="unrelated foo",
        url="https://ex/q",
        content="foo",
        published_at=datetime.now(UTC) - timedelta(hours=1),
        source_name="Tech",
        source_tier=Tier.SUMMARIZE,
        source_tags=[],
    )
    llm = FakeLLM(
        [
            json.dumps({"scores": [{"id": 2, "score": 80, "language": "en"}]}),
            json.dumps({"sections": []}),
        ]
    )
    source = FakeSource([article])
    deps, _ = make_deps(tmp_path, llm, source, with_tracking=True)
    av = deps.cfg.app.agent_vault.path
    tracking_p = av / "tracking.yaml"
    active = [
        {
            "name": "量子",
            "keywords": ["quantum"],
            "created": "2026-01-01",
            "status": "active",
        }
    ]
    tracking_p.write_text(yaml.safe_dump({"topics": active}, allow_unicode=True))
    report = await run_digest(deps, RunOptions(enable_learning=False))
    assert report.status == "ok"
    assert len(llm.calls) == 2
    text = report.rendered or (report.digest_path.read_text() if report.digest_path else "")
    assert "追蹤主題更新" not in text


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
        await run_digest(deps, RunOptions(enable_learning=False))
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

    report = await run_digest(deps, RunOptions(enable_learning=False))

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

    report = await run_digest(deps, RunOptions(enable_learning=False))

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

    report = await run_digest(deps, RunOptions(enable_learning=False))

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

    report = await run_digest(deps, RunOptions(enable_learning=False))

    assert report.status == "ok"
    assert not any("newsletter" in m.lower() for m in messages)
    assert contents
    assert contents[0].synthetic_url_count == 0

