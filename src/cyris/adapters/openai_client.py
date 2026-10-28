"""OpenAI chat-completions adapter satisfying the LLMClient protocol.

Plain httpx rather than the `openai` SDK: the surface used here is one POST, and
a dependency earns its place by saving more than it costs.
"""

import asyncio
import json
import logging

import httpx

from cyris.service_layer.ports import LLMResponse

logger = logging.getLogger(__name__)

_URL = "https://api.openai.com/v1/chat/completions"
_RETRYABLE_STATUS = (429, 500, 502, 503)

# Bounds visible output *and* reasoning tokens together, so it has to be well
# clear of the largest real reply — the digest's biggest single call has run to
# roughly 4k output tokens.
_MAX_OUTPUT_TOKENS = 16384


class OpenAIClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        max_retries: int = 2,
        timeout: float = 180.0,
        *,
        reasoning_effort: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.model = model
        self._reasoning_effort = reasoning_effort
        self._max_retries = max_retries
        # An empty key points at an endpoint of one's own (LAN or localhost),
        # which may answer with no Authorization header to carry at all.
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=timeout,
        )
        # The OpenAI-compatible choice: the reader names the /v1 level, and the
        # one path this adapter speaks is appended — the same shape the official
        # address ends in, so anything that serves it serves this.
        if base_url:
            self._url = f"{base_url.rstrip('/')}/chat/completions"
        else:
            self._url = _URL

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
            "model": self.model,
            "messages": messages,
            # Not `max_tokens`: that name is deprecated and is rejected outright by
            # the reasoning models, which is most of what anyone would point this at.
            "max_completion_tokens": max_tokens or _MAX_OUTPUT_TOKENS,
            # complete_json parses whatever comes back, so this only removes a
            # failure mode (markdown fences, a preamble) rather than enabling one.
            "response_format": {"type": "json_object"},
        }
        if self._reasoning_effort is not None:
            body["reasoning_effort"] = self._reasoning_effort
        if temperature is not None:
            body["temperature"] = temperature

        for attempt in range(self._max_retries + 1):
            response = await self._client.post(self._url, json=body)
            if response.status_code not in _RETRYABLE_STATUS or attempt == self._max_retries:
                break
            await asyncio.sleep(attempt + 1)

        # The reason a request was rejected — a bad model name, a length limit — is
        # in the body, and raise_for_status() would discard exactly that.
        if response.status_code >= 400:
            raise RuntimeError(
                f"OpenAI refused the request (HTTP {response.status_code}): {_error_of(response)}"
            )

        data = response.json()
        choice = (data.get("choices") or [{}])[0]
        usage = data.get("usage") or {}
        text = (choice.get("message") or {}).get("content") or ""

        if not text:
            logger.warning(
                "OpenAI returned no text (finish_reason=%s, %s completion tokens against "
                "max_completion_tokens=%d)",
                choice.get("finish_reason"),
                usage.get("completion_tokens", 0),
                body["max_completion_tokens"],
            )
        elif choice.get("finish_reason") == "length":
            logger.warning(
                "OpenAI hit max_completion_tokens=%d; the JSON reply is probably truncated",
                body["max_completion_tokens"],
            )

        return LLMResponse(
            text=text,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
        )


def _error_of(response: httpx.Response) -> str:
    try:
        error = response.json().get("error") or {}
    except (json.JSONDecodeError, ValueError):
        return response.text[:300]
    return error.get("message") or response.text[:300]
