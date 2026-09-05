"""`cyris llm-compare --arm provider:model` — what it accepts and how it refuses."""

import ast
from dataclasses import fields
from pathlib import Path

import pytest
import typer

from cyris.adapters.anthropic_client import AnthropicClient
from cyris.adapters.workers_ai_client import WorkersAIClient
from cyris.bootstrap import Deps
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


def _deps_attrs(source: str) -> set[str]:
    tree = ast.parse(source)
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "deps"
    }


def test_the_cli_only_reads_fields_that_exist_on_deps():
    """`deps.writer.render` survived the vault's deletion for four days.

    Nothing referenced it in a test — `tests/test_compare.py` injects its own
    `render`, so every test passed while `cyris llm-compare` raised
    AttributeError before its first arm ran. A rename is the whole failure mode,
    so the check is structural: every `deps.<name>` in the CLI must be a field
    on the container the composition root actually returns.
    """
    known = {f.name for f in fields(Deps)}

    read = _deps_attrs(Path("src/cyris/entrypoints/cli.py").read_text())

    assert read <= known, f"not on Deps: {sorted(read - known)}"


def test_a_field_that_no_longer_exists_is_named():
    known = {f.name for f in fields(Deps)}

    read = _deps_attrs("rows = compare(render=deps.writer.render)\n")

    assert sorted(read - known) == ["writer"]
