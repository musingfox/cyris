"""The clients the CLI builds outside `build_deps` run at the configured reasoning effort."""

from types import SimpleNamespace

import pytest
from fakes import settings_toml
from typer.testing import CliRunner

from cyris.entrypoints.cli import _build_arm, _llm_from_values, app

pytestmark = pytest.mark.integration

KEY = "llm_provider.reasoning_effort"


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> list:
    """Every LLMProviderConfig handed to `build_llm`; each call returns a stub client."""
    configs: list = []

    def record(cfg):
        configs.append(cfg)
        return SimpleNamespace(model=cfg.model or "stub-model")

    monkeypatch.setattr("cyris.bootstrap.build_llm", record)
    return configs


def test_an_arm_runs_at_the_given_effort(built) -> None:
    _build_arm("openai:gpt-6-luna", "high")

    [cfg] = built
    assert cfg.provider == "openai"
    assert cfg.reasoning_effort == "high"


def test_llm_compare_builds_every_arm_at_the_configured_effort(
    tmp_path, monkeypatch: pytest.MonkeyPatch, built
) -> None:
    monkeypatch.setenv("CYRIS_STORE_BACKEND", "json")
    config = tmp_path / "cyris.toml"
    config.write_text(
        settings_toml(**{"llm_provider.provider": "openai", KEY: "high"})
        + "[html_output]\nenabled = true\n"
        f'[agent_vault]\npath = "{tmp_path / "vault"}"\n'
    )
    sources = tmp_path / "sources.yaml"
    sources.write_text('sources:\n  - name: "Feed"\n    url: "https://a.test/feed"\n')

    result = CliRunner().invoke(
        app,
        [
            "llm-compare",
            "--arm",
            "anthropic:claude-haiku-5-5",
            "--config",
            str(config),
            "--sources",
            str(sources),
        ],
    )

    assert result.exit_code == 1, result.output
    assert [(cfg.provider, cfg.reasoning_effort) for cfg in built] == [
        ("openai", "high"),
        ("anthropic", "high"),
    ]


def test_a_saved_effort_reaches_the_client_the_settings_page_builds(built) -> None:
    _llm_from_values({"llm_provider.provider": "openai", "llm_provider.model": "", KEY: "medium"})

    [cfg] = built
    assert cfg.reasoning_effort == "medium"


def test_a_page_without_a_saved_effort_builds_at_default(built) -> None:
    _llm_from_values({"llm_provider.provider": "openai", "llm_provider.model": ""})

    [cfg] = built
    assert cfg.reasoning_effort == "default"
