"""`cyris settings push` copies what D1 lacks from a cyris.toml, and nothing else."""

from pathlib import Path

import pytest
from fakes import SqliteD1, settings_toml
from typer.testing import CliRunner

from cyris.adapters.store.settings import D1Settings
from cyris.entrypoints.cli import app

pytestmark = pytest.mark.integration

runner = CliRunner()

WEBHOOK = "https://discord.com/api/webhooks/123/abcTOKEN"


def _push(tmp_path: Path, toml: str):
    config_path = tmp_path / "cyris.toml"
    config_path.write_text(toml)
    result = runner.invoke(
        app,
        [
            "settings",
            "push",
            "--config",
            str(config_path),
            "--sources",
            str(tmp_path / "sources.yaml"),
        ],
    )
    return result, config_path


def test_an_empty_d1_gets_every_key_from_a_complete_file(tmp_path: Path, d1: SqliteD1) -> None:
    result, config_path = _push(tmp_path, settings_toml())

    assert result.exit_code == 0, result.output
    assert len(D1Settings(d1).all()) == 21
    assert result.stdout.rstrip().endswith(
        f"Added 21 of 21 settings from {config_path}; 0 kept; 0 still missing."
    )


def test_a_row_d1_holds_is_kept_whatever_the_file_says(tmp_path: Path, d1: SqliteD1) -> None:
    D1Settings(d1).set({"llm_provider.model": "gemini-3.8-flash"})

    result, _ = _push(tmp_path, settings_toml(**{"llm_provider.model": "gemini-3.7-flash"}))

    assert result.exit_code == 0, result.output
    assert D1Settings(d1).all()["llm_provider.model"] == "gemini-3.8-flash"
    assert '  kept     llm_provider.model (file has "gemini-3.7-flash")' in result.stdout


def test_an_invalid_file_value_writes_nothing(tmp_path: Path, d1: SqliteD1) -> None:
    result, config_path = _push(tmp_path, settings_toml(**{"digest.max_featured": 0}))

    assert result.exit_code == 1
    assert f"{config_path}: digest.max_featured: " in result.stderr
    assert D1Settings(d1).all() == {}


def test_an_undecodable_row_is_replaced_from_the_file(tmp_path: Path, d1: SqliteD1) -> None:
    d1.query(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
        ["digest.max_featured", '"not json', "2026-09-19T00:00:00+00:00"],
    )

    result, _ = _push(tmp_path, settings_toml())

    assert result.exit_code == 0, result.output
    assert D1Settings(d1).all()["digest.max_featured"] == 5
    assert "  added    digest.max_featured = 5" in result.stdout


def test_a_key_in_neither_home_is_reported_missing(tmp_path: Path, d1: SqliteD1) -> None:
    result, config_path = _push(tmp_path, settings_toml(omit=["digest.style_prompt"]))

    assert result.exit_code == 0, result.output
    assert f"  missing  digest.style_prompt — in neither D1 nor {config_path}" in result.stdout
    assert "1 still missing." in result.stdout


def test_provider_none_is_pushed_as_a_value(tmp_path: Path, d1: SqliteD1) -> None:
    result, _ = _push(tmp_path, settings_toml(**{"llm_provider.provider": "none"}))

    assert result.exit_code == 0, result.output
    assert D1Settings(d1).all()["llm_provider.provider"] == "none"
    assert '  added    llm_provider.provider = "none"' in result.stdout


def test_the_webhook_is_never_printed(tmp_path: Path, d1: SqliteD1) -> None:
    toml = settings_toml(**{"notify.discord_webhook_url": WEBHOOK})

    added, _ = _push(tmp_path, toml)
    D1Settings(d1).set({"notify.discord_webhook_url": "https://discord.com/api/webhooks/9/old"})
    kept, _ = _push(tmp_path, toml)

    assert "notify.discord_webhook_url = " in added.stdout
    assert "notify.discord_webhook_url (file has " in kept.stdout
    assert "abcTOKEN" not in added.output
    assert "abcTOKEN" not in kept.output


def test_the_first_line_names_the_database_it_bound_to(
    tmp_path: Path, d1: SqliteD1, monkeypatch
) -> None:
    constructed: list[dict] = []

    def fake_client(**kwargs):
        constructed.append(kwargs)
        return d1

    monkeypatch.setattr("cyris.adapters.store.d1.D1Client", fake_client)
    monkeypatch.setenv("CYRIS_STORE_DATABASE_ID", "env-db")

    result, _ = _push(tmp_path, '[store]\nbackend = "d1"\ndatabase_id = "file-db"\n\n')

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[0] == "D1 database file-db"
    assert constructed and all(kw["database_id"] == "file-db" for kw in constructed)


def test_a_d1_deployments_file_is_still_read(tmp_path: Path, d1: SqliteD1) -> None:
    """The load ignores these keys under d1; push reads the file itself."""
    result, _ = _push(tmp_path, '[store]\nbackend = "d1"\n\n' + settings_toml())

    assert result.exit_code == 0, result.output
    assert len(D1Settings(d1).all()) == 21
