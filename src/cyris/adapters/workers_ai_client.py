"""Cloudflare Workers AI REST adapter satisfying the LLMClient protocol.

Same account as D1, the Pages publish and the feed buffer, so a deployment that
already uses those needs no second vendor for the digest itself.

Not yet a drop-in replacement, and `cyris llm-compare` is what shows it. Measured
2026-08-25 over one 174-article window: gpt-oss-120b writes clean 繁體中文
summaries at roughly Gemini's quality, but its news *clustering* call fails at
every setting tried — default reasoning spends the whole 8,192-token budget
thinking and returns an empty message, `reasoning_effort=low` returns zero
clusters, and medium at 24,000 tokens raises. Gemini 3.6 Flash produces 13-19
clusters on the same input. On a corpus that is mostly wire news that is the
body of the digest, not a detail.
"""

import asyncio
import json
import logging

import httpx

from cyris.service_layer.ports import LLMResponse

logger = logging.getLogger(__name__)

_API_ROOT = "https://api.cloudflare.com/client/v4/accounts"
_RETRYABLE_STATUS = (429, 500, 502, 503)

# Workers AI defaults max_tokens to 256. Every cyris call wants a JSON object
# back, so that default truncates mid-object and fails in extract_json with no
# hint of why — the request always carries an explicit number.
_MAX_OUTPUT_TOKENS = 8192


class WorkersAIClient:
    """Run a Workers AI text model over the REST API.

    Needs a token carrying Workers AI -> Read. That permission covers inference
    for both text and embeddings, so the token `cyris embed-compare` already uses
    works here unchanged; the D1/Pages token does not carry it.
    """

    def __init__(
        self,
        api_token: str,
        account_id: str,
        model: str,
        max_retries: int = 2,
        timeout: float = 180.0,
    ) -> None:
        self.model = model
        # What the run actually cost, in Cloudflare's own unit. Reported per
        # request, so it beats deriving a number from the published rates.
        self.neurons = 0.0
        self._max_retries = max_retries
        self._url = f"{_API_ROOT}/{account_id}/ai/run/{model}"
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=timeout,
        )

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        messages = []
        if system is not None:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        body: dict = {
            "messages": messages,
            "max_tokens": max_tokens or _MAX_OUTPUT_TOKENS,
            # Every cyris call is extract-and-summarise, not a problem to solve, so
            # thinking is pure overhead here — and left unbounded it is fatal, not
            # merely wasteful: gpt-oss-120b spent an entire 8,192-token budget
            # reasoning about one clustering prompt and returned an empty message.
            # Models that do not reason ignore the field (verified on llama-3.3).
            # `reasoning: {effort}` is the spelling Workers AI drops silently.
            "reasoning_effort": "low",
        }
        if temperature is not None:
            body["temperature"] = temperature

        for attempt in range(self._max_retries + 1):
            response = await self._client.post(self._url, json=body)
            if response.status_code not in _RETRYABLE_STATUS or attempt == self._max_retries:
                break
            await asyncio.sleep(attempt + 1)

        data = (
            response.json()
            if response.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        # Workers AI puts the reason in the body — "prompt is too long", a bad model
        # name — and raise_for_status() would throw away the only part worth reading.
        # Context overflow arrives here as a plain 400.
        if response.status_code >= 400 or not data.get("success", True):
            raise RuntimeError(
                f"Workers AI refused the request (HTTP {response.status_code}): "
                f"{_errors_of(data) or response.text[:300]}"
            )

        result = data.get("result") or {}
        usage = result.get("usage") or {}
        self.neurons += usage.get("neurons") or 0.0

        text = _text_of(result)
        finish = (result.get("choices") or [{}])[0].get("finish_reason")
        if not text:
            # A reasoning model can spend the whole budget thinking and return an
            # empty message. Left unreported that surfaces as "no valid JSON found
            # in response: " with nothing after the colon, which names no cause.
            logger.warning(
                "Workers AI returned no text (finish_reason=%s, %s completion tokens "
                "against max_tokens=%d)",
                finish,
                usage.get("completion_tokens", 0),
                body["max_tokens"],
            )
        elif finish == "length":
            logger.warning(
                "Workers AI hit max_tokens=%d; the JSON reply is probably truncated",
                body["max_tokens"],
            )

        return LLMResponse(
            text=text,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
        )


def _errors_of(data: dict) -> str:
    return "; ".join(str(e.get("message", e)) for e in data.get("errors") or [])


def _text_of(result: dict) -> str:
    """The reply text, out of whichever shape the model returned.

    Two shapes are live at once: the OpenAI-compatible `choices[]` (gpt-oss,
    and the newer models generally) and the flat `response` (llama-3.3). The
    flat one is not reliably a string — llama-3.3 asked for JSON hands back an
    already-decoded list — so a non-string is re-encoded rather than passed on
    to `extract_json`, which parses text and would choke on an object.
    """
    choices = result.get("choices") or []
    raw = (choices[0].get("message") or {}).get("content") if choices else None
    if raw is None:
        raw = result.get("response")
    if raw is None:
        return ""
    return raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
