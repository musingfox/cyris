"""The type size is a required runtime setting with three steps."""

from pathlib import Path

import pytest
from fakes import SqliteD1, settings_toml
from typer.testing import CliRunner

from cyris.adapters.store.settings import D1Settings
from cyris.config import validate_setting
from cyris.entrypoints.cli import app

pytestmark = pytest.mark.integration

KEY = "digest.type_scale"
runner = CliRunner()


@pytest.mark.parametrize("value", [0.875, 1, 1.125])
def test_each_step_is_accepted(value) -> None:
    assert validate_setting(KEY, value) == value


@pytest.mark.parametrize("value", ["1.125", 1.25, 0.9, 2])
def test_anything_else_is_refused(value) -> None:
    with pytest.raises(ValueError):
        validate_setting(KEY, value)


def test_no_value_is_refused_as_required() -> None:
    with pytest.raises(ValueError, match=r"^digest\.type_scale is required$"):
        validate_setting(KEY, None)


def test_run_stops_naming_it_when_cyris_toml_lacks_it(tmp_path: Path, caplog, monkeypatch) -> None:
    monkeypatch.delenv("CYRIS_STORE_BACKEND", raising=False)
    built: list[object] = []

    def fake_build_deps(cfg, *args, **kwargs):
        built.append(cfg)
        raise RuntimeError("the run went past the settings gate")

    monkeypatch.setattr("cyris.bootstrap.build_deps", fake_build_deps)
    config_path = tmp_path / "cyris.toml"
    config_path.write_text(
        f'[agent_vault]\npath = "{tmp_path / "vault"}"\n\n' + settings_toml(omit=[KEY])
    )
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text('sources:\n  - name: "Feed"\n    url: "https://a.test/feed"\n')

    result = runner.invoke(
        app, ["run", "--config", str(config_path), "--sources", str(sources_path)]
    )

    assert result.exit_code == 1
    assert KEY in caplog.text
    assert built == []


def test_settings_push_adds_it_from_a_file(tmp_path: Path, d1: SqliteD1) -> None:
    config_path = tmp_path / "cyris.toml"
    config_path.write_text("[digest]\ntype_scale = 1\n")

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

    assert result.exit_code == 0, result.output
    assert "  added    digest.type_scale = 1" in result.stdout.splitlines()
    assert D1Settings(d1).all()[KEY] == 1
