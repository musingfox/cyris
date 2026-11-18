"""A hosted tenant's run: its own database through the gateway, and no channel of any other kind.

The hosted Worker starts a tenant's container with `CYRIS_STORE_TENANT_ID`. From that id alone
`bootstrap` gives the run the tenant's Durable Object through the gateway and nothing that
reaches outside it: no shared queue (the newsletter and vote Workers belong to another
deployment), and until per-tenant serving and operator alerts land, no publish and no
notification, whatever the environment or the tenant's settings name. The issue stays in the
tenant's `digests` table (ADR-0028).
"""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from fakes import TEST_SETTINGS, SqliteD1, load_files, seed_d1_settings
from test_run_digest import FakeSource
from test_tenant_gateway import FakeGateway
from typer.testing import CliRunner

from cyris import bootstrap
from cyris.adapters.fetch.rss_source import RssSource
from cyris.adapters.hosted import NOT_SENT, skip_alert, skip_digest_notification
from cyris.adapters.store import tenant_gateway
from cyris.adapters.store.d1 import D1Client, apply_schema
from cyris.adapters.store.tenant_gateway import GatewayD1Client
from cyris.domain.models import Article, Lane
from cyris.entrypoints.cli import app
from cyris.service_layer.run_digest import RunOptions, run_digest

pytestmark = pytest.mark.integration

WEBHOOK = "https://discord.com/api/webhooks/1/tok"

CONFIG = """
[agent_vault]
path = "{vault}"
"""

SOURCES = """
sources:
  - name: "A"
    url: "https://a.test/feed"
"""

# Everything a non-hosted deployment would publish, notify and drain the queues with.
EVERY_CHANNEL = {
    "CYRIS_HTML_OUTPUT_ENABLED": "true",
    "CYRIS_PROMOTE_PUBLISH_ENABLED": "true",
    "CYRIS_PROMOTE_PAGES_PROJECT": "someones-pages",
    "CYRIS_PROMOTE_WORKER_URL": "https://promote.test",
    "CYRIS_NEWSLETTER_WORKER_URL": "https://newsletter.test",
    "CYRIS_RSS_WORKER_URL": "https://rss.test",
    "CYRIS_WORKER_TOKEN": "worker-tok",
    "CYRIS_PROMOTE_TOKEN": "promote-tok",
    "CYRIS_STORE_DATABASE_ID": "someones-d1",
    "CLOUDFLARE_ACCOUNT_ID": "acct",
    "CLOUDFLARE_API_TOKEN": "api-tok",
    "CYRIS_APP_WORKER_NAME": "cyris-app",
}

NOTIFY = {
    "notify.discord_webhook_url": WEBHOOK,
    "notify.email_to": "reader@a.test",
    "notify.email_from": "cyris@a.test",
}


def _hosted_env(monkeypatch: pytest.MonkeyPatch, tenant: str = "alpha") -> None:
    monkeypatch.setenv("CYRIS_STORE_BACKEND", "d1")
    monkeypatch.setenv("CYRIS_STORE_TENANT_ID", tenant)


def _files(tmp_path: Path) -> tuple[Path, Path]:
    config_path = tmp_path / "cyris.toml"
    config_path.write_text(CONFIG.format(vault=tmp_path / "vault"), encoding="utf-8")
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text(SOURCES, encoding="utf-8")
    return config_path, sources_path


