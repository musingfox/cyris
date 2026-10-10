"""Cloudflare Clef adapter: one state and one instruction become one noul probability.

Same account and token family as Workers AI, so a deployment that already runs
`workers-ai` needs no second vendor.
"""

import asyncio

import httpx

from cyris.adapters.cloudflare import API_ROOT
from cyris.service_layer.ports import NoulAnswer

_RETRYABLE_STATUS = (429, 500, 502, 503)

CLEF_MODEL = "@cf/cloudflare/clef-flash"
CLEF_REQUEST_TIMEOUT_SECONDS = 20


class ClefError(RuntimeError):
    """Clef refused the request or answered without a usable probability."""


class ClefClient:
    def __init__(self, api_token: str, account_id: str, max_retries: int = 2) -> None:
        self._max_retries = max_retries
        self._url = f"{API_ROOT}/accounts/{account_id}/ai/run/{CLEF_MODEL}"
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=CLEF_REQUEST_TIMEOUT_SECONDS,
        )

    async def ask(self, state: dict, instructions: str) -> NoulAnswer:
        body = {
            "model": CLEF_MODEL.rsplit("/", 1)[1],
            "state": state,
            "questions": {"q": {"type": "noul", "instructions": instructions}},
        }
        for attempt in range(self._max_retries + 1):
            response = await self._client.post(self._url, json=body)
            if response.status_code not in _RETRYABLE_STATUS:
                break
            if attempt == self._max_retries:
                raise ClefError(
                    f"Clef kept failing (HTTP {response.status_code}): "
                    f"{_errors_of(_json_of(response)) or response.text[:300]}"
                )
            await asyncio.sleep(attempt + 1)
        data = _json_of(response)
        if response.status_code >= 400:
            raise ClefError(
                f"Clef refused the request (HTTP {response.status_code}): "
                f"{_errors_of(data) or response.text[:300]}"
            )

        unusable = ClefError(f"Clef answered without a noul probability: {response.text[:300]}")
        result = data.get("result")
        if not data.get("success", True) or not isinstance(result, dict):
            raise unusable
        answers, usage = result.get("answers"), result.get("usage", {})
        question = answers.get("q") if isinstance(answers, dict) else None
        if not isinstance(question, dict) or not isinstance(usage, dict):
            raise unusable
        noul = question.get("noul")
        if isinstance(noul, bool) or not isinstance(noul, int | float):
            raise unusable
        if not 0 <= noul <= 1:
            raise unusable
        model, input_tokens = result.get("model"), usage.get("input_tokens")
        return NoulAnswer(
            noul=float(noul),
            model=model if isinstance(model, str) else None,
            input_tokens=(
                input_tokens
                if isinstance(input_tokens, int) and not isinstance(input_tokens, bool)
                else None
            ),
        )


def _json_of(response: httpx.Response) -> dict:
    if not response.headers.get("content-type", "").startswith("application/json"):
        return {}
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _errors_of(data: dict) -> str:
    errors = data.get("errors")
    if not isinstance(errors, list):
        return ""
    return "; ".join(str(e.get("message", e) if isinstance(e, dict) else e) for e in errors)
