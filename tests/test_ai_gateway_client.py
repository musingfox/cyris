"""Tests for the AI Gateway adapter: one `POST /ai/run` envelope, BYOK keys held by the gateway.

Every response body here is copied from Cloudflare's docs or from the ticket's measurement,
never invented; the source sits beside each one.
"""

import json

import httpx
import pytest
import respx

from cyris.adapters.ai_gateway_client import AIGatewayClient, AIGatewayError
from cyris.domain.models import UsageStats

pytestmark = pytest.mark.unit

ACCOUNT = "acct-123"
MODEL = "google/gemini-3.8-flash"
RUN_URL = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/ai/run"

# The `/ai/run` response for `google/gemini-3-flash` on the model catalog page,
# https://developers.cloudflare.com/ai/models/google/gemini-3-flash/ — text shortened,
# `thoughtSignature` dropped, every usage number kept as published.
CATALOG_GEMINI_RESPONSE = {
    "candidates": [
        {
            "content": {"role": "model", "parts": [{"text": '{"ok": true}'}]},
            "finishReason": "STOP",
        }
    ],
    "usageMetadata": {
        "promptTokenCount": 8,
        "candidatesTokenCount": 697,
        "totalTokenCount": 1107,
        "trafficType": "ON_DEMAND",
        "promptTokensDetails": [{"modality": "TEXT", "tokenCount": 8}],
        "candidatesTokensDetails": [{"modality": "TEXT", "tokenCount": 697}],
        "thoughtsTokenCount": 402,
    },
    "modelVersion": "gemini-3-flash-preview",
    "createTime": "2026-07-24T21:49:35.132185Z",
    "responseId": "791jatmICOSg3dAPo6eioAg",
    "gatewayMetadata": {"keySource": "Unified"},
}


def _client(model: str = MODEL) -> AIGatewayClient:
    return AIGatewayClient(api_token="test-cf-token", account_id=ACCOUNT, model=model)


def _cloudflare_error(status: int, code: int, message: str) -> httpx.Response:
    """Cloudflare's v4 error body, as the AI Gateway docs show it for their own refusals."""
    return httpx.Response(
        status, json={"success": False, "errors": [{"code": code, "message": message}]}
    )


async def test_the_catalog_response_yields_its_text_and_token_usage():
    async with respx.mock:
        route = respx.post(RUN_URL).mock(
            return_value=httpx.Response(200, json=CATALOG_GEMINI_RESPONSE)
        )
        response = await _client().complete("Summarise.", system="Be brief.", temperature=0.5)

    assert route.call_count == 1
    assert response.text == '{"ok": true}'
    assert response.input_tokens == 8
    # Thinking is billed as output, the way GeminiClient counts it: 697 + 402.
    assert response.output_tokens == 1099
    assert response.neurons is None


async def test_the_request_is_the_documented_envelope_and_nothing_else():
    """No provider key and no gateway id: BYOK supplies the one, `default` is the other."""
    async with respx.mock:
        route = respx.post(RUN_URL).mock(
            return_value=httpx.Response(200, json=CATALOG_GEMINI_RESPONSE)
        )
        await _client().complete("Summarise.", system="Be brief.", temperature=0.5)

    assert route.call_count == 1
    request = route.calls[0].request
    assert str(request.url) == RUN_URL
    assert request.headers["authorization"] == "Bearer test-cf-token"
    assert request.headers["content-type"] == "application/json"
    assert not [name for name in request.headers if name.lower().startswith("cf-aig-")]
    assert "x-goog-api-key" not in request.headers
    assert json.loads(request.content) == {
        "model": "google/gemini-3.8-flash",
        "input": {
            "contents": [{"role": "user", "parts": [{"text": "Summarise."}]}],
            "systemInstruction": {"parts": [{"text": "Be brief."}]},
            "generationConfig": {
                "maxOutputTokens": 65536,
                "responseMimeType": "application/json",
                "temperature": 0.5,
            },
        },
    }


async def test_without_a_system_prompt_or_temperature_neither_is_sent():
    async with respx.mock:
        route = respx.post(RUN_URL).mock(
            return_value=httpx.Response(200, json=CATALOG_GEMINI_RESPONSE)
        )
        await _client().complete("Hi.", max_tokens=128)

    assert route.call_count == 1
    assert json.loads(route.calls[0].request.content)["input"] == {
        "contents": [{"role": "user", "parts": [{"text": "Hi."}]}],
        "generationConfig": {"maxOutputTokens": 128, "responseMimeType": "application/json"},
    }


