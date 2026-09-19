"""The commands that read runtime settings refuse to start while any is missing.

Under pytest the CLI's `logging.basicConfig` finds pytest's handlers already on
the root logger, so the error a real process writes to stderr lands in `caplog`.
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fakes import SqliteD1, seed_d1_settings, settings_toml
from typer.testing import CliRunner

from cyris.config import GRADE_D_KEYS
from cyris.domain.models import SourceConfig
from cyris.entrypoints.cli import app

runner = CliRunner()


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Records every `build_deps` call: nothing is fetched without one."""
    calls: list[object] = []

    def fake_build_deps(cfg, *args, **kwargs):
        calls.append(cfg)
        raise RuntimeError("the run went past the settings gate")

    monkeypatch.setattr("cyris.bootstrap.build_deps", fake_build_deps)
    return calls


def _at_hour(monkeypatch: pytest.MonkeyPatch, hour: int) -> None:
    monkeypatch.setattr(
        "cyris.utils.timezone.now_in_timezone",
        lambda tz: datetime(2026, 9, 19, hour, 5, tzinfo=ZoneInfo(tz)),
    )


def _paths(tmp_path: Path, toml: str = "") -> list[str]:
    config_path = tmp_path / "cyris.toml"
    config_path.write_text(f'[agent_vault]\npath = "{tmp_path / "vault"}"\n\n' + toml)
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text('sources:\n  - name: "Feed"\n    url: "https://a.test/feed"\n')
    return ["--config", str(config_path), "--sources", str(sources_path)]


def _one_d1_source(d1: SqliteD1) -> None:
    from cyris.adapters.store.d1 import apply_schema
    from cyris.adapters.store.source_store import D1SourceStore

    apply_schema(d1)
    D1SourceStore(d1).upsert(SourceConfig(name="Feed", url="https://a.test/feed"))


def test_run_if_due_on_an_empty_d1_names_every_key_and_fetches_nothing(
    tmp_path: Path, d1: SqliteD1, built: list[object], caplog, monkeypatch
) -> None:
    _at_hour(monkeypatch, 8)

    result = runner.invoke(app, ["run", "--if-due", *_paths(tmp_path)])

    assert result.exit_code == 1
    assert "Configuration error: Missing settings in D1: " in caplog.text
    assert "general.timezone" in caplog.text
    assert "cyris settings push" in caplog.text
    assert built == []


def test_run_names_the_key_cyris_toml_lacks(
    tmp_path: Path, built: list[object], caplog, monkeypatch
) -> None:
    monkeypatch.delenv("CYRIS_STORE_BACKEND", raising=False)

    result = runner.invoke(
        app, ["run", *_paths(tmp_path, settings_toml(omit=["digest.max_featured"]))]
    )

    assert result.exit_code == 1
    assert (
        "Missing from cyris.toml: digest.max_featured. cyris.toml.example lists every key."
        in caplog.text
    )
    assert built == []


def test_articles_score_names_the_missing_snippet_length(
    tmp_path: Path, caplog, monkeypatch
) -> None:
    monkeypatch.delenv("CYRIS_STORE_BACKEND", raising=False)

    result = runner.invoke(
        app,
        [
            "articles",
            "score",
            *_paths(tmp_path, settings_toml(omit=["digest.scoring_snippet_length"])),
        ],
    )

    assert result.exit_code == 1
    assert "digest.scoring_snippet_length" in caplog.text


@pytest.mark.parametrize(
    "command",
    [["vote-sim"], ["embed-compare"], ["llm-compare", "--arm", "gemini:x"]],
    ids=["vote-sim", "embed-compare", "llm-compare"],
)
def test_the_diagnostic_commands_name_a_missing_key(
    command: list[str], tmp_path: Path, built: list[object], caplog, monkeypatch
) -> None:
    monkeypatch.delenv("CYRIS_STORE_BACKEND", raising=False)

    result = runner.invoke(
        app, [*command, *_paths(tmp_path, settings_toml(omit=["vote_similarity.max_seeds"]))]
    )

    assert result.exit_code == 1
    assert "Missing from cyris.toml: vote_similarity.max_seeds" in caplog.text
    assert built == []


