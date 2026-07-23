"""Tests for the Gemini REST adapter."""

import json

import httpx
import pytest
import respx

from cyris.adapters.gemini_client import GeminiClient

GENERATE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
)


def _ok_response(text: str = "hello") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [{"content": {"parts": [{"text": text}], "role": "model"}}],
            "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 12},
        },
    )


async def test_complete_parses_text_and_usage():
    async with respx.mock:
        route = respx.post(GENERATE_URL).mock(return_value=_ok_response())
        client = GeminiClient(api_key="test-gemini-key", model="gemini-2.5-flash")
        result = await client.complete(
            "Tell me a joke.", system="Be brief.", max_tokens=100, temperature=0.5
        )

    assert result.text == "hello"
    assert result.input_tokens == 4
    assert result.output_tokens == 12

    request = route.calls[0].request
    assert request.headers["x-goog-api-key"] == "test-gemini-key"
    body = json.loads(request.content)
    assert body["contents"] == [{"parts": [{"text": "Tell me a joke."}]}]
    assert body["system_instruction"] == {"parts": [{"text": "Be brief."}]}
    assert body["generationConfig"] == {
        "maxOutputTokens": 100,
        "responseMimeType": "application/json",
        "temperature": 0.5,
    }


async def test_complete_omits_optional_fields():
    async with respx.mock:
        route = respx.post(GENERATE_URL).mock(return_value=_ok_response())
        client = GeminiClient(api_key="k", model="gemini-2.5-flash")
        await client.complete("hi")

    body = json.loads(route.calls[0].request.content)
    assert "system_instruction" not in body
    assert body["generationConfig"] == {
        "maxOutputTokens": 65536,
        "responseMimeType": "application/json",
    }


async def test_retries_on_503_then_succeeds():
    async with respx.mock:
        route = respx.post(GENERATE_URL).mock(side_effect=[httpx.Response(503), _ok_response()])
        client = GeminiClient(api_key="k", model="gemini-2.5-flash")
        result = await client.complete("hi")

    assert result.text == "hello"
    assert route.call_count == 2


async def test_raises_after_retries_exhausted():
    async with respx.mock:
        route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(503))
        client = GeminiClient(api_key="k", model="gemini-2.5-flash", max_retries=1)
        with pytest.raises(httpx.HTTPStatusError):
            await client.complete("hi")

    assert route.call_count == 2


async def test_raises_on_client_error_without_retry():
    async with respx.mock:
        route = respx.post(GENERATE_URL).mock(
            return_value=httpx.Response(400, json={"error": {"message": "bad"}})
        )
        client = GeminiClient(api_key="k", model="gemini-2.5-flash")
        with pytest.raises(httpx.HTTPStatusError):
            await client.complete("hi")

    assert route.call_count == 1
