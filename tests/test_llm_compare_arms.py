"""`cyris llm-compare --arm provider:model` — what it accepts and how it refuses."""

import pytest
import typer

from cyris.adapters.anthropic_client import AnthropicClient
from cyris.adapters.workers_ai_client import WorkersAIClient
from cyris.entrypoints.cli import _build_arm


def test_builds_an_arm_from_provider_and_model(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    label, llm = _build_arm("anthropic:claude-haiku-4-5")

    assert isinstance(llm, AnthropicClient)
    assert label == "anthropic:claude-haiku-4-5"


def test_an_omitted_model_means_the_provider_default(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    label, llm = _build_arm("anthropic")

    assert llm.model == "claude-sonnet-4-6"
    assert label == "anthropic:claude-sonnet-4-6"  # the label names what actually ran


def test_a_workers_ai_arm_picks_up_the_cloudflare_environment(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_AI_TOKEN", "t")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")

    _, llm = _build_arm("workers_ai:@cf/openai/gpt-oss-120b")

    assert isinstance(llm, WorkersAIClient)


def test_an_unknown_provider_is_refused_by_name():
    with pytest.raises(typer.BadParameter, match="unknown provider 'openaii'"):
        _build_arm("openaii:gpt-5.6-luna")


def test_a_missing_key_names_the_variable(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(typer.BadParameter, match="ANTHROPIC_API_KEY is empty"):
        _build_arm("anthropic:claude-haiku-4-5")


def test_workers_ai_with_a_token_but_no_account_says_which_one(monkeypatch):
    """Naming the key here would send the reader to look at a variable that is fine."""
    monkeypatch.setenv("CLOUDFLARE_AI_TOKEN", "t")
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)

    with pytest.raises(typer.BadParameter, match="CLOUDFLARE_ACCOUNT_ID is empty"):
        _build_arm("workers_ai:@cf/openai/gpt-oss-120b")
