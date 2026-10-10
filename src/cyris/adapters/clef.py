"""Cloudflare Clef adapter: one state and one instruction become one noul probability.

Same account and token family as Workers AI, so a deployment that already runs
`workers-ai` needs no second vendor.
"""

import httpx

from cyris.service_layer.ports import NoulAnswer

_API_ROOT = "https://api.cloudflare.com/client/v4/accounts"

CLEF_MODEL = "@cf/cloudflare/clef-flash"
CLEF_REQUEST_TIMEOUT_SECONDS = 20.0


class ClefError(RuntimeError):
    """Clef refused the request or answered without a usable probability."""


class ClefClient:
    def __init__(self, api_token: str, account_id: str) -> None:
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
        response = await self._client.post(self._url, json=body)
        result = response.json().get("result") or {}
        usage = result.get("usage") or {}
        return NoulAnswer(
            noul=result["answers"]["q"]["noul"],
            model=result.get("model"),
            input_tokens=usage.get("input_tokens"),
        )