async def test_a_response_inside_cloudflares_result_wrapper_reads_the_same():
    """The docs show the catalog body bare, and `/ai/run` for a `@cf/` model wrapped.

    Which one a third-party model gets over REST is not stated, so both must parse.
    """
    wrapped = {"result": CATALOG_GEMINI_RESPONSE, "success": True, "errors": [], "messages": []}
    async with respx.mock:
        route = respx.post(RUN_URL).mock(return_value=httpx.Response(200, json=wrapped))
        response = await _client().complete("Hi.")

    assert route.call_count == 1
    assert (response.text, response.input_tokens, response.output_tokens) == (
        '{"ok": true}',
        8,
        1099,
    )


async def test_usage_reaches_the_run_total():
    usage = UsageStats(model=MODEL)
    async with respx.mock:
        route = respx.post(RUN_URL).mock(
            return_value=httpx.Response(200, json=CATALOG_GEMINI_RESPONSE)
        )
        r = await _client().complete("Hi.")
        usage.add(r.input_tokens, r.output_tokens, r.neurons)

    assert route.call_count == 1
    assert (usage.input_tokens, usage.output_tokens) == (8, 1099)


@pytest.mark.parametrize(
    ("status", "code", "message", "side"),
    [
        # Measured 2026-09-21 (ticket ai-gateway-provider-byok): a google model with no
        # key stored in the gateway, whatever provider header the request carried.
        (402, 2021, "Insufficient balance; add money to your gateway or use BYOK", "gateway"),
        # https://developers.cloudflare.com/ai-gateway/usage/rest-api/#authentication
        (401, 10000, "Authentication error", "gateway"),
        # Measured 2026-09-21: the catalog is partial, and Cloudflare retires versions.
        (404, 7003, "Model not found", "model"),
        (410, 7010, "Model is deprecated, use anthropic/claude-sonnet-4.6", "model"),
    ],
)
async def test_a_refusal_names_which_side_refused_and_is_never_retried(
    backoff_sleeps, status, code, message, side
):
    async with respx.mock:
        route = respx.post(RUN_URL).mock(return_value=_cloudflare_error(status, code, message))
        with pytest.raises(AIGatewayError) as raised:
            await _client().complete("Hi.")

    assert route.call_count == 1
    assert backoff_sleeps == []
    error = raised.value
    assert (error.status, error.code, error.side) == (status, code, side)
    assert error.message == message
    assert message in str(error)
    assert ("AI Gateway refused" if side == "gateway" else "refused the model") in str(error)
    assert MODEL in str(error)


async def test_a_refusal_with_no_cloudflare_error_body_keeps_what_the_body_said(backoff_sleeps):
    async with respx.mock:
        route = respx.post(RUN_URL).mock(return_value=httpx.Response(400, text="bad input shape"))
        with pytest.raises(AIGatewayError) as raised:
            await _client().complete("Hi.")

    assert route.call_count == 1
    assert raised.value.side == "unknown"
    assert raised.value.code is None
    assert "bad input shape" in str(raised.value)


async def test_a_server_error_is_retried_then_answered(backoff_sleeps):
    async with respx.mock:
        route = respx.post(RUN_URL).mock(
            side_effect=[
                httpx.Response(503, text="unavailable"),
                httpx.Response(200, json=CATALOG_GEMINI_RESPONSE),
            ]
        )
        response = await _client().complete("Hi.")

    assert route.call_count == 2
    assert backoff_sleeps == [1]
    assert response.input_tokens == 8


async def test_a_reply_with_no_candidates_says_why():
    blocked = {"promptFeedback": {"blockReason": "SAFETY"}, "usageMetadata": {}}
    async with respx.mock:
        route = respx.post(RUN_URL).mock(return_value=httpx.Response(200, json=blocked))
        with pytest.raises(RuntimeError, match="google/gemini-3.8-flash.*SAFETY"):
            await _client().complete("Hi.")

    assert route.call_count == 1


async def test_an_author_this_adapter_cannot_parse_is_refused_before_any_request():
    """Only the google body is documented end to end under `/ai/run`.

    Guessing another author's usage field would report zero tokens, which reads as a
    measured run that cost nothing.
    """
    async with respx.mock:
        route = respx.post(RUN_URL).mock(return_value=httpx.Response(200, json={}))
        with pytest.raises(ValueError, match="anthropic/claude-sonnet-4.6.*google/"):
            await _client("anthropic/claude-sonnet-4.6").complete("Hi.")

    assert route.call_count == 0
