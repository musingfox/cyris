"""Test doubles for cross-boundary Protocols."""

from cyris.service_layer.ports import LLMResponse


class FakeLLM:
    """In-memory LLMClient: returns queued response texts, records every call.

    Responses are consumed in order; the last one repeats for extra calls.
    """

    def __init__(
        self,
        responses: str | list[str] = "{}",
        model: str = "test-model",
        input_tokens: int = 500,
        output_tokens: int = 100,
        error: Exception | None = None,
    ) -> None:
        self.model = model
        self._responses = [responses] if isinstance(responses, str) else list(responses)
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self._error = error
        self.calls: list[dict] = []

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        self.calls.append(
            {
                "prompt": prompt,
                "system": system,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        if self._error is not None:
            raise self._error
        text = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        return LLMResponse(
            text=text,
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
        )
