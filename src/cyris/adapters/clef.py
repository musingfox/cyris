"""Cloudflare Clef adapter: one state and one instruction become one noul probability.

Same account and token family as Workers AI, so a deployment that already runs
`workers-ai` needs no second vendor.
"""

import asyncio

import httpx

from cyris.service_layer.ports import NoulAnswer

_RETRYABLE_STATUS = (429, 500, 502, 503)
_API_ROOT = "https://api.cloudflare.com/client/v4/accounts"

CLEF_MODEL = "@cf/cloudflare/clef-flash"
CLEF_REQUEST_TIMEOUT_SECONDS = 20.0


class ClefError(RuntimeError):
    """Clef refused the request or answered without a usable probability."""


class ClefClient:
    def __init__(self, api_token: str, account_id: str, max_retries: int = 2) -> None:
        self._max_retries = max_retries
        self._url = f"{_API_ROOT}/{account_id}/ai/run/{CLEF_MODEL}"
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
        result = _json_of(response).get("result") or {}
        usage = result.get("usage") or {}
        return NoulAnswer(
            noul=result["answers"]["q"]["noul"],
            model=result.get("model"),
            input_tokens=usage.get("input_tokens"),
        )


def _json_of(response: httpx.Response) -> dict:
    if not response.headers.get("content-type", "").startswith("application/json"):
        return {}
    data = response.json()
    return data if isinstance(data, dict) else {}


def _errors_of(data: dict) -> str:
    return "; ".join(str(e.get("message", e)) for e in data.get("errors") or [])
