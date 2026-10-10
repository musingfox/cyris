"""Tests for the Cloudflare Clef adapter."""

import json

import httpx
import pytest
import respx

from cyris.adapters.clef import ClefClient, ClefError
from cyris.service_layer.ports import NoulAnswer

pytestmark = pytest.mark.unit

ACCOUNT = "acct-clef"
RUN_URL = (
    f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/ai/run/@cf/cloudflare/clef-flash"
)

# Envelope measured against the live endpoint.
ANSWER = {
    "result": {
        "model": "clef-flash",
        "answers": {"q": {"type": "noul", "noul": 0.8833}},
        "usage": {"input_tokens": 842, "output_tokens": 0},
    },
    "success": True,
    "errors": [],
    "messages": [],
}

STATE = {
    "article": {"title": "T", "source": "S", "content": "C"},
    "reader_upvoted": ["U"],
    "reader_downvoted": [],
}


def _client(token: str = "tok-clef") -> ClefClient:
    return ClefClient(token, ACCOUNT)


async def test_reads_the_probability_model_and_input_tokens():
    async with respx.mock:
        route = respx.post(RUN_URL).mock(return_value=httpx.Response(200, json=ANSWER))
        answer = await _client().ask(STATE, "I")

    assert answer == NoulAnswer(noul=0.8833, model="clef-flash", input_tokens=842)
    assert route.call_count == 1


async def test_sends_one_noul_question_with_the_bearer_token():
    async with respx.mock:
        route = respx.post(RUN_URL).mock(return_value=httpx.Response(200, json=ANSWER))
        await _client().ask(STATE, "I")

    assert route.call_count == 1
    request = route.calls[0].request
    assert str(request.url) == RUN_URL
    assert request.headers["authorization"] == "Bearer tok-clef"
    assert json.loads(request.content) == {
        "model": "clef-flash",
        "state": STATE,
        "questions": {"q": {"type": "noul", "instructions": "I"}},
    }


async def test_retries_on_429_then_succeeds(backoff_sleeps):
    async with respx.mock:
        route = respx.post(RUN_URL).mock(
            side_effect=[httpx.Response(429), httpx.Response(200, json=ANSWER)]
        )
        answer = await _client().ask(STATE, "I")

    assert answer.noul == 0.8833
    assert route.call_count == 2
    assert backoff_sleeps == [1]


async def test_gives_up_after_three_503s(backoff_sleeps):
    # Error body is the Cloudflare v4 envelope.
    busy = {"success": False, "errors": [{"code": 1, "message": "busy"}]}
    async with respx.mock:
        route = respx.post(RUN_URL).mock(return_value=httpx.Response(503, json=busy))
        with pytest.raises(ClefError) as err:
            await _client().ask(STATE, "I")

    assert "HTTP 503" in str(err.value)
    assert "busy" in str(err.value)
    assert route.call_count == 3
    assert backoff_sleeps == [1, 2]
