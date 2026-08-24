"""Blocking D1 client over Cloudflare's HTTP query API.

`ArticleRepository` is a synchronous Protocol and `run_digest` calls it without
`await`, so this client is deliberately blocking. Making it async would push
`async` up through every call site and into `service_layer/`, which the cloud
migration promises not to touch. See docs/cloud-migration.md, constraint 1.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)

API_ROOT = "https://api.cloudflare.com/client/v4"
TIMEOUT_SECONDS = 60

# D1 binds at most 100 parameters per statement, so every multi-row write here
# is chunked against this budget rather than sent as one statement.
MAX_BOUND_PARAMS = 100


class D1Error(RuntimeError):
    """A D1 query was rejected, or the API call itself failed."""


@dataclass(frozen=True)
class QueryResult:
    rows: list[dict[str, Any]]
    changes: int


class D1Queryable(Protocol):
    """The one seam the store talks through, so tests can run real SQL locally."""

    def query(self, sql: str, params: list[Any] | None = None) -> QueryResult: ...


class D1Client:
    """Execute SQL against a D1 database over the Cloudflare REST API."""

    def __init__(self, account_id: str, database_id: str, api_token: str) -> None:
        self._url = f"{API_ROOT}/accounts/{account_id}/d1/database/{database_id}/query"
        # One client, so writes chunked into several statements reuse the connection.
        self._http = httpx.Client(
            timeout=TIMEOUT_SECONDS,
            headers={"Authorization": f"Bearer {api_token}"},
        )

    def query(self, sql: str, params: list[Any] | None = None) -> QueryResult:
        try:
            resp = self._http.post(self._url, json={"sql": sql, "params": params or []})
            resp.raise_for_status()
            body = resp.json()
        except httpx.HTTPError as e:
            raise D1Error(f"D1 request failed: {e}") from e

        if not body.get("success"):
            errors = body.get("errors") or [{"message": "unknown error"}]
            raise D1Error("; ".join(str(e.get("message", e)) for e in errors))

        rows: list[dict[str, Any]] = []
        changes = 0
        for statement in body.get("result", []):
            rows.extend(statement.get("results") or [])
            changes += (statement.get("meta") or {}).get("changes", 0) or 0
        return QueryResult(rows=rows, changes=changes)

    def close(self) -> None:
        self._http.close()


def chunk_rows(rows: list[Any], params_per_row: int) -> list[list[Any]]:
    """Split rows so each statement stays inside D1's bound-parameter budget."""
    per_statement = max(1, MAX_BOUND_PARAMS // params_per_row)
    return [rows[i : i + per_statement] for i in range(0, len(rows), per_statement)]
