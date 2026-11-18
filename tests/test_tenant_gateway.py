"""A hosted tenant's run reaches its own database through the Worker's gateway, as D1 is reached.

`GatewayD1Client` answers the `D1Queryable` seam every store talks through, so it must hand
back what `D1Client` hands back for the same SQL, and keep `D1Client`'s retry rules: a
transport failure or a 5xx is retried with the same backoff, and a 4xx, a refused tenant
included, is raised at once with the reason the gateway gave.
"""

import json

import httpx
import pytest
from fakes import SqliteD1

from cyris.adapters.store import d1
from cyris.adapters.store.d1 import MAX_ATTEMPTS, RETRY_BACKOFF_SECONDS, D1Error, QueryResult
from cyris.adapters.store.tenant_gateway import GATEWAY_ORIGIN, GatewayD1Client, TenantDbError

pytestmark = pytest.mark.unit


class FakeGateway:
    """Answers `POST /t/<tenant>/query` as the hosted Worker's gateway does, over real SQL.

    Only `tenant` is this run's; any other is refused with 403, as the gateway refuses a
    tenant its props do not name. A bound value a Durable Object's SQL cannot take is refused
    as it would be: `SqlStorageValue` is a string, a number, an ArrayBuffer or null, so a
    JSON boolean is not one. `failures` are answered before the first real query, and every
    statement refused is kept in `refused`.
    """

    def __init__(self, tenant: str, *, failures: list = ()) -> None:
        self.tenant = tenant
        self.db = SqliteD1()
        self.failures = list(failures)
        self.requests: list[httpx.Request] = []
        self.refused: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.failures:
            failure = self.failures.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return failure
        parts = request.url.path.strip("/").split("/")
        if request.method != "POST" or len(parts) != 3 or parts[0] != "t" or parts[2] != "query":
            return _errors(404, "POST /t/<tenant>/query")
        if parts[1] != self.tenant:
            return _errors(403, f"tenant {parts[1]} is not this run's")
        body = json.loads(request.content)
        unbindable = [p for p in body["params"] if isinstance(p, bool | dict | list)]
        try:
            if unbindable:
                raise TypeError(f"a Durable Object cannot bind {unbindable!r}")
            result = self.db.query(body["sql"], body["params"])
        except Exception as e:  # noqa: BLE001 - the gateway answers a failed statement with 400
            self.refused.append(f"{body['sql'][:80]}: {e}")
            return _errors(400, str(e))
        return httpx.Response(200, json={"rows": result.rows, "changes": result.changes})


def _errors(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"errors": [{"message": message}]})


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The retry backoff, recorded instead of slept."""
    delays: list[float] = []
    monkeypatch.setattr(d1.time, "sleep", delays.append)
    return delays


def _client(gateway: FakeGateway, tenant: str = "alpha") -> GatewayD1Client:
    client = GatewayD1Client(tenant)
    client._http = httpx.Client(transport=httpx.MockTransport(gateway))
    return client


def test_a_query_goes_to_this_tenants_path_on_the_intercepted_host() -> None:
    gateway = FakeGateway("alpha")

    _client(gateway).query("SELECT 1 AS one")

    [request] = gateway.requests
    assert str(request.url) == f"{GATEWAY_ORIGIN}/t/alpha/query"
    assert GATEWAY_ORIGIN == "http://db.cyris"
    assert json.loads(request.content) == {"sql": "SELECT 1 AS one", "params": []}


def test_rows_and_changes_come_back_as_d1_hands_them_over() -> None:
    gateway = FakeGateway("alpha")
    client = _client(gateway)

    written = client.query(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
        ["general.timezone", '"UTC"', "2026-11-18T00:00:00+00:00"],
    )
    read = client.query("SELECT key, value FROM settings WHERE key = ?", ["general.timezone"])

    assert written == QueryResult(rows=[], changes=1)
    assert read == QueryResult(rows=[{"key": "general.timezone", "value": '"UTC"'}], changes=0)


def test_a_server_error_is_retried_with_d1s_backoff(sleeps: list[float]) -> None:
    gateway = FakeGateway("alpha", failures=[_errors(503, "reset"), _errors(502, "bad gateway")])

    result = _client(gateway).query("SELECT 1 AS one")

    assert result.rows == [{"one": 1}]
    assert len(gateway.requests) == 3
    assert sleeps == [RETRY_BACKOFF_SECONDS * 1, RETRY_BACKOFF_SECONDS * 2]


def test_a_transport_failure_is_retried_and_then_named(sleeps: list[float]) -> None:
    refused = httpx.ConnectError("connection refused")
    gateway = FakeGateway("alpha", failures=[refused] * MAX_ATTEMPTS)

    with pytest.raises(TenantDbError, match=f"after {MAX_ATTEMPTS} attempts.*connection refused"):
        _client(gateway).query("SELECT 1")

    assert len(gateway.requests) == MAX_ATTEMPTS
    assert len(sleeps) == MAX_ATTEMPTS - 1


def test_a_rejected_statement_is_raised_at_once_with_the_gateways_reason(
    sleeps: list[float],
) -> None:
    gateway = FakeGateway("alpha")

    with pytest.raises(TenantDbError, match="HTTP 400: no such table: nowhere"):
        _client(gateway).query("SELECT * FROM nowhere")

    assert len(gateway.requests) == 1
    assert sleeps == []


def test_another_tenants_database_is_refused_and_not_retried(sleeps: list[float]) -> None:
    gateway = FakeGateway("alpha")

    with pytest.raises(TenantDbError, match="HTTP 403: tenant bravo is not this run's"):
        _client(gateway, tenant="bravo").query("SELECT 1")

    assert len(gateway.requests) == 1
    assert sleeps == []


def test_a_value_a_durable_object_cannot_bind_is_refused_as_it_would_be() -> None:
    gateway = FakeGateway("alpha")

    with pytest.raises(TenantDbError, match="cannot bind"):
        _client(gateway).query("SELECT ? AS flag", [True])

    assert len(gateway.refused) == 1


def test_an_answer_without_rows_and_changes_is_an_error_not_an_empty_table() -> None:
    gateway = FakeGateway("alpha", failures=[httpx.Response(200, json={"ok": True})])

    with pytest.raises(TenantDbError, match="no rows and changes"):
        _client(gateway).query("SELECT key FROM settings")


def test_its_errors_are_d1_errors_so_every_caller_of_the_store_handles_them() -> None:
    assert issubclass(TenantDbError, D1Error)
