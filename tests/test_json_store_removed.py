"""The json store and every way out of it are gone: what is left of them is a refusal."""

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import get_args

import pytest
from fakes import seeded_sqlite, settings_toml
from test_run_digest import FakeSource
from typer.testing import CliRunner

from cyris.adapters.store.source_store import D1SourceStore
from cyris.bootstrap import build_d1_client, load_effective_config
from cyris.config import JsonStoreRemovedError, StoreConfig, read_config_files
from cyris.domain.models import Article, Lane, SourceConfig
from cyris.entrypoints.cli import app

pytestmark = pytest.mark.integration

COMMANDS = [
    ["doctor"],
    ["config", "show"],
    ["run"],
    ["ingest"],
    ["triage-ui"],
    ["settings", "push"],
]
IDS = ["doctor", "config-show", "run", "ingest", "triage-ui", "settings-push"]


def _run(tmp_path: Path, command: list[str]):
    return CliRunner().invoke(
        app,
        [
            *command,
            "--config",
            str(tmp_path / "cyris.toml"),
            "--sources",
            str(tmp_path / "sources.yaml"),
        ],
    )


def _says_how_on_one_line(result) -> str:
    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Traceback" not in result.output
    [line] = result.output.splitlines()
    assert "json store was removed" in line
    assert "delete the" in line
    assert "run on SQLite" in line
    return line


def test_the_store_command_group_is_an_unknown_command() -> None:
    result = CliRunner().invoke(app, ["store", "--help"])

    assert result.exit_code != 0
    assert "No such command" in result.output


def test_the_json_store_modules_are_gone() -> None:
    for module in ("article_store", "json_topics"):
        assert importlib.util.find_spec(f"cyris.adapters.store.{module}") is None


def test_a_store_is_d1_or_sqlite_and_nothing_else() -> None:
    assert set(get_args(StoreConfig.model_fields["backend"].annotation)) == {"d1", "sqlite"}
    assert not hasattr(StoreConfig, "is_sql")


@pytest.mark.parametrize("command", [["doctor"], ["config", "show"]], ids=["doctor", "config-show"])
def test_a_file_naming_json_stops_doctor_and_config_show_on_one_line(tmp_path, command) -> None:
    (tmp_path / "cyris.toml").write_text('[store]\nbackend = "json"\n')

    line = _says_how_on_one_line(_run(tmp_path, command))

    assert 'backend = "json"' in line
    assert "cyris.toml" in line
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("command", [["doctor"], ["config", "show"]], ids=["doctor", "config-show"])
def test_the_environment_naming_json_stops_doctor_and_config_show_on_one_line(
    tmp_path, monkeypatch, command
) -> None:
    monkeypatch.setenv("CYRIS_STORE_BACKEND", "json")

    line = _says_how_on_one_line(_run(tmp_path, command))

    assert "CYRIS_STORE_BACKEND=json" in line


def test_a_dotenv_naming_json_is_the_environment_naming_it(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CYRIS_STORE_BACKEND", raising=False)
    (tmp_path / "cyris.toml").write_text("")
    (tmp_path / ".env").write_text("CYRIS_STORE_BACKEND=json\n")
    # `.env` loads with setdefault; recorded here, so the undo takes its value away too.
    monkeypatch.setenv("CYRIS_STORE_BACKEND", "")
    monkeypatch.delenv("CYRIS_STORE_BACKEND")

    with pytest.raises(JsonStoreRemovedError, match="CYRIS_STORE_BACKEND=json"):
        read_config_files(tmp_path / "cyris.toml", tmp_path / "sources.yaml")


@pytest.mark.parametrize("command", COMMANDS, ids=IDS)
def test_every_command_that_loads_exits_non_zero_with_no_traceback(tmp_path, command) -> None:
    (tmp_path / "cyris.toml").write_text('[store]\nbackend = "json"\n')

    result = _run(tmp_path, command)

    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Traceback" not in result.output


def test_a_file_naming_a_store_beats_an_environment_naming_json(tmp_path, monkeypatch) -> None:
    """A set file value wins, as it does for every grade-B key."""
    monkeypatch.setenv("CYRIS_STORE_BACKEND", "json")
    (tmp_path / "cyris.toml").write_text('[store]\nbackend = "sqlite"\n')

    raw = read_config_files(tmp_path / "cyris.toml", tmp_path / "sources.yaml")

    assert raw.toml["store"]["backend"] == "sqlite"


def test_a_grade_d_value_or_a_topic_in_the_file_reaches_no_run(tmp_path, monkeypatch) -> None:
    """The file is `settings push`'s to read; a run reads the store alone."""
    state = tmp_path / "cyris.sqlite3"
    monkeypatch.setenv("CYRIS_STORE_SQLITE_PATH", str(state))
    seeded_sqlite(state, **{"digest.max_articles_per_digest": 3})
    config = tmp_path / "cyris.toml"
    config.write_text(
        settings_toml(**{"digest.max_articles_per_digest": 99})
        + '[[tracked_topics]]\nname = "From File"\ndescription = "d"\n'
        + 'threshold = 0.7\nmodel = "m"\n'
    )

    cfg = load_effective_config(config, tmp_path / "sources.yaml")

    assert cfg.app.digest.max_articles_per_digest == 3
    assert cfg.tracked_topics == []


def test_a_sqlite_install_always_has_a_client(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CYRIS_STORE_SQLITE_PATH", str(tmp_path / "cyris.sqlite3"))

    cfg = load_effective_config(tmp_path / "nope.toml", tmp_path / "nope.yaml")

    assert build_d1_client(cfg) is not None


def _leftovers(vault: Path) -> dict[Path, bytes]:
    return {p: p.read_bytes() for p in sorted(vault.rglob("*")) if p.is_file()}


def test_a_vault_holding_the_json_stores_files_is_byte_identical_after_a_run(
    tmp_path, monkeypatch
) -> None:
    """Those files are ignored and untouched: nothing detects them, nothing moves them."""
    vault = tmp_path / "agent-vault"
    (vault / "articles").mkdir(parents=True)
    (vault / "articles" / "2026-10-01.json").write_text('[{"url": "https://example.com/old"}]')
    (vault / "topics.json").write_text('{"topics": {}, "members": [], "tags": [], "votes": []}')
    (vault / "usage.jsonl").write_text('{"timestamp": "2026-10-01T00:00:00+00:00"}\n')
    before = _leftovers(vault)
    state = tmp_path / "state" / "cyris.sqlite3"
    client = seeded_sqlite(state)
    D1SourceStore(client).upsert(SourceConfig(name="Feed", url="https://example.com/feed"))
    monkeypatch.setenv("CYRIS_STORE_SQLITE_PATH", str(state))
    article = Article(
        id=1,
        title="Title",
        url="https://example.com/1",
        content="Content",
        published_at=datetime.now(UTC) - timedelta(hours=1),
        source_name="Feed",
        source_lane=Lane.SKIM,
    )
    monkeypatch.setattr("cyris.bootstrap.build_rss_source", lambda cfg: FakeSource([article]))

    async def no_pages(articles, **_kwargs):
        return articles

    monkeypatch.setattr("cyris.adapters.fetch.article_page.lengthen_articles", no_pages)
    (tmp_path / "cyris.toml").write_text(f'[agent_vault]\npath = "{vault}"\n')

    result = _run(tmp_path, ["run"])
    _run(tmp_path, ["doctor"])
    _run(tmp_path, ["config", "show"])

    assert result.exit_code == 0, result.output
    assert client.query("SELECT count(*) AS n FROM stored_articles").rows == [{"n": 1}]
    assert _leftovers(vault) == before
