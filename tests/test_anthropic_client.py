"""Tests for the Anthropic Messages adapter."""

import json

import httpx
import pytest
import respx

from cyris.adapters.anthropic_client import AnthropicClient

pytestmark = pytest.mark.unit

URL = "https://api.anthropic.com/v1/messages"


def _client() -> AnthropicClient:
    return AnthropicClient(api_key="test-anthropic-key", model="claude-sonnet-4-6", max_retries=0)


def _ok(content: list[dict], stop_reason: str = "end_turn") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 145, "output_tokens": 207},
        },
    )


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


async def test_parses_text_and_usage():
    async with respx.mock:
        route = respx.post(URL).mock(return_value=_ok([_text('{"items": []}')]))
        response = await _client().complete("Hi.")

    assert route.call_count == 1
    request = route.calls.last.request
    assert request.headers["x-api-key"] == "test-anthropic-key"
    body = json.loads(request.content)
    assert body["model"] == "claude-sonnet-4-6"
    assert body["messages"] == [{"role": "user", "content": "Hi."}]
    assert response.text == '{"items": []}'
    assert response.input_tokens == 145
    assert response.output_tokens == 207


async def test_reads_past_a_leading_thinking_block():
    # Models that think by default put a thinking block first, and it has no text.
    thinking = {"type": "thinking", "thinking": "", "signature": "sig"}
    async with respx.mock:
        route = respx.post(URL).mock(return_value=_ok([thinking, _text('{"a": 1}')]))
        response = await _client().complete("Hi.")

    assert route.call_count == 1
    assert response.text == '{"a": 1}'


async def test_joins_every_text_block():
    async with respx.mock:
        route = respx.post(URL).mock(return_value=_ok([_text('{"a": '), _text("1}")]))
        response = await _client().complete("Hi.")

    assert route.call_count == 1
    assert response.text == '{"a": 1}'


async def test_warns_when_truncated(caplog):
    async with respx.mock:
        route = respx.post(URL).mock(return_value=_ok([_text('{"a":')], stop_reason="max_tokens"))
        await _client().complete("Hi.")

    assert route.call_count == 1
    assert "truncated" in caplog.text


async def test_warns_on_a_refusal(caplog):
    async with respx.mock:
        route = respx.post(URL).mock(return_value=_ok([], stop_reason="refusal"))
        response = await _client().complete("Hi.")

    assert route.call_count == 1
    assert response.text == ""
    assert "refused" in caplog.text