def _through(gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every gateway client `build_d1_client` makes talks to `gateway`, as the Worker's would."""
    real = GatewayD1Client

    def client(tenant_id: str) -> GatewayD1Client:
        made = real(tenant_id)
        made._http = httpx.Client(transport=httpx.MockTransport(gateway))
        return made

    monkeypatch.setattr(tenant_gateway, "GatewayD1Client", client)


def test_a_container_carrying_a_tenant_id_gets_its_gateway_and_needs_no_d1_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("CYRIS_STORE_DATABASE_ID", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    _hosted_env(monkeypatch, "bravo")
    cfg = load_files(*_files(tmp_path), TEST_SETTINGS)

    client = bootstrap.build_d1_client(cfg)

    assert isinstance(client, GatewayD1Client)
    assert client.tenant_id == "bravo"
    assert cfg.missing_store_keys() == []


def test_without_a_tenant_id_the_d1_store_is_d1_over_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CYRIS_STORE_TENANT_ID", raising=False)
    for name, value in EVERY_CHANNEL.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("CYRIS_STORE_BACKEND", "d1")

    assert isinstance(bootstrap.build_d1_client(load_files(*_files(tmp_path))), D1Client)


@pytest.mark.parametrize(
    ("backend", "tenant", "refusal"),
    [
        ("sqlite", "alpha", "needs the d1 backend"),
        ("d1", "../bravo", "must match"),
        ("d1", "Alpha", "must match"),
    ],
)
def test_a_tenant_id_off_cloudflare_or_outside_one_path_segment_stops_the_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str, tenant: str, refusal: str
) -> None:
    monkeypatch.setenv("CYRIS_STORE_BACKEND", backend)
    monkeypatch.setenv("CYRIS_STORE_TENANT_ID", tenant)

    with pytest.raises(ValueError, match=refusal):
        load_files(*_files(tmp_path))


def test_the_schema_and_its_steps_land_through_the_gateway() -> None:
    gateway = FakeGateway("alpha")
    gateway.db = fresh = SqliteD1(with_schema=False)
    client = GatewayD1Client("alpha")
    client._http = httpx.Client(transport=httpx.MockTransport(gateway))

    apply_schema(client)

    tables = {r["name"] for r in fresh.query("SELECT name FROM sqlite_master").rows}
    assert {"settings", "sources", "digests", "digest_runs", "schema_migrations"} <= tables


@pytest.fixture
def hosted_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for name, value in EVERY_CHANNEL.items():
        monkeypatch.setenv(name, value)
    _hosted_env(monkeypatch)
    gateway = FakeGateway("alpha")
    _through(gateway, monkeypatch)
    cfg = load_files(*_files(tmp_path), {**TEST_SETTINGS, **NOTIFY})
    return cfg, gateway


def test_a_hosted_run_is_wired_to_no_queue_no_page_and_no_channel(hosted_cfg) -> None:
    cfg, _ = hosted_cfg

    deps = bootstrap.build_deps(cfg)

    assert [type(s) for s in deps.fetch_sources] == [RssSource]
    assert deps.sync_promotions is None
    assert bootstrap.build_promotion_sync(cfg) is None
    assert (deps.html_writer, deps.publish_site, deps.page_serves) == (None, None, None)
    assert deps.worker_domains is None
    assert deps.send_discord is skip_digest_notification
    assert deps.send_email is skip_digest_notification
    assert (deps.send_discord_alert, deps.send_email_alert) == (skip_alert, skip_alert)


async def test_a_hosted_run_keeps_its_issue_and_neither_publishes_nor_notifies(
    hosted_cfg,
) -> None:
    cfg, gateway = hosted_cfg
    article = Article(
        id=1,
        title="A launch",
        url="https://a.test/launch",
        content="Something launched today.",
        published_at=datetime.now(UTC) - timedelta(hours=1),
        source_name="A",
        source_lane=Lane.FOLLOW,
        source_tags=["tech"],
    )
    deps = replace(bootstrap.build_deps(cfg), fetch_sources=[FakeSource([article])])

    with respx.mock:
        # Every request that leaves the process lands here; the gateway's never does.
        anywhere = respx.route().mock(return_value=httpx.Response(500))
        report = await run_digest(deps, RunOptions(period="morning"))

    assert report.status == "ok"
    assert anywhere.call_count == 0
    [issue] = gateway.db.query("SELECT date, period, content FROM digests").rows
    assert issue["period"] == "morning"
    assert article.url in issue["content"]
    [run] = gateway.db.query("SELECT status, summary FROM digest_runs").rows
    summary = json.loads(run["summary"])
    assert run["status"] == "ok"
    assert summary["discord_notification"] == NOT_SENT
    assert summary["mail_notification"] == NOT_SENT
    assert summary["digest_url"] == ""


def test_a_hosted_tick_that_cannot_start_alerts_no_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI's own alert path, before any `Deps`, takes its senders from `bootstrap` too."""
    sent: list[str] = []

    async def discord(*_args) -> None:
        sent.append("discord")

    async def mail(*_args) -> None:
        sent.append("mail")

    async def stand_in(*_args) -> None:
        sent.append("stand-in")

    monkeypatch.setattr("cyris.adapters.notify.send_discord_alert", discord)
    monkeypatch.setattr("cyris.bootstrap.build_send_email_alert", lambda: mail)
    monkeypatch.setattr("cyris.adapters.hosted.skip_alert", stand_in)
    monkeypatch.setattr(
        "cyris.utils.timezone.now_in_timezone",
        lambda tz: datetime(2026, 11, 18, 8, 5).astimezone(),
    )
    _hosted_env(monkeypatch)
    gateway = FakeGateway("alpha")
    _through(gateway, monkeypatch)
    seed_d1_settings(
        gateway.db,
        omit=["digest.max_articles_per_digest"],
        **{**NOTIFY, "general.timezone": "UTC"},
    )
    config_path, sources_path = _files(tmp_path)

    result = CliRunner().invoke(
        app, ["run", "--if-due", "--config", str(config_path), "--sources", str(sources_path)]
    )

    assert result.exit_code == 1
    # The alert was due and worded for both channels, and only the stand-ins took it.
    assert sent == ["stand-in", "stand-in"]
