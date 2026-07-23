"""Anthropic SDK adapter satisfying the LLMClient protocol."""

import anthropic

from cyris.service_layer.ports import LLMResponse


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
        max_tokens: int = 16384,
        temperature: float | None = None,
    ) -> LLMResponse:
        kwargs: dict = {}
        if system is not None:
            kwargs["system"] = system
        if temperature is not None:
            kwargs["temperature"] = temperature
        message = await self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
        )
        return LLMResponse(
            text=message.content[0].text,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
        )
