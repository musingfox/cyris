"""Gemini REST adapter satisfying the LLMClient protocol."""

import asyncio

import httpx

from cyris.service_layer.ports import LLMResponse

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
_RETRYABLE_STATUS = (429, 500, 502, 503)
# Documented output token limit for gemini-3.x flash; thinking tokens count
# against maxOutputTokens, so anything lower risks truncated JSON.
_MAX_OUTPUT_TOKENS = 65536


class GeminiClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        max_retries: int = 2,
        timeout: float = 120.0,
    ) -> None:
        self.model = model
        self._max_retries = max_retries
        self._client = httpx.AsyncClient(
            base_url=_BASE_URL,
            headers={"x-goog-api-key": api_key},
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
        # Every cyris LLM call expects JSON (complete_json); JSON mode stops Gemini
        # from emitting markdown fences or unescaped quotes that break parsing.
        generation_config: dict = {
            "maxOutputTokens": max_tokens or _MAX_OUTPUT_TOKENS,
            "responseMimeType": "application/json",
        }
        if temperature is not None:
            generation_config["temperature"] = temperature
        body: dict = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": generation_config,
        }
        if system is not None:
            body["system_instruction"] = {"parts": [{"text": system}]}

        for attempt in range(self._max_retries + 1):
            response = await self._client.post(f"/models/{self.model}:generateContent", json=body)
            if response.status_code not in _RETRYABLE_STATUS or attempt == self._max_retries:
                break
            await asyncio.sleep(attempt + 1)
        response.raise_for_status()

        data = response.json()
        candidate = data["candidates"][0]
        # A thinking model can spend the whole output budget before writing a
        # word, and then `content` carries no `parts` at all. Reading through it
        # raised KeyError('parts'), which named neither the cause nor the fix.
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
            output_tokens=usage.get("candidatesTokenCount", 0),
        )
