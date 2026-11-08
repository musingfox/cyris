"""The local edition's hourly ingest: the article store is the RSS buffer.

A feed holds only a few hours of entries, so a digest that polls only at digest time
misses what a busy feed published and dropped in between (ADR-0001). `cyris ingest`
polls and stores without scoring or publishing; the digest reads the store's window
by `first_seen_at`, so what an earlier hour stored reaches it.
"""

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fakes import FakeLLM, make_config
from typer.testing import CliRunner

from cyris.adapters.output.html_digest import HtmlDigestWriter
from cyris.adapters.store import ArticleStore
from cyris.bootstrap import Deps
from cyris.config import AgentVaultConfig
from cyris.domain.models import Article, ArticleState, Lane
from cyris.entrypoints.cli import app
from cyris.service_layer.ingest import ingest
from cyris.service_layer.run_digest import RunOptions, run_digest

pytestmark = pytest.mark.integration


class FakeSource:
    def __init__(self, articles: list[Article]) -> None:
        self.articles = articles

    async def fetch_articles(self, **kwargs) -> list[Article]:
        return self.articles

    async def health_check(self) -> bool:
        return True


class BrokenSource:
    async def fetch_articles(self, **kwargs) -> list[Article]:
        raise RuntimeError("feed host down")

    async def health_check(self) -> bool:
        return False


def article(n: int) -> Article:
    return Article(
        id=n,
        title=f"Follow-lane story {n}",
        url=f"https://example.com/{n}",
        content=f"Body of story {n}.",
        published_at=datetime.now(UTC) - timedelta(hours=1),
        source_name="Blog",
        source_lane=Lane.FOLLOW,
        source_tags=[],
    )


def config(tmp_path: Path):
    vault = tmp_path / "agent-vault"
    vault.mkdir(exist_ok=True)
    return make_config(agent_vault=AgentVaultConfig(path=vault)), vault


async def test_ingest_stores_pending_rows_and_calls_nothing_else(tmp_path: Path) -> None:
    cfg, vault = config(tmp_path)
    store = ArticleStore(vault)

    report = await ingest(cfg, store, [FakeSource([article(1), article(2)])])

    assert (report.status, report.fetched, report.saved, report.failed_sources) == (
        "ok",
        2,
        2,
        [],
    )
    rows = store.load_by_time_range(
        start=datetime.now(UTC) - timedelta(days=1), end=datetime.now(UTC) + timedelta(minutes=1)
    )
    assert {r.url: r.state for r in rows} == {
        "https://example.com/1": ArticleState.PENDING,
        "https://example.com/2": ArticleState.PENDING,
    }


async def test_a_second_ingest_of_the_same_feed_saves_nothing(tmp_path: Path) -> None:
    cfg, vault = config(tmp_path)
    store = ArticleStore(vault)
    source = FakeSource([article(1)])

    await ingest(cfg, store, [source])
    again = await ingest(cfg, store, [source])

    assert (again.saved, again.skipped) == (0, 1)


async def test_a_failed_fetch_is_an_ingest_status_of_its_own(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg, vault = config(tmp_path)

    with caplog.at_level(logging.INFO, logger="cyris.service_layer.ingest"):
        report = await ingest(cfg, ArticleStore(vault), [BrokenSource()])

    assert (report.status, report.failed_sources) == ("failed", ["BrokenSource"])
    events = [
        json.loads(r.getMessage().split(" ", 1)[1])
        for r in caplog.records
        if r.getMessage().startswith("ingest_summary ")
    ]
    assert [e["event"] for e in events] == ["ingest_summary"]
    assert events[0]["status"] == "failed"


@pytest.mark.parametrize(
    "at_digest_time", [[article(2)], []], ids=["feed-has-others", "feed-empty"]
)
async def test_what_an_earlier_hour_stored_reaches_the_digest(
    tmp_path: Path, at_digest_time: list[Article]
) -> None:
    """The feed dropped story 1 before the digest hour; the digest still carries it."""
    cfg, vault = config(tmp_path)
    store = ArticleStore(vault)
    await ingest(cfg, store, [FakeSource([article(1)])])

    html_dir = tmp_path / "html"

    async def no_discord(*args, **kwargs):
        return {"status": "skipped"}

    deps = Deps(
        cfg=cfg,
        store=store,
        llm=FakeLLM([]),
        fetch_sources=[FakeSource(at_digest_time)],
        html_writer=HtmlDigestWriter(html_dir),
        publish=None,
        sync_promotions=None,
        log_usage=lambda content: None,
        send_discord=no_discord,
    )
    report = await run_digest(deps, RunOptions(period="morning"))

    assert report.status == "ok"
    page = next(html_dir.glob("*-morning.html")).read_text()
    assert "https://example.com/1" in page
    assert all(a.url in page for a in at_digest_time)


def write_config(tmp_path: Path) -> tuple[Path, Path]:
    vault = tmp_path / "agent-vault"
    vault.mkdir()
    config_path = tmp_path / "cyris.toml"
    example = (Path(__file__).resolve().parent.parent / "settings.example.toml").read_text()
    config_path.write_text(
        example.replace('provider = "anthropic"', 'provider = "none"').replace(
            'model = "claude-sonnet-4-6"', 'model = ""'
        )
        + f'\n[agent_vault]\npath = "{vault}"\n\n[store]\nbackend = "json"\n'
    )
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text('sources:\n  - {"name": "Blog", "url": "https://example.com/feed"}\n')
    return config_path, sources_path


def test_cli_exits_non_zero_when_the_fetch_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, sources_path = write_config(tmp_path)
    monkeypatch.setattr("cyris.bootstrap.build_rss_source", lambda cfg: BrokenSource())

    result = CliRunner().invoke(
        app, ["ingest", "--config", str(config_path), "--sources", str(sources_path)]
    )

    assert result.exit_code == 1, result.output


def test_cli_stores_what_the_feed_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path, sources_path = write_config(tmp_path)
    monkeypatch.setattr("cyris.bootstrap.build_rss_source", lambda cfg: FakeSource([article(1)]))

    result = CliRunner().invoke(
        app, ["ingest", "--config", str(config_path), "--sources", str(sources_path)]
    )

    assert result.exit_code == 0, result.output
    assert "Stored 1 new article" in result.output
    assert list((tmp_path / "agent-vault" / "articles").glob("*.json"))


def test_cli_polls_without_the_llm_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path, sources_path = write_config(tmp_path)
    config_path.write_text(
        config_path.read_text()
        .replace('\nprovider = "none"', '\nprovider = "anthropic"', 1)
        .replace('\nmodel = ""', '\nmodel = "claude-haiku-5-5"', 1)
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("cyris.bootstrap.build_rss_source", lambda cfg: FakeSource([article(1)]))

    result = CliRunner().invoke(
        app, ["ingest", "--config", str(config_path), "--sources", str(sources_path)]
    )

    assert result.exit_code == 0, result.output
    assert "Stored 1 new article" in result.output
