"""A hosted tenant's run: its own database through the gateway, and no channel of any other kind.

The hosted Worker starts a tenant's container with `CYRIS_STORE_TENANT_ID` and the hosted
domain. From those `bootstrap` gives the run the tenant's Durable Object through the gateway and
nothing that reaches outside it: no shared queue (the newsletter and vote Workers belong to
another deployment), no Pages project, and until operator alerts land no notification, whatever
the environment or the tenant's settings name. The issue stays in the tenant's `digests` table
(ADR-0028), and its pages go into the tenant's own `hosted_pages`, linked on the hosted domain
(ADR-0029).
"""

import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

import httpx
import pytest
import respx
from fakes import TEST_SETTINGS, FakeLLM, SqliteD1, load_files, seed_d1_settings
from test_run_digest import FakeSource
from test_tenant_gateway import FakeGateway
from typer.testing import CliRunner

from cyris import bootstrap
from cyris.adapters import hosted
from cyris.adapters.fetch.rss_source import RssSource
from cyris.adapters.hosted import NOT_SENT, skip_alert, skip_digest_notification
from cyris.adapters.output import publish
from cyris.adapters.output.hosted_pages import HostedPages
from cyris.adapters.store import tenant_gateway
from cyris.adapters.store.d1 import D1Client, apply_schema
from cyris.adapters.store.tenant_gateway import GatewayD1Client, apply_hosted_schema
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


# The hosted Worker's hostname, which the Worker hands every tenant's container.
DOMAIN = "cyris-hosted.trial.workers.dev"


def _hosted_env(monkeypatch: pytest.MonkeyPatch, tenant: str = "alpha") -> None:
    monkeypatch.setenv("CYRIS_STORE_BACKEND", "d1")
    monkeypatch.setenv("CYRIS_STORE_TENANT_ID", tenant)
    monkeypatch.setenv("CYRIS_STORE_HOSTED_DOMAIN", DOMAIN)


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
    monkeypatch.setenv("CYRIS_STORE_HOSTED_DOMAIN", DOMAIN)

    with pytest.raises(ValueError, match=refusal):
        load_files(*_files(tmp_path))


@pytest.mark.parametrize(
    ("tenant", "domain", "refusal"),
    [
        ("alpha", None, "needs CYRIS_STORE_HOSTED_DOMAIN"),
        (None, DOMAIN, "CYRIS_STORE_HOSTED_DOMAIN is set without a hosted tenant id"),
        ("alpha", f"https://{DOMAIN}", "must be a hostname"),
        ("alpha", f"{DOMAIN}/t/alpha", "must be a hostname"),
    ],
)
def test_a_tenant_needs_the_hosted_domain_and_the_domain_a_tenant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tenant: str | None,
    domain: str | None,
    refusal: str,
) -> None:
    """A hosted run's links have no host but the hosted Worker's to fall back to."""
    monkeypatch.setenv("CYRIS_STORE_BACKEND", "d1")
    for name, value in (("CYRIS_STORE_TENANT_ID", tenant), ("CYRIS_STORE_HOSTED_DOMAIN", domain)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=refusal):
        load_files(*_files(tmp_path))


@pytest.mark.parametrize("worker_set", ["bravo", None])
@pytest.mark.parametrize("written", ['"alpha"', '""'])
def test_a_tenant_id_written_in_cyris_toml_stops_the_load_whatever_the_worker_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worker_set: str | None, written: str
) -> None:
    """Only the hosted Worker names a container's tenant; a file value would win over it."""
    monkeypatch.setenv("CYRIS_STORE_BACKEND", "d1")
    if worker_set is None:
        monkeypatch.delenv("CYRIS_STORE_TENANT_ID", raising=False)
    else:
        monkeypatch.setenv("CYRIS_STORE_TENANT_ID", worker_set)
    config_path, sources_path = _files(tmp_path)
    with config_path.open("a", encoding="utf-8") as config:
        config.write(f'\n[store]\nbackend = "d1"\ntenant_id = {written}\n')

    with pytest.raises(ValueError, match=r"\[store\] tenant_id cannot be set in a config file"):
        load_files(config_path, sources_path)


def test_the_tenant_id_the_worker_set_is_the_one_the_store_carries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hosted_env(monkeypatch, "bravo")
    config_path, sources_path = _files(tmp_path)
    with config_path.open("a", encoding="utf-8") as config:
        config.write('\n[store]\nbackend = "d1"\n')

    assert load_files(config_path, sources_path).app.store.tenant_id == "bravo"


