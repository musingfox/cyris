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


# The hosted Worker's route into one tenant's database for the operator, behind its Access check
# (`query` in workers/app/src/hosted_router.js). It answers as the gateway does.
OPERATOR_ROUTE = "/hosted/tenants/{tenant}/query"

# A refusal of the sign-in itself, never retried: a redirect is Access sending the request to
# its login page, 401 is Access refusing the token, 403 is the Worker refusing the JWT.
_SIGN_IN_REFUSALS = {
    302: "Access redirected to its login page: no Service Auth policy admits this service token",
    401: "Access refused the service token: it is expired, or not this application's",
    403: "the hosted Worker refused the Access sign-in",
}


class OperatorQueryError(TenantDbError):
    """The operator's query into a tenant was refused, or could not be made."""


class OperatorD1Client(GatewayD1Client):
    """Execute SQL against one hosted tenant's Durable Object as the operator, from outside.

    Every query goes to the hosted Worker's `OPERATOR_ROUTE` on the hosted domain, signed in with
    the operator's Access service token (ADR-0036), and keeps `GatewayD1Client`'s reading of the
    answer and `D1Client`'s retry rules. Without the token nothing is sent: there is no second
    credential to fall back to, and an unsigned request would only meet Access's login page.
    """

    def __init__(
        self,
        tenant_id: str,
        hosted_domain: str,
        client_id: str,
        client_secret: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not (client_id.strip() and client_secret.strip()):
            raise OperatorQueryError(
                f"tenant {tenant_id}: no operator credential — set CYRIS_ACCESS_CLIENT_ID and "
                "CYRIS_ACCESS_CLIENT_SECRET to an Access service token the hosted Worker admits"
            )
        self.tenant_id = tenant_id
        self._url = f"https://{hosted_domain}{OPERATOR_ROUTE.format(tenant=tenant_id)}"
        self._http = httpx.Client(
            timeout=TIMEOUT_SECONDS,
            # A pasted trailing space or newline is not part of the token.
            headers={
                "CF-Access-Client-Id": client_id.strip(),
                "CF-Access-Client-Secret": client_secret.strip(),
            },
            # Access's Location is its login page, not this tenant's answer.
            follow_redirects=False,
            event_hooks={"response": [self._refuse_unanswered]},
            transport=transport,
        )

    def query(self, sql: str, params: list[Any] | None = None) -> QueryResult:
        try:
            return super().query(sql, params)
        except OperatorQueryError:
            raise
        except TenantDbError as e:
            raise OperatorQueryError(str(e)) from e

    def _refuse_unanswered(self, response: httpx.Response) -> None:
        """Raise at once on an answer `post_with_retries` would misread.

        That is a sign-in refusal, a refusal the route states as `error`, or a 2xx that is not
        JSON. `post_with_retries` reads D1's `errors` shape, which the route's own query
        answers use. The registry's 404 and the Access check's 403 come before the query in
        another shape, and a redirect is not an answer at all.
        """
        status = response.status_code
        if status < 300:
            if "json" not in response.headers.get("content-type", ""):
                raise OperatorQueryError(
                    f"tenant {self.tenant_id} via {self._url}: HTTP {status} without JSON, so no "
                    "hosted Worker answered; check CYRIS_STORE_HOSTED_DOMAIN"
                )
            return
        if status >= 500:
            return
        response.read()
        try:
            body = response.json()
        except ValueError:
            body = None
        body = body if isinstance(body, dict) else {}
        if body.get("errors"):
            return
        stated = body.get("detail") or body.get("error") or ""
        reason = _SIGN_IN_REFUSALS.get(status, "")
        detail = "; ".join(part for part in (reason, str(stated)) if part)
        raise OperatorQueryError(
            f"tenant {self.tenant_id} via {self._url}: HTTP {status}"
            + (f": {detail}" if detail else "")
        )
