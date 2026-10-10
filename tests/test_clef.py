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


async def test_a_400_raises_with_cloudflares_reason(backoff_sleeps):
    # Error body is the Cloudflare v4 envelope.
    body = {"success": False, "errors": [{"code": 5006, "message": "Type mismatch of '/model'"}]}
    async with respx.mock:
        route = respx.post(RUN_URL).mock(return_value=httpx.Response(400, json=body))
        with pytest.raises(ClefError) as err:
            await _client().ask(STATE, "I")

    assert "HTTP 400" in str(err.value)
    assert "Type mismatch of '/model'" in str(err.value)
    assert route.call_count == 1
    assert backoff_sleeps == []


async def test_a_401_error_never_carries_the_token():
    body = {"success": False, "errors": [{"code": 10000, "message": "Authentication error"}]}
    async with respx.mock:
        route = respx.post(RUN_URL).mock(return_value=httpx.Response(401, json=body))
        with pytest.raises(ClefError) as err:
            await _client("tok-secret-clef").ask(STATE, "I")

    assert "tok-secret-clef" not in str(err.value)
    assert route.call_count == 1


async def test_a_2xx_that_says_it_failed_is_an_answer_without_a_noul():
    body = {"success": False, "errors": [{"code": 1, "message": "nope"}], "result": None}
    async with respx.mock:
        route = respx.post(RUN_URL).mock(return_value=httpx.Response(200, json=body))
        with pytest.raises(ClefError, match="without a noul probability"):
            await _client().ask(STATE, "I")

    assert route.call_count == 1


async def test_an_answer_without_a_noul_raises():
    body = {
        **ANSWER,
        "result": {"model": "clef-flash", "answers": {}, "usage": {"input_tokens": 842}},
    }
    async with respx.mock:
        route = respx.post(RUN_URL).mock(return_value=httpx.Response(200, json=body))
        with pytest.raises(ClefError, match="without a noul"):
            await _client().ask(STATE, "I")

    assert route.call_count == 1


async def test_a_non_numeric_noul_raises():
    body = {**ANSWER, "result": {"answers": {"q": {"type": "noul", "noul": "high"}}}}
    async with respx.mock:
        route = respx.post(RUN_URL).mock(return_value=httpx.Response(200, json=body))
        with pytest.raises(ClefError):
            await _client().ask(STATE, "I")

    assert route.call_count == 1


_UNUSABLE = [
    pytest.param({"success": True, "result": "x"}, id="result-not-a-dict"),
    pytest.param({"success": True, "result": {"answers": []}}, id="answers-not-a-dict"),
    pytest.param({"success": True, "result": {"answers": {"q": 0.5}}}, id="q-not-a-dict"),
    pytest.param(
        {"success": True, "result": {"answers": {"q": {"noul": 0.5}}, "usage": 7}},
        id="usage-not-a-dict",
    ),
    pytest.param({"success": True, "result": {"answers": {"q": {"noul": True}}}}, id="noul-a-bool"),
    pytest.param(
        {"success": True, "result": {"answers": {"q": {"noul": 1.5}}}}, id="noul-above-one"
    ),
    pytest.param(
        {"success": True, "result": {"answers": {"q": {"noul": -0.1}}}}, id="noul-below-zero"
    ),
]


@pytest.mark.parametrize("body", _UNUSABLE)
async def test_an_unusable_answer_raises_clef_error(body):
    async with respx.mock:
        route = respx.post(RUN_URL).mock(return_value=httpx.Response(200, json=body))
        with pytest.raises(ClefError, match="without a noul probability"):
            await _client().ask(STATE, "I")

    assert route.call_count == 1


async def test_a_nan_noul_raises_clef_error():
    raw = b'{"success": true, "result": {"answers": {"q": {"noul": NaN}}}}'
    async with respx.mock:
        route = respx.post(RUN_URL).mock(
            return_value=httpx.Response(
                200, content=raw, headers={"content-type": "application/json"}
            )
        )
        with pytest.raises(ClefError, match="without a noul probability"):
            await _client().ask(STATE, "I")

    assert route.call_count == 1


async def test_malformed_json_on_a_2xx_raises_clef_error():
    async with respx.mock:
        route = respx.post(RUN_URL).mock(
            return_value=httpx.Response(
                200, content=b"{not json", headers={"content-type": "application/json"}
            )
        )
        with pytest.raises(ClefError, match="without a noul probability: {not json"):
            await _client().ask(STATE, "I")

    assert route.call_count == 1


async def test_malformed_json_on_a_4xx_keeps_the_refusal_shape():
    async with respx.mock:
        route = respx.post(RUN_URL).mock(
            return_value=httpx.Response(
                400, content=b"{not json", headers={"content-type": "application/json"}
            )
        )
        with pytest.raises(ClefError) as err:
            await _client().ask(STATE, "I")

    assert str(err.value) == "Clef refused the request (HTTP 400): {not json"
    assert route.call_count == 1


async def test_a_string_token_count_is_dropped_to_none():
    body = {**ANSWER, "result": {**ANSWER["result"], "usage": {"input_tokens": "842"}}}
    async with respx.mock:
        route = respx.post(RUN_URL).mock(return_value=httpx.Response(200, json=body))
        answer = await _client().ask(STATE, "I")

    assert answer == NoulAnswer(noul=0.8833, model="clef-flash", input_tokens=None)
    assert route.call_count == 1


async def test_a_non_string_model_is_dropped_to_none():
    body = {**ANSWER, "result": {**ANSWER["result"], "model": 3}}
    async with respx.mock:
        route = respx.post(RUN_URL).mock(return_value=httpx.Response(200, json=body))
        answer = await _client().ask(STATE, "I")

    assert answer.model is None
    assert route.call_count == 1