def test_the_schema_and_its_steps_land_through_the_gateway() -> None:
    gateway = FakeGateway("alpha")
    gateway.db = fresh = SqliteD1(with_schema=False)
    client = GatewayD1Client("alpha")
    client._http = httpx.Client(transport=httpx.MockTransport(gateway))

    apply_schema(client)

    tables = {r["name"] for r in fresh.query("SELECT name FROM sqlite_master").rows}
    assert {"settings", "sources", "digests", "digest_runs", "schema_migrations"} <= tables
    assert "hosted_pages" not in tables


def test_a_hosted_load_creates_the_tenants_pages_table_through_the_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tenant seeded before its pages had a table gets one on its first load, as D1 does."""
    _hosted_env(monkeypatch)
    gateway = FakeGateway("alpha")
    seed_d1_settings(gateway.db)
    _through(gateway, monkeypatch)

    bootstrap.load_effective_config(*_files(tmp_path))

    tables = {r["name"] for r in gateway.db.query("SELECT name FROM sqlite_master").rows}
    assert "hosted_pages" in tables
    assert gateway.refused == []


@pytest.fixture
def hosted_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for name, value in EVERY_CHANNEL.items():
        monkeypatch.setenv(name, value)
    _hosted_env(monkeypatch)
    gateway = FakeGateway("alpha")
    # What the operator's seed creates beside schema.sql (hosted_seed.js).
    apply_hosted_schema(gateway.db)
    _through(gateway, monkeypatch)
    cfg = load_files(*_files(tmp_path), {**TEST_SETTINGS, **NOTIFY})
    return cfg, gateway


def test_a_hosted_run_is_wired_to_its_own_site_and_to_no_queue_page_or_channel(hosted_cfg) -> None:
    cfg, _ = hosted_cfg

    deps = bootstrap.build_deps(cfg)

    assert [type(s) for s in deps.fetch_sources] == [RssSource]
    assert deps.sync_promotions is None
    assert bootstrap.build_promotion_sync(cfg) is None
    # Every environment switch for Pages is on, and still the publisher is the tenant's site.
    assert deps.html_writer is not None
    for wired in (deps.publish_site, deps.page_serves, deps.site_filenames):
        assert isinstance(wired.__self__, HostedPages)
    assert deps.worker_domains() == [f"{DOMAIN}/t/alpha"]
    assert deps.send_discord is skip_digest_notification
    assert deps.send_email is skip_digest_notification
    assert (deps.send_discord_alert, deps.send_email_alert) == (skip_alert, skip_alert)


def test_a_self_hosted_run_still_publishes_to_its_pages_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CYRIS_STORE_TENANT_ID", raising=False)
    monkeypatch.delenv("CYRIS_STORE_HOSTED_DOMAIN", raising=False)
    for name, value in EVERY_CHANNEL.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("CYRIS_STORE_BACKEND", "d1")
    cfg = load_files(*_files(tmp_path), TEST_SETTINGS)

    deps = bootstrap.build_deps(cfg)

    assert deps.publish_site.func is publish.publish_site
    assert deps.publish_site.keywords["pages_project"] == "someones-pages"
    assert deps.page_serves.func is publish.page_serves
    assert deps.page_serves.args == ("someones-pages",)


async def test_a_hosted_run_stores_its_pages_in_its_own_site_and_links_the_hosted_domain(
    hosted_cfg, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, gateway = hosted_cfg
    sent: list[dict] = []

    async def stand_in(*args, **kwargs) -> dict[str, str]:
        sent.append(kwargs)
        return dict(NOT_SENT)

    monkeypatch.setattr(hosted, "skip_digest_notification", stand_in)
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
        # Every request that leaves the process lands here, a Pages call included; the
        # gateway's never does.
        anywhere = respx.route().mock(return_value=httpx.Response(500))
        report = await run_digest(deps, RunOptions(period="morning"))

    assert report.status == "ok"
    assert anywhere.call_count == 0
    # The stores are fail-soft, so a write the tenant's database refused would not fail the run.
    assert gateway.refused == []
    [issue] = gateway.db.query("SELECT date, period, content FROM digests").rows
    assert issue["period"] == "morning"
    assert article.url in issue["content"]
    slug = f"{issue['date']}-morning"

    pages = {
        r["path"]: r["body"] for r in gateway.db.query("SELECT path, body FROM hosted_pages").rows
    }
    assert {f"/{slug}.html", f"/{slug}-raw.html", "/index.html", "/favicon.svg"} <= set(pages)
    assert f'<meta name="cyris-issue" content="{slug}">' in pages[f"/{slug}.html"]
    assert article.url in pages[f"/{slug}-raw.html"]

    digest_url = f"https://{DOMAIN}/t/alpha/{slug}"
    [run] = gateway.db.query("SELECT status, summary FROM digest_runs").rows
    summary = json.loads(run["summary"])
    assert run["status"] == "ok"
    assert summary["discord_notification"] == NOT_SENT
    assert summary["mail_notification"] == NOT_SENT
    assert summary["digest_url"] == digest_url
    # Both notifications were handed the hosted link, though neither is sent yet.
    assert [call["digest_url"] for call in sent] == [digest_url, digest_url]
    assert [call["publish_failed"] for call in sent] == [False, False]

    # Every link a page makes to its own site lands on the hosted domain; the issue's own
    # links (the digest, the raw page, the archive, the icon) stay inside the tenant's site.
    for body in pages.values():
        assert "pages.dev" not in body
    hrefs = re.findall(r"""(?:href|src)=["']([^"']+)["']""", pages[f"/{slug}.html"])
    own = [h for h in hrefs if not re.match(r"[a-z]+:", h) and not h.startswith("#")]
    assert own
    for href in own:
        assert urljoin(digest_url, href).startswith(f"https://{DOMAIN}/")
    for name in (f"{slug}-raw.html", "index.html", "favicon.svg"):
        assert name in own
        assert urljoin(digest_url, name) == f"https://{DOMAIN}/t/alpha/{name}"


def test_a_page_over_a_statements_100_kb_round_trips_through_the_gateway() -> None:
    gateway = FakeGateway("alpha")
    apply_hosted_schema(gateway.db)
    client = GatewayD1Client("alpha")
    client._http = httpx.Client(transport=httpx.MockTransport(gateway))
    site = HostedPages(client)
    raw = ("<html>" + "原文 " * 40_000 + "</html>").encode("utf-8")
    assert len(raw) > 150_000

    assert site.publish({"/2026-11-18-morning-raw.html": raw}, "2026-11-18-morning")

    assert site.serves("/2026-11-18-morning-raw.html", raw)
    assert gateway.refused == []
    # The same body written into the statement is refused, as the object refuses it.
    with pytest.raises(tenant_gateway.TenantDbError, match="statement too long"):
        client.query(f"SELECT '{raw.decode('utf-8')}'")


async def test_a_hosted_run_republishes_an_issue_its_site_lacks(hosted_cfg) -> None:
    """An issue stored before its tenant had a site is rendered from `digests` into it."""
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
    await run_digest(deps, RunOptions(period="morning"))
    [issue] = gateway.db.query("SELECT date FROM digests").rows
    slug = f"{issue['date']}-morning"
    gateway.db.query("DELETE FROM hosted_pages")

    await run_digest(replace(deps, fetch_sources=[FakeSource([])]), RunOptions(period="evening"))

    paths = {r["path"] for r in gateway.db.query("SELECT path FROM hosted_pages").rows}
    assert f"/{slug}.html" in paths
    assert gateway.refused == []


async def test_every_write_of_a_scored_run_is_one_a_durable_object_can_bind(hosted_cfg) -> None:
    """Scores, tags and topics too: each value the stores bind is a string, a number or null."""
    cfg, gateway = hosted_cfg
    article = Article(
        id=1,
        title="A model release",
        url="https://a.test/model",
        content="A lab released a model.",
        published_at=datetime.now(UTC) - timedelta(hours=1),
        source_name="A",
        source_lane=Lane.READ,
        source_tags=["ai"],
    )
    scores = {"scores": [{"id": 0, "score": 80, "language": "en", "tags": ["ai", "models"]}]}
    deps = replace(
        bootstrap.build_deps(cfg),
        llm=FakeLLM([json.dumps(scores)]),
        fetch_sources=[FakeSource([article])],
    )

    report = await run_digest(deps, RunOptions(period="evening"))

    assert report.status == "ok"
    assert gateway.refused == []
    [row] = gateway.db.query("SELECT score FROM stored_articles").rows
    assert row["score"] == 80


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
