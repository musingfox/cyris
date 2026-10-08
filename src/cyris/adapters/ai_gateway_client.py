"""Cloudflare AI Gateway adapter satisfying the LLMClient protocol.

One `POST /accounts/{id}/ai/run` with Cloudflare's envelope, `{"model": "author/model",
"input": {...}}` (https://developers.cloudflare.com/ai-gateway/usage/rest-api/). The
provider's key is not in the request: it is stored in the gateway (BYOK), and on this
path that is mandatory — measured 2026-09-21, an embedded provider key, under every
header tried, was refused with HTTP 402 code 2021 like no key at all. What the gateway
gives back is a log of every request and response body, kept outside cyris.

No gateway id is sent. Without `cf-aig-gateway-id` the request goes to the gateway named
`default` (https://developers.cloudflare.com/ai-gateway/configuration/manage-gateway/),
so the provider keys belong there.

Only `google/*` is spoken. Its `input` is Gemini's own generateContent body and its
response carries Gemini's `usageMetadata`, both shown under `/ai/run` on the model
catalog page (https://developers.cloudflare.com/ai/models/google/gemini-3-flash/). The
catalog shows Anthropic and OpenAI models only through their own-format endpoints, and a
guessed usage field would report zero tokens, which reads as a measurement.
"""

import asyncio

import httpx

from cyris.service_layer.ports import LLMResponse

_API_ROOT = "https://api.cloudflare.com/client/v4/accounts"
_RETRYABLE_STATUS = (429, 500, 502, 503)
_SUPPORTED_AUTHORS = ("google",)
# Gemini 3.x flash's documented output limit, as in GeminiClient: thinking tokens
# count against maxOutputTokens, so anything lower risks truncated JSON.
_MAX_OUTPUT_TOKENS = 65536

# Which side refused, by Cloudflare's error code. 2021 and the 7xxx codes are the
# ticket's 2026-09-21 measurements; 10000 is the REST page's token without Workers AI.
_GATEWAY_CODES = {2021, 10000}
_MODEL_CODES = {7003, 7010}


class AIGatewayError(RuntimeError):
    """A refusal, with the side that refused it.

    `side` is "gateway" when the account, token or BYOK setup is the fix, "model" when
    the model name is, and "unknown" when the body named no code this adapter knows.
    """

    def __init__(
        self, *, model: str, status: int, code: int | None, message: str, side: str
    ) -> None:
        self.model = model
        self.status = status
        self.code = code
        self.message = message
        self.side = side
        who = {
            "gateway": "AI Gateway refused the request for",
            "model": "AI Gateway refused the model",
        }.get(side, "AI Gateway returned an error for")
        super().__init__(f"{who} {model} (HTTP {status}, code {code}): {message}")


class AIGatewayClient:
    """Run a model through AI Gateway's REST API.

    Needs a token carrying Workers AI -> Read: the REST page says every
    `/accounts/{id}/ai/*` call needs that permission, third-party models included.
    """

    def __init__(
        self,
        api_token: str,
        account_id: str,
        model: str,
        max_retries: int = 2,
        timeout: float = 120.0,
    ) -> None:
        self.model = model
        self._max_retries = max_retries
        self._url = f"{_API_ROOT}/{account_id}/ai/run"
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
        author = self.model.partition("/")[0]
        if author not in _SUPPORTED_AUTHORS:
            raise ValueError(
                f"{self.model}: the ai_gateway provider parses "
                f"{', '.join(a + '/' for a in _SUPPORTED_AUTHORS)} models only"
            )

        # Every cyris LLM call expects JSON (complete_json); JSON mode stops Gemini
        # from emitting markdown fences or unescaped quotes that break parsing.
        generation_config: dict = {
            "maxOutputTokens": max_tokens or _MAX_OUTPUT_TOKENS,
            "responseMimeType": "application/json",
        }
        if temperature is not None:
            generation_config["temperature"] = temperature
        model_input: dict = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
        if system is not None:
            model_input["systemInstruction"] = {"parts": [{"text": system}]}
        model_input["generationConfig"] = generation_config
        body = {"model": self.model, "input": model_input}

        for attempt in range(self._max_retries + 1):
            response = await self._client.post(self._url, json=body)
            if response.status_code not in _RETRYABLE_STATUS or attempt == self._max_retries:
                break
            await asyncio.sleep(attempt + 1)

        if response.status_code >= 400:
            raise self._refusal(response)

        data = response.json()
        # The catalog shows the body bare; `/ai/run` for a `@cf/` model wraps it in
        # Cloudflare's `{success, result}`. Which a third-party model gets over REST
        # is not stated, so either is read.
        if "success" in data and isinstance(data.get("result"), dict):
            data = data["result"]

        if not data.get("candidates"):
            block_reason = (data.get("promptFeedback") or {}).get("blockReason", "unknown")
            raise RuntimeError(f"{self.model} returned no candidates (blockReason {block_reason})")
        candidate = data["candidates"][0]
        parts = candidate.get("content", {}).get("parts")
        if parts is None:
            raise RuntimeError(
                f"{self.model} returned no output "
                f"(finishReason {candidate.get('finishReason', 'unknown')}) — "
                "raise max_tokens if this is a reasoning model"
            )
        usage = data.get("usageMetadata", {})
        return LLMResponse(
            text="".join(part.get("text", "") for part in parts),
            input_tokens=usage.get("promptTokenCount", 0),
            # Thinking is billed as output but reported in its own field.
            output_tokens=usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0),
        )

    def _refusal(self, response: httpx.Response) -> AIGatewayError:
        """The body's own code and words; raise_for_status() would keep only the status."""
        code, message = None, response.text[:300]
        try:
            errors = response.json().get("errors")
        except (ValueError, AttributeError):
            errors = None
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            code = errors[0].get("code")
            message = str(errors[0].get("message", errors[0]))
        if code in _GATEWAY_CODES:
            side = "gateway"
        elif code in _MODEL_CODES:
            side = "model"
        else:
            side = "unknown"
        return AIGatewayError(
            model=self.model, status=response.status_code, code=code, message=message, side=side
        )
