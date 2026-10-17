"""Cloudflare AI Gateway adapter satisfying the LLMClient protocol.

One `POST gateway.ai.cloudflare.com/v1/{account}/default/google-ai-studio/v1beta/models/
{model}:generateContent`, Gemini's own endpoint behind the gateway
(https://developers.cloudflare.com/ai-gateway/usage/providers/google-ai-studio/). The
body and the response are `GeminiClient`'s. The provider's key is not in the request: it
is stored in the gateway (BYOK) and the request carries only `cf-aig-authorization`. A
provider-key header would be forwarded to Google and the stored key skipped.

The REST route `/accounts/{id}/ai/run` was dropped: measured 2026-10-16, it answered
HTTP 402 code 2021 with a BYOK key stored, because it bills Unified Billing credit and
never reads BYOK. What the gateway gives back is a log of every request and response
body, kept outside cyris.

No gateway id is configurable: the path names the gateway `default`, so the provider
key belongs there.

Only `google/*` is spoken, since `usageMetadata` is Gemini's. A guessed usage field for
another author would report zero tokens, which reads as a measurement.
"""

import asyncio

import httpx

from cyris.adapters.gemini_client import OutputBudgetSpentError
from cyris.service_layer.ports import LLMResponse

_GATEWAY_ROOT = "https://gateway.ai.cloudflare.com/v1"
_RETRYABLE_STATUS = (429, 500, 502, 503)
_SUPPORTED_AUTHORS = ("google",)
# Gemini 3.x flash's documented output limit, as in GeminiClient: thinking tokens
# count against maxOutputTokens, so anything lower risks truncated JSON.
_MAX_OUTPUT_TOKENS = 65536

# The gateway's own refusal is a list in `error`; 2009 is the 2026-10-16 measurement of
# a token without AI Gateway -> Run. Google's refusal is a dict whose `status` names the
# cause, and NOT_FOUND is how it says the model is not there.
_GATEWAY_CODES = {2009}
_MODEL_STATUSES = {"NOT_FOUND"}


class AIGatewayError(RuntimeError):
    """A refusal, with the side that refused it.

    `side` is "gateway" when the account, token or BYOK setup is the fix, "model" when
    the model name is, and "unknown" when the body matched neither shape this adapter knows.
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
    """Run a model through AI Gateway's Google AI Studio endpoint.

    Needs a token carrying Account -> AI Gateway -> Run; a Workers AI token is 401 here.
    """

    def __init__(
        self,
        api_token: str,
        account_id: str,
        model: str,
        max_retries: int = 2,
        timeout: float = 120.0,
        *,
        reasoning_effort: str | None = None,
        thinking_budget: int | None = None,
    ) -> None:
        self.model = model
        self._reasoning_effort = reasoning_effort
        self._thinking_budget = thinking_budget
        self._max_retries = max_retries
        self._url = (
            f"{_GATEWAY_ROOT}/{account_id}/default/google-ai-studio/v1beta/models/"
            f"{model.partition('/')[2]}:generateContent"
        )
        self._client = httpx.AsyncClient(
            headers={"cf-aig-authorization": f"Bearer {api_token}"},
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
        if self._thinking_budget is not None:
            generation_config["thinkingConfig"] = {"thinkingBudget": self._thinking_budget}
        elif self._reasoning_effort is not None:
            generation_config["thinkingConfig"] = {"thinkingLevel": self._reasoning_effort.upper()}
        body: dict = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
        if system is not None:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        body["generationConfig"] = generation_config

        for attempt in range(self._max_retries + 1):
            response = await self._client.post(self._url, json=body)
            if response.status_code not in _RETRYABLE_STATUS or attempt == self._max_retries:
                break
            await asyncio.sleep(attempt + 1)

        if response.status_code >= 400:
            raise self._refusal(response)

        data = response.json()
        if not data.get("candidates"):
            block_reason = (data.get("promptFeedback") or {}).get("blockReason", "unknown")
            raise RuntimeError(f"{self.model} returned no candidates (blockReason {block_reason})")
        candidate = data["candidates"][0]
        parts = candidate.get("content", {}).get("parts")
        if parts is None:
            raise OutputBudgetSpentError(
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
        code, message, side = None, response.text[:300], "unknown"
        try:
            error = response.json().get("error")
        except (ValueError, AttributeError):
            error = None
        if isinstance(error, list) and error and isinstance(error[0], dict):
            code = error[0].get("code")
            message = str(error[0].get("message", error[0]))
            if code in _GATEWAY_CODES:
                side = "gateway"
        elif isinstance(error, dict):
            code = error.get("code")
            message = str(error.get("message", error))
            if error.get("status") in _MODEL_STATUSES:
                side = "model"
        return AIGatewayError(
            model=self.model, status=response.status_code, code=code, message=message, side=side
        )
