"""A hosted tenant's SQL store: its own SQLite Durable Object, reached through the hosted Worker.

The hosted Worker starts each tenant's run container with the tenant's id and registers an
interception for `GATEWAY_ORIGIN` before the container starts, so a request from here to
`http://db.cyris/t/<tenant>/query` never leaves the machine: the Worker's gateway answers it
from that tenant's Durable Object, and refuses any other tenant with 403. The container holds
no token and no binding for it. Why a Durable Object per tenant and not D1:
docs/decisions/0028-one-sqlite-durable-object-per-hosted-tenant.md.

Blocking for the same reason `D1Client` is (ADR-0002), and with the same retry rules
(`post_with_retries`), so every store above `D1Queryable` runs unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from cyris.adapters.store.d1 import (
    TIMEOUT_SECONDS,
    D1Error,
    D1Queryable,
    QueryResult,
    post_with_retries,
)

# The host the hosted Worker intercepts. Plain http: the request is answered inside the
# Workers runtime and never crosses a network. `DB_HOST` in workers/app/src/tenant_db.js is
# the same name; tests/test_hosted_config.py holds the two together.
GATEWAY_ORIGIN = "http://db.cyris"

# The tables only a hosted tenant has (its pages); the hosted Worker's seed applies the same file.
HOSTED_SCHEMA_PATH = Path(__file__).with_name("hosted_schema.sql")


def apply_hosted_schema(client: D1Queryable) -> None:
    """Create a hosted tenant's own tables where they are missing; one idempotent query."""
    client.query(HOSTED_SCHEMA_PATH.read_text(encoding="utf-8"))


class TenantDbError(D1Error):
    """The tenant database refused a query, or the gateway could not be reached."""


class GatewayD1Client:
    """Execute SQL against one hosted tenant's Durable Object through the Worker's gateway."""

    def __init__(self, tenant_id: str, origin: str = GATEWAY_ORIGIN) -> None:
        self.tenant_id = tenant_id
        self._url = f"{origin}/t/{tenant_id}/query"
        # One client, so writes chunked into several statements reuse the connection.
        self._http = httpx.Client(timeout=TIMEOUT_SECONDS)

    def query(self, sql: str, params: list[Any] | None = None) -> QueryResult:
        body = post_with_retries(
            self._http,
            self._url,
            {"sql": sql, "params": params or []},
            what=f"tenant {self.tenant_id} database",
            error=TenantDbError,
        )
        rows = body.get("rows")
        changes = body.get("changes")
        # A body without both is not the gateway's answer, and an empty table must
        # never be what a malformed one reads as.
        if not isinstance(rows, list) or not isinstance(changes, int):
            raise TenantDbError(f"tenant {self.tenant_id} database answered no rows and changes")
        return QueryResult(rows=rows, changes=changes)

    def close(self) -> None:
        self._http.close()
