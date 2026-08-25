"""Tests for the OpenAI chat-completions adapter."""

import json

import httpx
import pytest
import respx

from cyris.adapters.openai_client import OpenAIClient

URL = "https://api.openai.com/v1/chat/completions"


def _client(model: str = "gpt-5.6-luna") -> OpenAIClient:
    return OpenAIClient(api_key="test-openai-key", model=model)


def _ok(content: str = '{"items": []}', finish: str = "stop") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 145, "completion_tokens": 207},
        },
    )


async def test_parses_content_and_usage():
    async with respx.mock:
        route = respx.post(URL).mock(return_value=_ok())
        response = await _client().complete("Summarise.", system="Be brief.", temperature=0.5)

    assert response.text == '{"items": []}'
    assert response.input_tokens == 145
    assert response.output_tokens == 207

    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer test-openai-key"
    body = json.loads(request.content)
    assert body["model"] == "gpt-5.6-luna"
    assert body["messages"] == [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "Summarise."},
    ]
    assert body["temperature"] == 0.5


async def test_sends_max_completion_tokens_not_the_deprecated_name():
    """`max_tokens` is deprecated and rejected outright by the reasoning models."""
    async with respx.mock:
        route = respx.post(URL).mock(return_value=_ok())
        await _client().complete("Hi.")

    body = json.loads(route.calls[0].request.content)
    assert body["max_completion_tokens"] == 16384
    assert "max_tokens" not in body


async def test_explicit_max_tokens_maps_onto_the_current_parameter():
    async with respx.mock:
        route = respx.post(URL).mock(return_value=_ok())
        await _client().complete("Hi.", max_tokens=512)

    assert json.loads(route.calls[0].request.content)["max_completion_tokens"] == 512


async def test_asks_for_low_reasoning_effort_and_json():
    """Load-bearing: on Workers AI, unbounded reasoning ate a whole budget and
    returned an empty message. Same class of model, same precaution."""
    async with respx.mock:
        route = respx.post(URL).mock(return_value=_ok())
        await _client().complete("Hi.")

    body = json.loads(route.calls[0].request.content)
    assert body["reasoning_effort"] == "low"
    assert body["response_format"] == {"type": "json_object"}


async def test_warns_when_truncated(caplog):
    async with respx.mock:
        respx.post(URL).mock(return_value=_ok(content='{"a":', finish="length"))
        await _client().complete("Hi.")

    assert "truncated" in caplog.text


async def test_warns_when_the_reply_is_empty(caplog):
    async with respx.mock:
        respx.post(URL).mock(return_value=_ok(content="", finish="length"))
        response = await _client().complete("Hi.")

    assert response.text == ""
    assert "returned no text" in caplog.text


async def test_retries_a_429_then_succeeds():
    async with respx.mock:
        route = respx.post(URL).mock(side_effect=[httpx.Response(429), _ok()])
        response = await _client().complete("Hi.")

    assert route.call_count == 2
    assert response.text == '{"items": []}'


async def test_a_4xx_reason_reaches_the_caller():
    """raise_for_status() would report the URL and nothing about why."""
    body = {"error": {"message": "The model `gpt-5.6-lunar` does not exist"}}
    async with respx.mock:
        route = respx.post(URL).mock(return_value=httpx.Response(404, json=body))
        with pytest.raises(RuntimeError, match="does not exist"):
            await _client("gpt-5.6-lunar").complete("Hi.")

    assert route.call_count == 1  # a wrong request is not retried
