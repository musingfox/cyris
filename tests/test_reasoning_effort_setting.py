"""The reasoning-effort setting is optional: a home without one runs at "default"."""

from pathlib import Path

import pytest
from fakes import SqliteD1, seed_d1_settings, settings_toml
from pydantic import ValidationError

from cyris.bootstrap import load_effective_config
from cyris.config import (
    IncompleteSettingsError,
    LLMProviderConfig,
    load_config,
    validate_setting,
)

pytestmark = pytest.mark.integration

KEY = "llm_provider.reasoning_effort"


def _d1_config(tmp_path: Path):
    return load_effective_config(tmp_path / "nope.toml", tmp_path / "nope.yaml")


def test_a_d1_home_without_the_effort_runs_at_default(tmp_path: Path, d1: SqliteD1) -> None:
    seed_d1_settings(
        d1, omit=[KEY], **{"llm_provider.provider": "openai", "llm_provider.model": "gpt-6-luna"}
    )

    cfg = _d1_config(tmp_path)

    assert cfg.missing_settings == []
    assert cfg.app.llm_provider.provider == "openai"
    assert cfg.app.llm_provider.model == "gpt-6-luna"
    assert cfg.app.llm_provider.reasoning_effort == "default"


def test_a_defaulted_effort_is_reported_apart_from_the_stored_settings(
    tmp_path: Path, d1: SqliteD1
) -> None:
    seed_d1_settings(d1, omit=[KEY])

    cfg = _d1_config(tmp_path)

    assert cfg.defaulted_settings() == {KEY: "default"}
    assert KEY not in cfg.present_settings()
    assert cfg.require_complete_settings() is None


def test_a_json_home_without_the_effort_runs_at_default(tmp_path: Path) -> None:
    (tmp_path / "cyris.toml").write_text(settings_toml(omit=[KEY]))

    cfg = load_config(tmp_path / "cyris.toml", tmp_path / "sources.yaml")

    assert cfg.missing_settings == []
    assert cfg.app.llm_provider.reasoning_effort == "default"
    assert cfg.defaulted_settings() == {KEY: "default"}


@pytest.mark.parametrize("value", ["default", "low", "medium", "high"])
def test_each_of_the_four_efforts_is_a_valid_setting(value: str) -> None:
    assert validate_setting(KEY, value) == value


@pytest.mark.parametrize("value", ["xhigh", "none", "minimal", "LOW", ""])
def test_any_other_effort_is_refused(value: str) -> None:
    with pytest.raises(ValueError):
        validate_setting(KEY, value)


def test_an_effort_of_none_is_refused_as_a_missing_value() -> None:
    with pytest.raises(ValueError, match="a value is required"):
        validate_setting(KEY, None)


def test_a_provider_config_defaults_the_effort() -> None:
    assert LLMProviderConfig(provider="openai", model="").reasoning_effort == "default"


def test_a_provider_config_refuses_an_unknown_effort() -> None:
    with pytest.raises(ValidationError):
        LLMProviderConfig(provider="openai", model="", reasoning_effort="max")


def test_a_stored_effort_outside_the_four_stops_the_run_naming_it(
    tmp_path: Path, d1: SqliteD1, caplog: pytest.LogCaptureFixture
) -> None:
    seed_d1_settings(d1, **{KEY: "xhigh"})

    cfg = _d1_config(tmp_path)

    assert cfg.missing_settings == [KEY]
    assert cfg.app.llm_provider is None
    assert cfg.defaulted_settings() == {}
    assert "Ignoring D1 setting llm_provider.reasoning_effort" in caplog.text
    with pytest.raises(IncompleteSettingsError) as caught:
        cfg.require_complete_settings()
    assert str(caught.value) == (
        "Missing settings in D1: llm_provider.reasoning_effort. "
        "Set them on /settings, or run `cyris settings push` to copy them from cyris.toml."
    )


def test_a_file_effort_outside_the_four_fails_the_load(tmp_path: Path) -> None:
    (tmp_path / "cyris.toml").write_text(settings_toml(**{KEY: "xhigh"}))

    with pytest.raises(ValueError):
        load_config(tmp_path / "cyris.toml", tmp_path / "sources.yaml")


def test_a_stored_effort_reaches_the_config(tmp_path: Path, d1: SqliteD1) -> None:
    seed_d1_settings(d1, **{KEY: "high"})

    cfg = _d1_config(tmp_path)

    assert cfg.app.llm_provider.reasoning_effort == "high"
    assert cfg.present_settings()[KEY] == "high"


def test_a_d1_deployment_ignores_the_files_effort_when_d1_holds_one(
    tmp_path: Path, d1: SqliteD1
) -> None:
    seed_d1_settings(d1, **{KEY: "low"})
    (tmp_path / "cyris.toml").write_text(settings_toml(**{KEY: "high"}))

    cfg = load_effective_config(tmp_path / "cyris.toml", tmp_path / "sources.yaml")

    assert cfg.app.llm_provider.reasoning_effort == "low"


def test_a_d1_deployment_ignores_the_files_effort_when_d1_holds_none(
    tmp_path: Path, d1: SqliteD1
) -> None:
    seed_d1_settings(d1, omit=[KEY])
    (tmp_path / "cyris.toml").write_text(settings_toml(**{KEY: "high"}))

    cfg = load_effective_config(tmp_path / "cyris.toml", tmp_path / "sources.yaml")

    assert cfg.app.llm_provider.reasoning_effort == "default"


def test_a_json_deployment_reads_the_effort_from_its_file(tmp_path: Path) -> None:
    (tmp_path / "cyris.toml").write_text(settings_toml(**{KEY: "medium"}))

    cfg = load_config(tmp_path / "cyris.toml", tmp_path / "sources.yaml")

    assert cfg.app.llm_provider.reasoning_effort == "medium"
