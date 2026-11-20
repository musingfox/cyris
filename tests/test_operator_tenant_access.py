"""The operator reaches one hosted tenant's database from outside its run, and only signed in.

`cyris --tenant <id>` makes the store that tenant's Durable Object, reached through the hosted
Worker's `POST /hosted/tenants/<id>/query` on the hosted domain with the operator's Access
service token (ADR-0036). Every command, `doctor` included, then reads and writes that tenant's
rows as its own run would. Without the token nothing is sent; a token the Worker refuses is
named at once and never retried. A run container, which names its tenant with
`CYRIS_STORE_TENANT_ID`, keeps its gateway and cannot also take the operator's name.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fakes import SqliteD1, seed_d1_settings
from typer.testing import CliRunner

from cyris import bootstrap
from cyris.adapters.store import tenant_gateway
from cyris.adapters.store.d1 import MAX_ATTEMPTS
from cyris.adapters.store.d1_store import D1ArticleStore
from cyris.adapters.store.runs import D1RunLog
from cyris.adapters.store.source_store import D1SourceStore
from cyris.adapters.store.tenant_gateway import (
    OPERATOR_ROUTE,
    GatewayD1Client,
    OperatorD1Client,
    OperatorQueryError,
)
from cyris.config import (
    OPERATOR_TENANT_ENV_VAR,
    OPERATOR_TOKEN_ENV_VARS,
    SourceConfig,
    read_config_files,
    resolve_config,
)
from cyris.domain.models import Article, ArticleState, Lane
from cyris.entrypoints.cli import app

pytestmark = pytest.mark.integration

DOMAIN = "cyris-hosted.trial.workers.dev"
CLIENT_ID = "operator.access"
CLIENT_SECRET = "operator-secret"


class FakeHostedWorker:
    """Answers `POST /hosted/tenants/<tenant>/query` as hosted_router.js does, over real SQL.

    A request without the operator's service token is refused 403 in the Worker's shape, as a
    request Access passed no JWT for is. A tenant the registry does not hold is refused 404
    before any database is reached, and a statement SQLite refuses is 400 in D1's shape.
    """

    def __init__(self, *tenants: str) -> None:
        self.tenants = {tenant: SqliteD1() for tenant in tenants}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        token = (
            request.headers.get("CF-Access-Client-Id"),
            request.headers.get("CF-Access-Client-Secret"),
        )
        if token != (CLIENT_ID, CLIENT_SECRET):
            detail = "Sign in through Cloudflare Access to reach this page."
            return httpx.Response(403, json={"error": "forbidden", "detail": detail})
        parts = request.url.path.strip("/").split("/")
        if request.url.host != DOMAIN or request.method != "POST" or parts[-1] != "query":
            return httpx.Response(404, json={"error": "not found"})
        db = self.tenants.get(parts[2])
        if db is None:
            return httpx.Response(404, json={"error": f"{parts[2]} is not a registered tenant"})
        body = json.loads(request.content)
        try:
            result = db.query(body["sql"], body["params"])
        except Exception as e:  # noqa: BLE001 - the route answers a failed statement with 400
            return httpx.Response(400, json={"errors": [{"message": str(e)}]})
        return httpx.Response(200, json={"rows": result.rows, "changes": result.changes})


def _client(worker, tenant: str = "alpha", **token: str) -> OperatorD1Client:
    credentials = {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, **token}
    return OperatorD1Client(tenant, DOMAIN, **credentials, transport=httpx.MockTransport(worker))


@pytest.fixture
def operator_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator's machine: the hosted domain and the service token, and no run's tenant."""
    monkeypatch.delenv("CYRIS_STORE_TENANT_ID", raising=False)
    # Set blank, so the value `--tenant` writes is undone when the test ends.
    monkeypatch.setenv(OPERATOR_TENANT_ENV_VAR, "")
    monkeypatch.setenv("CYRIS_STORE_HOSTED_DOMAIN", DOMAIN)
    monkeypatch.setenv(OPERATOR_TOKEN_ENV_VARS[0], CLIENT_ID)
    monkeypatch.setenv(OPERATOR_TOKEN_ENV_VARS[1], CLIENT_SECRET)