def test_a_complete_d1_off_the_hour_is_not_due(
    tmp_path: Path, d1: SqliteD1, built: list[object], monkeypatch
) -> None:
    seed_d1_settings(d1)
    _one_d1_source(d1)
    _at_hour(monkeypatch, 11)

    result = runner.invoke(app, ["run", "--if-due", *_paths(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Not a digest hour (08:00, 20:00)." in result.output
    assert built == []


def test_the_error_lists_all_twenty_one_keys_on_an_empty_d1(tmp_path: Path, d1: SqliteD1) -> None:
    from cyris.bootstrap import load_effective_config
    from cyris.config import IncompleteSettingsError

    cfg = load_effective_config(tmp_path / "nope.toml", tmp_path / "nope.yaml")

    with pytest.raises(IncompleteSettingsError) as caught:
        cfg.require_complete_settings()
    assert str(caught.value) == (
        f"Missing settings in D1: {', '.join(sorted(GRADE_D_KEYS))}. "
        "Set them on /settings, or run `cyris settings push` to copy them from cyris.toml."
    )


def test_run_on_a_d1_with_no_sources_says_how_to_add_one(
    tmp_path: Path, d1: SqliteD1, built: list[object], caplog
) -> None:
    seed_d1_settings(d1)

    result = runner.invoke(app, ["run", *_paths(tmp_path)])

    assert result.exit_code == 1
    assert "No sources in D1. Add one on /settings, or run `cyris sources push`." in caplog.text
    assert built == []


def test_run_without_sources_yaml_names_the_file(
    tmp_path: Path, built: list[object], caplog, monkeypatch
) -> None:
    monkeypatch.delenv("CYRIS_STORE_BACKEND", raising=False)
    config_path = tmp_path / "cyris.toml"
    config_path.write_text(settings_toml())

    result = runner.invoke(
        app, ["run", "--config", str(config_path), "--sources", str(tmp_path / "absent.yaml")]
    )

    assert result.exit_code == 1
    assert "No sources in sources.yaml." in caplog.text
    assert built == []


def test_promote_sync_starts_on_an_empty_d1(tmp_path: Path, d1: SqliteD1, monkeypatch) -> None:
    monkeypatch.delenv("CYRIS_PROMOTE_WORKER_URL", raising=False)
    monkeypatch.delenv("CYRIS_PROMOTE_TOKEN", raising=False)

    result = runner.invoke(app, ["promote-sync", *_paths(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Promotion sync not configured" in result.output


def test_triage_ui_starts_on_an_empty_d1(tmp_path: Path, d1: SqliteD1, monkeypatch) -> None:
    from test_cli_triage_ui import FakeServer

    FakeServer.calls = []
    monkeypatch.setattr("cyris.entrypoints.triage_server.TriageServer", FakeServer)

    result = runner.invoke(app, ["triage-ui", *_paths(tmp_path)])

    assert result.exit_code == 0, result.output
    [(_args, kwargs)] = FakeServer.calls
    assert kwargs["settings"] is not None


def test_sources_list_starts_on_an_empty_d1(tmp_path: Path, d1: SqliteD1) -> None:
    result = runner.invoke(app, ["sources", "list", *_paths(tmp_path)])

    assert result.exit_code == 0, result.output


def test_sources_push_fills_an_empty_d1(tmp_path: Path, d1: SqliteD1) -> None:
    from cyris.adapters.store.source_store import D1SourceStore

    result = runner.invoke(app, ["sources", "push", *_paths(tmp_path)])

    assert result.exit_code == 0, result.output
    assert set(D1SourceStore(d1).list_sources()) == {"Feed"}
