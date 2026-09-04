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

    def __init__(self, *, with_schema: bool = True) -> None:
        import sqlite3
        from pathlib import Path

        schema = Path(__file__).parent.parent / "src/cyris/adapters/store/schema.sql"
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        # `with_schema=False` is what a clean Cloudflare account hands the first
        # boot: a reachable database with no tables in it.
        if with_schema:
            self._conn.executescript(schema.read_text(encoding="utf-8"))

    def query(self, sql, params=None):
        import sqlite3

        from cyris.adapters.store.d1 import QueryResult

        # The REST endpoint runs several statements in one POST (measured
        # 2026-09-04); sqlite3.execute takes exactly one, so fall back rather
        # than let the fake reject what production accepts. Only for *that*
        # ProgrammingError: the other one it raises is a bound-parameter count
        # mismatch, which D1 answers with HTTP 400, and executescript would
        # quietly run it with NULLs — turning a real bug into a passing test.
        try:
            cursor = self._conn.execute(sql, params or [])
        except sqlite3.ProgrammingError as e:
            if "one statement at a time" not in str(e):
                raise
            self._conn.executescript(sql)
            self._conn.commit()
            return QueryResult(rows=[], changes=0)
        rows = [dict(row) for row in cursor.fetchall()]
        self._conn.commit()
        return QueryResult(rows=rows, changes=max(cursor.rowcount, 0))


class CountingD1(SqliteD1):
    """SqliteD1 that counts queries, so a per-row-write regression fails a test."""

    def __init__(self) -> None:
        super().__init__()
        self.query_count = 0

    def query(self, sql, params=None):
        self.query_count += 1
        return super().query(sql, params)


class CompoundSelectLimitedD1(SqliteD1):
    """SqliteD1 that also enforces D1's compound-SELECT ceiling.

    stdlib sqlite3 allows 500 `UNION ALL` terms, so a statement that D1 rejects
    passes silently here. The real limit, measured against the live database on
    2026-08-29, is **5** — which is how `update_scores` shipped a statement that
    every scoring run past five articles died on, with the whole batch's scores
    and tags lost to the caller's `except`.
    """

    LIMIT = 5

    def query(self, sql, params=None):
        from cyris.adapters.store.d1 import D1Error

        if sql.upper().count(" UNION ALL ") >= self.LIMIT:
            raise D1Error("HTTP 400: too many terms in compound SELECT: SQLITE_ERROR")
        return super().query(sql, params)