def _through(worker: FakeHostedWorker, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every operator client `build_d1_client` makes talks to `worker`."""
    real = OperatorD1Client

    def client(*args, **kwargs) -> OperatorD1Client:
        return real(*args, **kwargs, transport=httpx.MockTransport(worker))

    monkeypatch.setattr(tenant_gateway, "OperatorD1Client", client)


def _files(tmp_path: Path, store: str = "") -> tuple[Path, Path]:
    config_path = tmp_path / "cyris.toml"
    config_path.write_text(store, encoding="utf-8")
    return config_path, tmp_path / "sources.yaml"


def _store(tmp_path: Path, store: str = ""):
    return resolve_config(read_config_files(*_files(tmp_path, store)), d1_settings={}).app.store


# The client


def test_a_query_goes_to_the_tenants_operator_route_signed_in_with_the_service_token() -> None:
    worker = FakeHostedWorker("alpha")

    result = _client(worker).query("SELECT ? AS one", [1])

    assert result.rows == [{"one": 1}]
    [request] = worker.requests
    assert str(request.url) == f"https://{DOMAIN}{OPERATOR_ROUTE.format(tenant='alpha')}"
    assert request.url.path == "/hosted/tenants/alpha/query"
    assert request.headers["CF-Access-Client-Id"] == CLIENT_ID
    assert request.headers["CF-Access-Client-Secret"] == CLIENT_SECRET
    assert json.loads(request.content) == {"sql": "SELECT ? AS one", "params": [1]}


def test_writes_land_in_the_named_tenants_database_and_no_other() -> None:
    worker = FakeHostedWorker("alpha", "bravo")

    _client(worker, "alpha").query(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
        ["general.timezone", '"Asia/Taipei"', "2026-11-20T00:00:00+00:00"],
    )

    read = "SELECT value FROM settings WHERE key = 'general.timezone'"
    assert worker.tenants["alpha"].query(read).rows == [{"value": '"Asia/Taipei"'}]
    assert worker.tenants["bravo"].query(read).rows == []


@pytest.mark.parametrize(
    "token", [{"client_id": ""}, {"client_secret": ""}, {"client_id": " ", "client_secret": "\n"}]
)
def test_without_the_service_token_nothing_is_sent(token: dict[str, str]) -> None:
    worker = FakeHostedWorker("alpha")

    with pytest.raises(OperatorQueryError, match="no operator credential"):
        _client(worker, **token)

    assert worker.requests == []


def test_a_token_the_worker_refuses_is_named_at_once_and_never_retried() -> None:
    worker = FakeHostedWorker("alpha")

    with pytest.raises(OperatorQueryError, match="HTTP 403: the hosted Worker refused") as caught:
        _client(worker, client_secret="someone-elses").query("SELECT 1")

    assert "Sign in through Cloudflare Access" in str(caught.value)
    assert len(worker.requests) == 1


def test_an_access_redirect_is_named_and_not_followed() -> None:
    requests: list[httpx.Request] = []

    def access(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        login = "https://team.cloudflareaccess.com/cdn-cgi/access/login"
        return httpx.Response(302, headers={"Location": login}, text="<html>")

    with pytest.raises(OperatorQueryError, match="HTTP 302: Access redirected to its login page"):
        _client(access).query("SELECT 1")

    assert len(requests) == 1


def test_a_tenant_the_registry_does_not_hold_is_named() -> None:
    worker = FakeHostedWorker("alpha")

    with pytest.raises(OperatorQueryError, match="HTTP 404: charlie is not a registered tenant"):
        _client(worker, "charlie").query("SELECT 1")

    assert len(worker.requests) == 1


def test_a_refused_statement_keeps_the_routes_reason() -> None:
    worker = FakeHostedWorker("alpha")

    with pytest.raises(OperatorQueryError, match="HTTP 400: no such table: nowhere"):
        _client(worker).query("SELECT * FROM nowhere")


def test_an_answer_that_is_not_json_is_no_hosted_workers() -> None:
    def elsewhere(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>some other site</html>")

    with pytest.raises(OperatorQueryError, match="without JSON.*CYRIS_STORE_HOSTED_DOMAIN"):
        _client(elsewhere).query("SELECT 1")


def test_a_server_error_is_retried_as_the_gateways_are(monkeypatch: pytest.MonkeyPatch) -> None:
    from cyris.adapters.store import d1

    monkeypatch.setattr(d1.time, "sleep", lambda _seconds: None)
    calls: list[httpx.Request] = []

    def down(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503, json={"errors": [{"message": "reset"}]})

    with pytest.raises(OperatorQueryError, match=f"after {MAX_ATTEMPTS} attempts"):
        _client(down).query("SELECT 1")

    assert len(calls) == MAX_ATTEMPTS


# Choosing the operator's store


def test_the_operator_tenant_makes_the_store_that_tenants_whatever_the_file_says(
    tmp_path: Path, operator_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(OPERATOR_TENANT_ENV_VAR, "alpha")

    store = _store(tmp_path, '[store]\nbackend = "sqlite"\n')

    assert (store.tenant_id, store.operator, store.backend) == ("alpha", True, "d1")
    assert store.is_hosted
    assert store.sql_home == "tenant alpha's database"


def test_a_run_containers_tenant_keeps_its_gateway(
    tmp_path: Path, operator_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CYRIS_STORE_BACKEND", "d1")
    monkeypatch.setenv("CYRIS_STORE_TENANT_ID", "alpha")

    store = _store(tmp_path)

    assert (store.tenant_id, store.operator) == ("alpha", False)


def test_the_operator_cannot_name_a_tenant_inside_a_run_container(
    tmp_path: Path, operator_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CYRIS_STORE_TENANT_ID", "alpha")
    monkeypatch.setenv(OPERATOR_TENANT_ENV_VAR, "bravo")

    with pytest.raises(ValueError, match="are both set"):
        _store(tmp_path)


@pytest.mark.parametrize(
    ("store", "tenant", "refusal"),
    [
        ("[store]\noperator = true\n", "", "operator cannot be set in a config file"),
        ("", "../bravo", "must match"),
        ("", "Alpha", "must match"),
    ],
)
def test_the_operator_is_named_only_by_a_valid_tenant_id(
    tmp_path: Path,
    operator_env: None,
    monkeypatch: pytest.MonkeyPatch,
    store: str,
    tenant: str,
    refusal: str,
) -> None:
    monkeypatch.setenv(OPERATOR_TENANT_ENV_VAR, tenant)

    with pytest.raises(ValueError, match=refusal):
        _store(tmp_path, store)


def test_the_operator_needs_the_hosted_domain(
    tmp_path: Path, operator_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CYRIS_STORE_HOSTED_DOMAIN")
    monkeypatch.setenv(OPERATOR_TENANT_ENV_VAR, "alpha")

    with pytest.raises(ValueError, match="needs CYRIS_STORE_HOSTED_DOMAIN"):
        _store(tmp_path)


def test_build_d1_client_gives_the_operator_its_route_and_a_run_its_gateway(
    tmp_path: Path, operator_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(OPERATOR_TENANT_ENV_VAR, "alpha")
    operator_cfg = resolve_config(read_config_files(*_files(tmp_path)), d1_settings={})
    monkeypatch.setenv(OPERATOR_TENANT_ENV_VAR, "")
    monkeypatch.setenv("CYRIS_STORE_BACKEND", "d1")
    monkeypatch.setenv("CYRIS_STORE_TENANT_ID", "alpha")
    run_cfg = resolve_config(read_config_files(*_files(tmp_path)), d1_settings={})

    operator = bootstrap.build_d1_client(operator_cfg)
    run = bootstrap.build_d1_client(run_cfg)

    assert isinstance(operator, OperatorD1Client)
    assert operator._url == f"https://{DOMAIN}/hosted/tenants/alpha/query"
    assert type(run) is GatewayD1Client


def test_build_d1_client_refuses_the_operator_without_the_service_token(
    tmp_path: Path, operator_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in OPERATOR_TOKEN_ENV_VARS:
        monkeypatch.delenv(name)
    monkeypatch.setenv(OPERATOR_TENANT_ENV_VAR, "alpha")
    cfg = resolve_config(read_config_files(*_files(tmp_path)), d1_settings={})

    with pytest.raises(ValueError, match=", ".join(OPERATOR_TOKEN_ENV_VARS)):
        bootstrap.build_d1_client(cfg)


# The CLI


def _seeded(worker: FakeHostedWorker) -> None:
    """Each tenant with every setting and a source of its own, as the operator's seed leaves it."""
    for tenant, db in worker.tenants.items():
        seed_d1_settings(db)
        D1SourceStore(db).upsert(
            SourceConfig(name=f"{tenant} feed", url=f"https://{tenant}.test/feed", lane=Lane.SKIM)
        )


def test_cyris_tenant_reads_that_tenants_rows_through_the_worker(
    tmp_path: Path, operator_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = FakeHostedWorker("alpha", "bravo")
    _seeded(worker)
    _through(worker, monkeypatch)
    config_path, sources_path = _files(tmp_path)

    result = CliRunner().invoke(
        app,
        ["--tenant", "alpha", "sources", "list"]
        + ["--config", str(config_path), "--sources", str(sources_path)],
    )

    assert result.exit_code == 0, result.output
    assert "alpha feed" in result.output
    assert "bravo feed" not in result.output
    assert {r.url.path for r in worker.requests} == {"/hosted/tenants/alpha/query"}


def test_cyris_tenant_writes_that_tenants_rows_through_the_worker(
    tmp_path: Path, operator_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = FakeHostedWorker("alpha", "bravo")
    _seeded(worker)
    url = "https://alpha.test/one"
    for db in worker.tenants.values():
        D1ArticleStore(db).save(
            [
                Article(
                    id=1,
                    title="One",
                    url=url,
                    content="Body.",
                    published_at=datetime.now(UTC),
                    source_name="feed",
                    source_lane=Lane.SKIM,
                )
            ]
        )
    _through(worker, monkeypatch)
    config_path, sources_path = _files(tmp_path)

    result = CliRunner().invoke(
        app,
        ["--tenant", "alpha", "articles", "accept", url]
        + ["--config", str(config_path), "--sources", str(sources_path)],
    )

    assert result.exit_code == 0, result.output
    assert "Accepted 1 article(s)." in result.output
    [alpha] = D1ArticleStore(worker.tenants["alpha"]).get_by_urls([url])
    [bravo] = D1ArticleStore(worker.tenants["bravo"]).get_by_urls([url])
    assert (alpha.state, bravo.state) == (ArticleState.ACCEPTED, ArticleState.PENDING)


def test_cyris_tenant_without_the_service_token_is_refused_and_sends_nothing(
    tmp_path: Path, operator_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in OPERATOR_TOKEN_ENV_VARS:
        monkeypatch.delenv(name)
    worker = FakeHostedWorker("alpha")
    _seeded(worker)
    _through(worker, monkeypatch)
    config_path, sources_path = _files(tmp_path)

    result = CliRunner().invoke(
        app,
        ["--tenant", "alpha", "doctor"]
        + ["--config", str(config_path), "--sources", str(sources_path)],
    )

    assert result.exit_code == 1
    assert "CYRIS_ACCESS_CLIENT_ID, CYRIS_ACCESS_CLIENT_SECRET" in result.output
    assert worker.requests == []


@pytest.mark.parametrize("tenant", ["", "Alpha", "../bravo", "a" * 41])
def test_an_empty_or_invalid_tenant_is_refused_at_the_option(
    tmp_path: Path, operator_env: None, monkeypatch: pytest.MonkeyPatch, tenant: str
) -> None:
    worker = FakeHostedWorker("alpha")
    _seeded(worker)
    _through(worker, monkeypatch)
    config_path, sources_path = _files(tmp_path)

    result = CliRunner().invoke(
        app,
        ["--tenant", tenant, "sources", "list"]
        + ["--config", str(config_path), "--sources", str(sources_path)],
    )

    assert result.exit_code == 2, result.output
    assert "a tenant id matches" in result.output
    # Not the operator's own store either: nothing ran past the option.
    assert "sources in" not in result.output
    assert os.environ[OPERATOR_TENANT_ENV_VAR] == ""
    assert worker.requests == []


def test_cyris_tenant_with_a_refused_token_names_the_operators_way_in(
    tmp_path: Path, operator_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(OPERATOR_TOKEN_ENV_VARS[1], "someone-elses")
    worker = FakeHostedWorker("alpha")
    _through(worker, monkeypatch)
    config_path, sources_path = _files(tmp_path)

    result = CliRunner().invoke(
        app,
        ["--tenant", "alpha", "doctor"]
        + ["--config", str(config_path), "--sources", str(sources_path)],
    )

    assert result.exit_code == 1
    assert "✗ tenant database — tenant alpha via" in result.output
    assert "HTTP 403" in result.output
    assert "The operator reaches a tenant through the hosted Worker" in result.output
    assert len(worker.requests) == 1


def test_cyris_tenant_doctor_reports_on_that_tenant(
    tmp_path: Path, operator_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # This machine's own keys and Workers are not the tenant's, and are not asked about.
    for name in ("ANTHROPIC_API_KEY", "CYRIS_RSS_WORKER_URL", "CLOUDFLARE_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    worker = FakeHostedWorker("alpha", "bravo")
    _seeded(worker)
    model = {"llm_provider.provider": "anthropic", "llm_provider.model": "claude-haiku-5-5"}
    seed_d1_settings(worker.tenants["alpha"], **model)
    D1RunLog(worker.tenants["alpha"], "").record(
        {"status": "ok", "period": "morning", "dry_run": False}
    )
    _through(worker, monkeypatch)
    config_path, sources_path = _files(tmp_path)

    result = CliRunner().invoke(
        app,
        ["--tenant", "alpha", "doctor"]
        + ["--config", str(config_path), "--sources", str(sources_path)],
    )

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == f"✓ tenant — alpha, read as the operator on {DOMAIN}"
    home = "tenant alpha's database"
    assert f"✓ sources — 1 RSS, 0 email-only, 0 on the read lane — from {home}" in lines
    assert any(line.startswith("✓ settings — ") and home in line for line in lines)
    assert "✓ llm provider — anthropic · claude-haiku-5-5, keyed by the hosted Worker" in lines
    assert any(line.startswith("✓ last run — ok at ") for line in lines)
    assert any(line.startswith("– this machine — not asked") for line in lines)
    machine = ("✓ rss buffer", "! rss buffer", "✗ digest output")
    assert not any(line.startswith(machine) for line in lines)
    assert "Ready to run." in result.output
    assert {r.url.path for r in worker.requests} == {"/hosted/tenants/alpha/query"}
    assert all(r.headers["CF-Access-Client-Id"] == CLIENT_ID for r in worker.requests)


def test_cyris_tenant_doctor_names_a_tenant_with_no_run_yet(
    tmp_path: Path, operator_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = FakeHostedWorker("alpha")
    _seeded(worker)
    _through(worker, monkeypatch)
    config_path, sources_path = _files(tmp_path)

    result = CliRunner().invoke(
        app,
        ["--tenant", "alpha", "doctor"]
        + ["--config", str(config_path), "--sources", str(sources_path)],
    )

    assert result.exit_code == 0, result.output
    assert "– last run — no run recorded in tenant alpha's database yet" in result.output
