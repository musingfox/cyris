"""Tests for the Cloudflare Workers AI REST adapter."""

import json

import httpx
import pytest
import respx

from cyris.adapters.workers_ai_client import WorkersAIClient

ACCOUNT = "acct-123"
MODEL = "@cf/openai/gpt-oss-120b"
RUN_URL = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/ai/run/{MODEL}"


def _client(model: str = MODEL) -> WorkersAIClient:
    return WorkersAIClient(api_token="test-cf-token", account_id=ACCOUNT, model=model)


def _envelope(result: dict) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "errors": [], "result": result})


def _usage(neurons: float = 19.0) -> dict:
    return {"prompt_tokens": 145, "completion_tokens": 207, "neurons": neurons}


async def test_reads_the_openai_shaped_choices_and_usage():
    result = {
        "choices": [{"message": {"content": '{"items": []}'}, "finish_reason": "stop"}],
        "usage": _usage(),
    }
    async with respx.mock:
        route = respx.post(RUN_URL).mock(return_value=_envelope(result))
        client = _client()
        response = await client.complete("Summarise.", system="Be brief.", temperature=0.5)

    assert response.text == '{"items": []}'
    assert response.input_tokens == 145
    assert response.output_tokens == 207
    assert client.neurons == 19.0

    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer test-cf-token"
    body = json.loads(request.content)
    assert body["messages"] == [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "Summarise."},
    ]
    assert body["temperature"] == 0.5


async def test_falls_back_to_the_flat_response_field():
    """llama-3.3 answers in `response`, not `choices`."""
    async with respx.mock:
        respx.post(RUN_URL).mock(return_value=_envelope({"response": "plain text", "usage": {}}))
        response = await _client().complete("Hi.")

    assert response.text == "plain text"
    assert response.input_tokens == 0


async def test_reencodes_a_response_that_is_not_a_string():
    """Asked for JSON, llama-3.3 hands back an already-decoded object.

    `extract_json` parses text, so passing the object straight through would
    fail on a reply that is in fact perfectly good.
    """
    decoded = [{"title": "台積電", "summary": "美國廠提前量產"}]
    async with respx.mock:
        respx.post(RUN_URL).mock(return_value=_envelope({"response": decoded, "usage": {}}))
        response = await _client().complete("Summarise.")

    assert json.loads(response.text) == decoded
    assert "台積電" in response.text  # not \u-escaped, so the digest stays readable


async def test_always_sends_max_tokens():
    """The API default is 256, which truncates every JSON reply cyris asks for."""
    async with respx.mock:
        route = respx.post(RUN_URL).mock(return_value=_envelope({"response": "ok", "usage": {}}))
        await _client().complete("Hi.")

    assert json.loads(route.calls[0].request.content)["max_tokens"] == 8192


async def test_asks_for_low_reasoning_effort():
    """Load-bearing, not a tuning preference.

    Every cyris call is extract-and-summarise, and left to think freely
    gpt-oss-120b spent a whole 8,192-token budget reasoning about one clustering
    prompt and answered with an empty message. Deleting this field regresses
    straight back into that.
    """
    async with respx.mock:
        route = respx.post(RUN_URL).mock(return_value=_envelope({"response": "ok", "usage": {}}))
        await _client().complete("Hi.")

    assert json.loads(route.calls[0].request.content)["reasoning_effort"] == "low"


async def test_explicit_max_tokens_wins():
    async with respx.mock:
        route = respx.post(RUN_URL).mock(return_value=_envelope({"response": "ok", "usage": {}}))
        await _client().complete("Hi.", max_tokens=512)

    assert json.loads(route.calls[0].request.content)["max_tokens"] == 512


async def test_warns_when_the_reply_was_truncated(caplog):
    result = {"choices": [{"message": {"content": '{"a":'}, "finish_reason": "length"}]}
    async with respx.mock:
        respx.post(RUN_URL).mock(return_value=_envelope(result))
        await _client().complete("Summarise.")

    assert "truncated" in caplog.text


async def test_accumulates_neurons_across_calls():
    async with respx.mock:
        respx.post(RUN_URL).mock(
            return_value=_envelope({"response": "ok", "usage": _usage(neurons=2.5)})
        )
        client = _client()
        await client.complete("Hi.")
        await client.complete("Hi again.")

    assert client.neurons == 5.0


async def test_retries_on_429_then_succeeds():
    async with respx.mock:
        route = respx.post(RUN_URL).mock(
            side_effect=[
                httpx.Response(429),
                _envelope({"response": "ok", "usage": {}}),
            ]
        )
        response = await _client().complete("Hi.")

    assert route.call_count == 2
    assert response.text == "ok"


async def test_does_not_retry_a_403():
    """A token without Workers AI is wrong, not busy — repeating it only delays saying so."""
    async with respx.mock:
        route = respx.post(RUN_URL).mock(return_value=httpx.Response(403))
        with pytest.raises(RuntimeError):
            await _client().complete("Hi.")

    assert route.call_count == 1


async def test_raises_with_the_reason_cloudflare_gave():
    body = {"success": False, "errors": [{"message": "No such model"}], "result": None}
    async with respx.mock:
        respx.post(RUN_URL).mock(return_value=httpx.Response(200, json=body))
        with pytest.raises(RuntimeError, match="No such model"):
            await _client().complete("Hi.")


async def test_a_4xx_body_reaches_the_caller():
    """Context overflow arrives as a 400 whose body is the only useful part.

    raise_for_status() would report the URL and nothing about why — the same way
    an opaque 400 from D1 once hid `no such table: sources`.
    """
    body = {"success": False, "errors": [{"message": "prompt is too long"}], "result": None}
    async with respx.mock:
        respx.post(RUN_URL).mock(return_value=httpx.Response(400, json=body))
        with pytest.raises(RuntimeError, match="prompt is too long"):
            await _client().complete("Hi.")


async def test_an_empty_reply_is_reported_rather_than_returned_blank(caplog):
    """A reasoning model can spend the whole budget thinking and answer nothing."""
    result = {
        "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
        "usage": {"completion_tokens": 8192},
    }
    async with respx.mock:
        respx.post(RUN_URL).mock(return_value=_envelope(result))
        response = await _client().complete("Summarise.")

    assert response.text == ""
    assert "returned no text" in caplog.text
