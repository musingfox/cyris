"""Blocking D1 client over Cloudflare's HTTP query API.

`ArticleRepository` is a synchronous Protocol and `run_digest` calls it without
`await`, so this client is deliberately blocking. Making it async would push
`async` up through every call site and into `service_layer/`, which the cloud
migration promises not to touch. See docs/cloud-migration.md, constraint 1.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)

API_ROOT = "https://api.cloudflare.com/client/v4"
TIMEOUT_SECONDS = 60

# D1 binds at most 100 parameters per statement, so every multi-row write here
# is chunked against this budget rather than sent as one statement.
MAX_BOUND_PARAMS = 100

MAX_ATTEMPTS = 4
RETRY_BACKOFF_SECONDS = 2


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
        body = self._post_with_retries({"sql": sql, "params": params or []})

        if not body.get("success"):
            errors = body.get("errors") or [{"message": "unknown error"}]
            raise D1Error("; ".join(str(e.get("message", e)) for e in errors))

        rows: list[dict[str, Any]] = []
        changes = 0
        for statement in body.get("result", []):
            rows.extend(statement.get("results") or [])
            changes += (statement.get("meta") or {}).get("changes", 0) or 0
        return QueryResult(rows=rows, changes=changes)

    def _post_with_retries(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Retry transport failures, because a bulk write is thousands of requests.

        A migration of the whole store is ~1,800 statements; at any realistic
        per-request failure rate, "give up on the first timeout" means it never
        finishes. A 4xx is not retried: the request itself is wrong, and repeating
        it only delays saying so.
        """
        last: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                resp = self._http.post(self._url, json=payload)
            except httpx.HTTPError as e:
                last = e
            else:
                # D1 puts the reason a statement was rejected in the body, so read
                # it before deciding anything — raise_for_status() would throw the
                # only useful part of a 400 away.
                if resp.status_code < 400:
                    return resp.json()
                if resp.status_code < 500:
                    raise D1Error(_message(resp))
                last = httpx.HTTPStatusError(_message(resp), request=resp.request, response=resp)

            if attempt < MAX_ATTEMPTS - 1:
                logger.warning("D1 request failed (%s); retrying", last)
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
        raise D1Error(f"D1 request failed after {MAX_ATTEMPTS} attempts: {last}")

    def close(self) -> None:
        self._http.close()


def _message(resp: httpx.Response) -> str:
    """The reason D1 gives, falling back to the status line when there isn't one."""
    try:
        errors = resp.json().get("errors") or []
    except ValueError:
        errors = []
    detail = "; ".join(str(e.get("message", e)) for e in errors)
    return f"HTTP {resp.status_code}: {detail}" if detail else f"HTTP {resp.status_code}"


def chunk_rows(rows: list[Any], params_per_row: int) -> list[list[Any]]:
    """Split rows so each statement stays inside D1's bound-parameter budget."""
    per_statement = max(1, MAX_BOUND_PARAMS // params_per_row)
    return [rows[i : i + per_statement] for i in range(0, len(rows), per_statement)]
