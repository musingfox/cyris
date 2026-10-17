"""Tests for the AI Gateway adapter: one native google-ai-studio call, BYOK held by the gateway.

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
RUN_URL = (
    f"https://gateway.ai.cloudflare.com/v1/{ACCOUNT}/default/google-ai-studio"
    "/v1beta/models/gemini-3.8-flash:generateContent"
)

# A Gemini generateContent response as the model catalog page shows it,
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


def _gateway_error(status: int, code: int, message: str) -> httpx.Response:
    """The gateway's own refusal body, measured 2026-10-16 for a token without Run."""
    return httpx.Response(
        status,
        json={
            "success": False,
            "result": [],
            "messages": [],
            "error": [{"code": code, "message": message}],
            "name": "AiGatewayError",
            "httpCode": status,
            "internalCode": code,
            "message": message,
        },
    )


def _google_error(status: int, code: int, message: str, status_name: str) -> httpx.Response:
    """Google's refusal body passed through, measured 2026-10-16 for an unknown model."""
    return httpx.Response(
        status, json={"error": {"code": code, "message": message, "status": status_name}}
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


async def test_the_request_is_gemini_native_and_carries_only_the_gateway_token():
    """No provider key: BYOK supplies it, and a header for one would bypass the stored key."""
    async with respx.mock:
        route = respx.post(RUN_URL).mock(
            return_value=httpx.Response(200, json=CATALOG_GEMINI_RESPONSE)
        )
        await _client().complete("Summarise.", system="Be brief.", temperature=0.5)

    assert route.call_count == 1
    request = route.calls[0].request
    assert str(request.url) == RUN_URL
    assert request.headers["cf-aig-authorization"] == "Bearer test-cf-token"
    assert request.headers["content-type"] == "application/json"
    assert "authorization" not in request.headers
    assert "x-goog-api-key" not in request.headers
    assert "x-api-key" not in request.headers
    assert json.loads(request.content) == {
        "contents": [{"role": "user", "parts": [{"text": "Summarise."}]}],
        "systemInstruction": {"parts": [{"text": "Be brief."}]},
        "generationConfig": {
            "maxOutputTokens": 65536,
            "responseMimeType": "application/json",
            "temperature": 0.5,
        },
    }


async def test_without_a_system_prompt_or_temperature_neither_is_sent():
    async with respx.mock:
        route = respx.post(RUN_URL).mock(
            return_value=httpx.Response(200, json=CATALOG_GEMINI_RESPONSE)
        )
        await _client().complete("Hi.", max_tokens=128)

    assert route.call_count == 1
    assert json.loads(route.calls[0].request.content) == {
        "contents": [{"role": "user", "parts": [{"text": "Hi."}]}],
        "generationConfig": {"maxOutputTokens": 128, "responseMimeType": "application/json"},
    }


@pytest.mark.parametrize(
    ("effort", "expected"), [("medium", {"thinkingLevel": "MEDIUM"}), (None, None)]
)
async def test_the_thinking_level_is_sent_uppercase_or_not_at_all(effort, expected):
    client = AIGatewayClient(
        api_token="test-cf-token", account_id=ACCOUNT, model=MODEL, reasoning_effort=effort
    )
    async with respx.mock:
        route = respx.post(RUN_URL).mock(
            return_value=httpx.Response(200, json=CATALOG_GEMINI_RESPONSE)
        )
        await client.complete("Hi.")

    assert route.call_count == 1
    config = json.loads(route.calls[0].request.content)["generationConfig"]
    assert config.get("thinkingConfig") == expected
    assert ("thinkingConfig" in config) == (expected is not None)


async def test_thinking_budget_is_sent_as_a_budget_and_never_with_a_level():
    client = AIGatewayClient(
        api_token="test-cf-token", account_id=ACCOUNT, model=MODEL, thinking_budget=8192
    )
    async with respx.mock:
        route = respx.post(RUN_URL).mock(
            return_value=httpx.Response(200, json=CATALOG_GEMINI_RESPONSE)
        )
        await client.complete("Hi.")

    assert route.call_count == 1
    config = json.loads(route.calls[0].request.content)["generationConfig"]
    assert config["thinkingConfig"] == {"thinkingBudget": 8192}


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
    ("refusal", "side"),
    [
        (_gateway_error(401, 2009, "Unauthorized"), "gateway"),
        (
            _google_error(
                404,
                404,
                "models/gemini-3-pro is not found for API version v1beta, or is not "
                "supported for generateContent.",
                "NOT_FOUND",
            ),
            "model",
        ),
        (_google_error(400, 400, "API key not valid.", "INVALID_ARGUMENT"), "unknown"),
    ],
)
async def test_a_refusal_names_which_side_refused_and_is_never_retried(
    backoff_sleeps, refusal, side
):
    async with respx.mock:
        route = respx.post(RUN_URL).mock(return_value=refusal)
        with pytest.raises(AIGatewayError) as raised:
            await _client().complete("Hi.")

    assert route.call_count == 1
    assert backoff_sleeps == []
    error = raised.value
    body = refusal.json()["error"]
    body = body[0] if isinstance(body, list) else body
    assert (error.status, error.code, error.side) == (refusal.status_code, body["code"], side)
    assert error.message == body["message"]
    assert body["message"] in str(error)
    assert MODEL in str(error)


async def test_a_refusal_with_no_json_error_body_keeps_what_the_body_said(backoff_sleeps):
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
    """Only the google body is parsed end to end.

    Guessing another author's usage field would report zero tokens, which reads as a
    measured run that cost nothing.
    """
    async with respx.mock:
        route = respx.post(RUN_URL).mock(return_value=httpx.Response(200, json={}))
        with pytest.raises(ValueError, match="anthropic/claude-sonnet-4.6.*google/"):
            await _client("anthropic/claude-sonnet-4.6").complete("Hi.")

    assert route.call_count == 0
