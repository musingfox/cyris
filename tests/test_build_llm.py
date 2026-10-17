"""Which adapter `build_llm` picks, and when it declines to pick one."""

import json

import httpx
import pytest
import respx

from cyris.adapters.ai_gateway_client import AIGatewayClient
from cyris.adapters.anthropic_client import AnthropicClient
from cyris.adapters.gemini_client import GeminiClient
from cyris.adapters.openai_client import OpenAIClient
from cyris.adapters.workers_ai_client import WorkersAIClient
from cyris.bootstrap import _reasoning_data, _reasoning_defaults, build_llm, default_models
from cyris.config import LLMProviderConfig

pytestmark = pytest.mark.integration


def test_anthropic_and_gemini_still_route_to_their_own_clients():
    anthropic = build_llm(LLMProviderConfig(provider="anthropic", model="", api_key="k"))
    gemini = build_llm(LLMProviderConfig(provider="gemini", model="", api_key="k"))

    assert isinstance(anthropic, AnthropicClient)
    assert isinstance(gemini, GeminiClient)


def test_openai_routes_to_its_client_and_defaults_to_luna():
    llm = build_llm(LLMProviderConfig(provider="openai", model="", api_key="k"))

    assert isinstance(llm, OpenAIClient)
    assert llm.model == "gpt-5.6-luna"


def test_workers_ai_defaults_to_gpt_oss():
    """llama-3.3's 24k context has no room for a busy window's filter batch."""
    llm = build_llm(LLMProviderConfig(provider="workers_ai", model="", api_key="k", account_id="a"))

    assert isinstance(llm, WorkersAIClient)
    assert llm.model == "@cf/openai/gpt-oss-120b"


def test_workers_ai_honours_an_explicit_model():
    llm = build_llm(
        LLMProviderConfig(
            provider="workers_ai",
            api_key="k",
            account_id="a",
            model="@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        )
    )

    assert llm.model == "@cf/meta/llama-3.3-70b-instruct-fp8-fast"


def test_workers_ai_without_an_account_id_degrades_rather_than_building_a_broken_url(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)

    assert build_llm(LLMProviderConfig(provider="workers_ai", model="", api_key="k")) is None


def test_provider_none_builds_no_client_even_with_a_key():
    """The key is passed explicitly, so the empty-key early return cannot mask it."""
    assert build_llm(LLMProviderConfig(provider="none", model="", api_key="k")) is None


def test_gemini_with_an_empty_model_still_builds_its_client():
    assert isinstance(
        build_llm(LLMProviderConfig(provider="gemini", model="", api_key="k")), GeminiClient
    )


def test_gemini_honours_an_explicit_model():
    gemini = build_llm(LLMProviderConfig(provider="gemini", model="gemini-2.5-pro", api_key="k"))

    assert isinstance(gemini, GeminiClient)
    assert gemini.model == "gemini-2.5-pro"


def test_ai_gateway_defaults_to_the_model_production_runs():
    llm = build_llm(LLMProviderConfig(provider="ai_gateway", model="", api_key="k", account_id="a"))

    assert isinstance(llm, AIGatewayClient)
    assert llm.model == "google/gemini-3.8-flash"


def test_ai_gateway_without_an_account_id_builds_nothing(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)

    assert build_llm(LLMProviderConfig(provider="ai_gateway", model="", api_key="k")) is None


# One body every provider's parser reads: the adapters differ only in which keys they look at.
_REPLY = {
    "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    "candidates": [{"content": {"parts": [{"text": "{}"}], "role": "model"}}],
    "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
    "id": "msg_test",
    "type": "message",
    "role": "assistant",
    "model": "m",
    "content": [{"type": "text", "text": "{}"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
}
_REPLY = {**_REPLY, "success": True, "errors": [], "result": dict(_REPLY)}

_LEVEL = {
    "openai": lambda b: b.get("reasoning_effort"),
    "workers_ai": lambda b: b.get("reasoning_effort"),
    "gemini": lambda b: b["generationConfig"].get("thinkingConfig"),
    "ai_gateway": lambda b: b["generationConfig"].get("thinkingConfig"),
    "anthropic": lambda b: b.get("output_config"),
}


@pytest.mark.parametrize(
    ("provider", "effort", "sent"),
    [
        ("openai", "default", "low"),
        ("openai", "high", "high"),
        ("gemini", "default", None),
        ("gemini", "low", {"thinkingBudget": 1024}),
        ("ai_gateway", "default", None),
        ("ai_gateway", "medium", {"thinkingLevel": "MEDIUM"}),
        ("anthropic", "default", None),
        ("anthropic", "high", {"effort": "high"}),
        ("workers_ai", "high", "low"),
        ("workers_ai", "default", "low"),
    ],
)
async def test_built_client_sends_the_level_the_setting_names(provider, effort, sent):
    cfg = LLMProviderConfig(
        provider=provider, model="", api_key="k", account_id="a", reasoning_effort=effort
    )
    llm = build_llm(cfg)

    async with respx.mock:
        route = respx.post(url__regex=r".*").mock(return_value=httpx.Response(200, json=_REPLY))
        await llm.complete("Hi.")

    assert route.call_count == 1
    assert _LEVEL[provider](json.loads(route.calls[0].request.content)) == sent
    assert cfg.reasoning_effort == effort


@pytest.mark.parametrize(
    ("provider", "model", "effort", "sent"),
    [
        ("gemini", "gemini-2.5-flash", "low", {"thinkingBudget": 1024}),
        ("gemini", "", "high", {"thinkingBudget": 24576}),
        ("ai_gateway", "google/gemini-2.5-flash", "medium", {"thinkingBudget": 8192}),
        ("gemini", "gemini-3.8-flash", "low", {"thinkingLevel": "LOW"}),
        ("gemini", "gemini-2.5-flash", "default", None),
        ("ai_gateway", "google/gemini-2.5-flash", "default", None),
    ],
)
async def test_gemini_2_5_takes_a_thinking_budget_not_a_thinking_level(
    provider, model, effort, sent
):
    """Gemini 2.5 rejects thinkingLevel with a 400; Gemini 3 rejects thinkingBudget."""
    llm = build_llm(
        LLMProviderConfig(
            provider=provider, model=model, api_key="k", account_id="a", reasoning_effort=effort
        )
    )

    async with respx.mock:
        route = respx.post(url__regex=r".*").mock(return_value=httpx.Response(200, json=_REPLY))
        await llm.complete("Hi.")

    assert route.call_count == 1
    assert _LEVEL[provider](json.loads(route.calls[0].request.content)) == sent


def test_every_wirable_provider_has_a_reasoning_default_or_is_fixed():
    assert {*_reasoning_defaults(), "workers_ai"} == set(default_models())


def test_the_thinking_budget_block_covers_every_explicit_level():
    spec = _reasoning_data()["gemini_thinking_budget"]

    assert set(spec["budgets"]) == {"low", "medium", "high"}
    assert spec["model_prefixes"]
