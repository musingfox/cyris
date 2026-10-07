"""Anthropic SDK adapter satisfying the LLMClient protocol."""

import logging

import anthropic

from cyris.service_layer.ports import LLMResponse

logger = logging.getLogger(__name__)

# Well clear of the largest real JSON reply.
_MAX_OUTPUT_TOKENS = 16384


class AnthropicClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        max_retries: int = 2,
        timeout: float = 120.0,
    ) -> None:
        self.model = model
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key, max_retries=max_retries, timeout=timeout
        )

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        limit = max_tokens or _MAX_OUTPUT_TOKENS
        kwargs: dict = {}
        if system is not None:
            kwargs["system"] = system
        if temperature is not None:
            kwargs["temperature"] = temperature
        message = await self._client.messages.create(
            model=self.model,
            max_tokens=limit,
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
        )
        # Models that think by default lead with a thinking block, which has no text.
        text = "".join(block.text for block in message.content if block.type == "text")
        if message.stop_reason == "max_tokens":
            logger.warning(
                "Anthropic hit max_tokens=%d; the JSON reply is probably truncated", limit
            )
        elif message.stop_reason == "refusal":
            logger.warning("Anthropic refused the request (stop_reason=refusal)")
        return LLMResponse(
            text=text,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
        )
