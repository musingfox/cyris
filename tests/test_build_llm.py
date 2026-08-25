"""Which adapter `build_llm` picks, and when it declines to pick one."""

from cyris.adapters.anthropic_client import AnthropicClient
from cyris.adapters.gemini_client import GeminiClient
from cyris.adapters.workers_ai_client import WorkersAIClient
from cyris.bootstrap import build_llm
from cyris.config import LLMProviderConfig


def test_no_provider_means_degraded_mode():
    assert build_llm(LLMProviderConfig()) is None


def test_anthropic_and_gemini_still_route_to_their_own_clients():
    anthropic = build_llm(LLMProviderConfig(provider="anthropic", api_key="k"))
    gemini = build_llm(LLMProviderConfig(provider="gemini", api_key="k"))

    assert isinstance(anthropic, AnthropicClient)
    assert isinstance(gemini, GeminiClient)


def test_workers_ai_defaults_to_gpt_oss():
    """llama-3.3's 24k context has no room for a busy window's filter batch."""
    llm = build_llm(LLMProviderConfig(provider="workers_ai", api_key="k", account_id="a"))

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

    assert build_llm(LLMProviderConfig(provider="workers_ai", api_key="k")) is None
