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


class SqliteD1:
    """A D1 stand-in that runs the real SQL against stdlib sqlite3.

    D1 *is* SQLite, so the store's queries can be exercised for real without a
    network: a broken statement fails here exactly as it would in production.
    The schema is loaded from the file that ships to D1, so a schema mistake
    fails here too.
    """

    def __init__(self) -> None:
        import sqlite3
        from pathlib import Path

        schema = Path(__file__).parent.parent / "src/cyris/adapters/store/schema.sql"
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(schema.read_text(encoding="utf-8"))

    def query(self, sql, params=None):
        from cyris.adapters.store.d1 import QueryResult

        cursor = self._conn.execute(sql, params or [])
        rows = [dict(row) for row in cursor.fetchall()]
        self._conn.commit()
        return QueryResult(rows=rows, changes=max(cursor.rowcount, 0))
