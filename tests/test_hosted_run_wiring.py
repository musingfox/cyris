"""A hosted tenant's run: its own database through the gateway, and no channel of any other kind.

The hosted Worker starts a tenant's container with `CYRIS_STORE_TENANT_ID` and the hosted
domain. From those `bootstrap` gives the run the tenant's Durable Object through the gateway and
nothing that reaches outside it: no shared queue (the newsletter and vote Workers belong to
another deployment), no Pages project, and no digest notification, whatever the environment or
the tenant's settings name. The issue stays in the tenant's `digests` table (ADR-0028), and its
pages go into the tenant's own `hosted_pages`, linked on the hosted domain (ADR-0029). A failed
run tells the operator, by tenant, with its error, and the reader without it (ADR-0035); every
line the run writes names its tenant.
"""

import json
import os
import re
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from urllib.parse import urljoin

import httpx
import pytest
import respx
import typer
from fakes import TEST_SETTINGS, FakeLLM, SqliteD1, load_files, seed_d1_settings
from test_run_digest import FakeSource
from test_tenant_gateway import FakeGateway
from typer.testing import CliRunner

from cyris import bootstrap
from cyris.adapters import hosted
from cyris.adapters.fetch.newsletter_worker_source import TenantNewsletterSource
from cyris.adapters.fetch.onboarding_cards import OnboardingArticles
from cyris.adapters.fetch.rss_source import RssSource
from cyris.adapters.fetch.rss_worker_source import TenantRssSource
from cyris.adapters.hosted import (
    NOT_SENT,
    OPERATOR_ORIGIN,
    TaggedLines,
    skip_digest_notification,
)
from cyris.adapters.output import publish
from cyris.adapters.output.hosted_pages import HostedPages
from cyris.adapters.run_alert import reader_alert_message
from cyris.adapters.store import tenant_gateway
from cyris.adapters.store.d1 import D1Client, apply_schema
from cyris.adapters.store.tenant_gateway import GatewayD1Client, apply_hosted_schema
from cyris.domain.models import Article, Lane
from cyris.entrypoints.cli import app
from cyris.service_layer import run_digest as run_digest_module
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


@pytest.mark.parametrize("from_file", [False, True])
def test_a_custom_domain_beside_a_tenant_id_stops_the_load_from_the_environment_or_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, from_file: bool
) -> None:
    """`run_digest` links a custom domain ahead of the hosted site, so a tenant may carry none."""
    _hosted_env(monkeypatch)
    monkeypatch.delenv("CYRIS_PROMOTE_CUSTOM_DOMAIN", raising=False)
    config_path, sources_path = _files(tmp_path)
    if from_file:
        with config_path.open("a", encoding="utf-8") as config:
            config.write('\n[promote]\ncustom_domain = "digest.example.com"\n')
    else:
        monkeypatch.setenv("CYRIS_PROMOTE_CUSTOM_DOMAIN", "digest.example.com")

    with pytest.raises(ValueError, match=r"\[promote\] custom_domain .* hosted tenant id"):
        load_files(config_path, sources_path)


def test_a_custom_domain_without_a_tenant_id_still_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CYRIS_STORE_TENANT_ID", raising=False)
    monkeypatch.setenv("CYRIS_PROMOTE_CUSTOM_DOMAIN", "digest.example.com")

    cfg = load_files(*_files(tmp_path))

    assert cfg.app.promote.custom_domain == "digest.example.com"


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
    # The tenant's AI allowance, which every hosted run asks and charges (ADR-0038).
    from test_hosted_spend import FakeSpendGateway, ledger_through

    ledger_through(FakeSpendGateway("alpha"), monkeypatch)
    cfg = load_files(*_files(tmp_path), {**TEST_SETTINGS, **NOTIFY})
    return cfg, gateway


def test_a_hosted_run_is_wired_to_its_own_site_and_to_no_queue_page_or_channel(hosted_cfg) -> None:
    cfg, _ = hosted_cfg

    deps = bootstrap.build_deps(cfg)

    # The rss and newsletter Workers' URLs and token are set, and still the edges are the
    # tenant's own buffer and its own newsletter queue.
    assert [type(s) for s in deps.fetch_sources] == [TenantNewsletterSource, TenantRssSource]
    assert deps.sync_promotions is None
    assert bootstrap.build_promotion_sync(cfg) is None
    # Every environment switch for Pages is on, and still the publisher is the tenant's site.
    assert deps.html_writer is not None
    for wired in (deps.publish_site, deps.page_serves, deps.site_filenames):
        assert isinstance(wired.__self__, HostedPages)
    assert deps.worker_domains() == [f"{DOMAIN}/t/alpha"]
    assert deps.send_discord is skip_digest_notification
    assert deps.send_email is skip_digest_notification
    # The reader's alert channels are the tenant's own, worded with the error withheld.
    assert deps.send_discord_alert.keywords == {"wording": reader_alert_message}
    assert deps.send_email_alert.keywords["wording"] is reader_alert_message
    # The operator's is bound to this run's tenant.
    assert deps.send_operator_alert.args == ("alpha",)


def test_a_self_hosted_run_tells_no_operator(tmp_path: Path) -> None:
    cfg = load_files(*_files(tmp_path), {**TEST_SETTINGS, **NOTIFY})

    assert bootstrap.build_operator_alert(cfg) is None
    assert bootstrap.build_alert_senders(cfg)[0] is not None
    assert not hasattr(bootstrap.build_alert_senders(cfg)[0], "keywords")


# The Email Sending API, which the reader's alert mail goes to.
MAIL_API = r"https://api\.cloudflare\.com/.*/email/sending/send"
MAILED = httpx.Response(200, json={"success": True, "result": {"delivered": ["r"]}})


# An error whose text a reader must never be shown: a provider's message, a key fragment, an
# account id.
RAW_ERROR = "Anthropic answered 401 for key sk-ant-abc123 on account 0f1e2d"


async def test_a_hosted_run_that_fails_tells_the_operator_its_tenant_and_the_reader_no_error(
    hosted_cfg, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, gateway = hosted_cfg

    async def fails(*_args) -> None:
        raise RuntimeError(RAW_ERROR)

    monkeypatch.setattr(run_digest_module, "_run_digest", fails)
    deps = bootstrap.build_deps(cfg)

    with respx.mock:
        operator = respx.post(f"{OPERATOR_ORIGIN}/t/alpha/alert").mock(
            return_value=httpx.Response(202)
        )
        discord = respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
        mail = respx.post(url__regex=MAIL_API).mock(return_value=MAILED)
        with pytest.raises(RuntimeError, match="sk-ant"):
            await run_digest(deps, RunOptions(period="morning"))

    assert (operator.call_count, discord.call_count, mail.call_count) == (1, 1, 1)
    # The operator: one alert, on the tenant's own path, with the raw error.
    [told] = operator.calls
    sent = json.loads(told.request.content)
    assert sent["kind"] == "run_failed"
    assert sent["subject"].startswith("Digest run failed: ")
    assert sent["body"] == f"RuntimeError: {RAW_ERROR}"
    # The reader: the same alert on each of the tenant's channels, and none of the error.
    [posted] = discord.calls
    [mailed] = mail.calls
    for request in (posted.request, mailed.request):
        text = request.content.decode()
        assert "Digest run failed" in text
        assert "The error has been sent to the service" in text
        for fragment in ("RuntimeError", "401", "sk-ant", "0f1e2d"):
            assert fragment not in text
    [run] = gateway.db.query("SELECT status FROM digest_runs").rows
    assert run["status"] == "error"


async def test_a_hosted_run_that_fetched_nothing_tells_the_operator_too(
    hosted_cfg, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _ = hosted_cfg

    class Dead:
        async def fetch_articles(self, **_kwargs):
            raise RuntimeError("feed a.test is down")

        async def health_check(self) -> bool:
            return False

    deps = replace(bootstrap.build_deps(cfg), fetch_sources=[Dead()])

    with respx.mock:
        operator = respx.post(f"{OPERATOR_ORIGIN}/t/alpha/alert").mock(
            return_value=httpx.Response(202)
        )
        discord = respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
        mail = respx.post(url__regex=MAIL_API).mock(return_value=MAILED)
        await run_digest(deps, RunOptions(period="morning"))

    assert (operator.call_count, discord.call_count, mail.call_count) == (1, 1, 1)
    [told] = operator.calls
    assert json.loads(told.request.content)["subject"].startswith("Digest run fetched nothing")


async def test_an_operator_alert_that_cannot_be_sent_leaves_the_runs_own_error(
    hosted_cfg, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _ = hosted_cfg

    async def fails(*_args) -> None:
        raise RuntimeError(RAW_ERROR)

    monkeypatch.setattr(run_digest_module, "_run_digest", fails)
    deps = bootstrap.build_deps(cfg)

    with respx.mock:
        operator = respx.post(f"{OPERATOR_ORIGIN}/t/alpha/alert").mock(
            return_value=httpx.Response(503)
        )
        discord = respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
        mail = respx.post(url__regex=MAIL_API).mock(return_value=MAILED)
        with pytest.raises(RuntimeError, match="sk-ant"):
            await run_digest(deps, RunOptions(period="morning"))

    # The refused operator alert is logged; the reader's channels still had their attempt.
    assert (operator.call_count, discord.call_count, mail.call_count) == (1, 1, 1)


async def test_a_hosted_runs_newsletters_are_its_own_queue_and_a_preview_leaves_them(
    hosted_cfg,
) -> None:
    cfg, gateway = hosted_cfg
    # No source matches it and it arrived long past the hold, so a run acks it.
    gateway.db.query(
        "INSERT INTO newsletters (id, sender, subject, html, text, date, headers, received_at) "
        "VALUES ('nl:1', 'x@a.test', 'S', '', 'body', '2026-01-01T06:00:00.000Z', '[]', "
        "'2026-01-01T06:00:00.000Z')"
    )
    gateway.requests.clear()

    [preview, _] = bootstrap.build_deps(cfg, dry_run=True).fetch_sources
    [source, _] = bootstrap.build_deps(cfg).fetch_sources
    await preview.fetch_articles(datetime.now(UTC), datetime.now(UTC), cfg.sources)
    left = gateway.db.query("SELECT count(*) AS n FROM newsletters").rows[0]["n"]
    await source.fetch_articles(datetime.now(UTC), datetime.now(UTC), cfg.sources)

    assert left == 1
    assert gateway.db.query("SELECT count(*) AS n FROM newsletters").rows[0]["n"] == 0
    assert {r.url.path for r in gateway.requests} == {"/t/alpha/query"}


async def test_a_hosted_runs_rss_is_its_own_buffer_read_through_its_gateway(hosted_cfg) -> None:
    cfg, gateway = hosted_cfg
    apply_schema(gateway.db)
    # What the hosted Worker's hourly poll wrote: `fetched_at` is the poll's `toISOString()`.
    for url, fetched_at in (
        ("https://a.test/in", "2026-11-18T07:00:00.000Z"),
        ("https://a.test/out", "2026-11-17T07:00:00.000Z"),
    ):
        gateway.db.query(
            "INSERT INTO articles (url, title, content, published_at, source_name, fetched_at) "
            "VALUES (?, 't', 'body', '2026-11-18T06:00:00.000Z', 'A', ?)",
            [url, fetched_at],
        )
    gateway.requests.clear()
    [_, source] = bootstrap.build_deps(cfg).fetch_sources

    articles = await source.fetch_articles(
        datetime(2026, 11, 18, tzinfo=UTC), datetime(2026, 11, 18, 8, tzinfo=UTC), cfg.sources
    )

    assert [a.url for a in articles] == ["https://a.test/in"]
    assert articles[0].source_lane == cfg.sources["A"].lane
    assert {r.url.path for r in gateway.requests} == {"/t/alpha/query"}


def test_a_hosted_tenants_onboarding_cards_still_poll_their_feeds_directly(hosted_cfg) -> None:
    """Onboarding is out of the buffer's card: its cards are built as they were before it."""
    cfg, _ = hosted_cfg

    cards = bootstrap.build_onboarding_articles(cfg)

    assert isinstance(cards, OnboardingArticles)
    assert isinstance(cards._source, RssSource)
    assert cards._poll is None


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


def test_a_hosted_tick_that_cannot_start_tells_the_operator_and_the_reader_no_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI's own alert path, before any `Deps`, takes its senders from `bootstrap` too."""
    sent: list[tuple[str, str, str]] = []

    async def discord(_url, alert, *, wording) -> None:
        sent.append(("discord", *wording(alert)))

    async def mail(_to, _sender, alert, *, wording) -> None:
        sent.append(("mail", *wording(alert)))

    async def operator(tenant, alert) -> None:
        sent.append(("operator", tenant, alert.error))

    monkeypatch.setattr("cyris.adapters.notify.send_discord_alert", discord)
    monkeypatch.setattr("cyris.bootstrap.build_send_email_alert", lambda **kw: partial(mail, **kw))
    monkeypatch.setattr("cyris.adapters.hosted.send_operator_alert", operator)
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
    # The alert was due: the reader's two channels were told it could not start, without the
    # error, and the operator was told which tenant, with it.
    [(_, d_subject, d_body), (_, m_subject, m_body), (who, tenant, error)] = sent
    assert [entry[0] for entry in sent] == ["discord", "mail", "operator"]
    assert d_subject.startswith("Digest run could not start")
    assert d_body == m_body == "The error has been sent to the service's operator."
    assert "max_articles_per_digest" not in d_subject + d_body + m_subject + m_body
    assert tenant == "alpha"
    assert "max_articles_per_digest" in error


def _operator_told(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, object]]:
    """Every alert the hosted operator is sent, as (tenant, alert)."""
    told: list[tuple[str, object]] = []

    async def operator(tenant, alert) -> None:
        told.append((tenant, alert))

    monkeypatch.setattr("cyris.adapters.hosted.send_operator_alert", operator)
    return told


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(["--period", "morning"], id="catch-up"),
        pytest.param(["--period", "morning", "--date", "2026-11-18"], id="manual"),
    ],
)
def test_a_hosted_run_started_by_period_that_cannot_load_its_settings_tells_the_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    """A catch-up and a manual run start without --if-due; the operator still hears."""
    told = _operator_told(monkeypatch)
    _hosted_env(monkeypatch)
    gateway = FakeGateway("alpha")
    _through(gateway, monkeypatch)
    seed_d1_settings(
        gateway.db, omit=["digest.max_articles_per_digest"], **{"general.timezone": "UTC"}
    )
    config_path, sources_path = _files(tmp_path)

    result = CliRunner().invoke(
        app, ["run", *args, "--config", str(config_path), "--sources", str(sources_path)]
    )

    assert result.exit_code == 1
    [(tenant, alert)] = told
    assert tenant == "alpha"
    assert alert.kind == "could_not_start"
    assert alert.period == "morning"
    assert "max_articles_per_digest" in alert.error


def test_a_hosted_run_whose_database_gateway_fails_tells_the_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No setting can be read, so the alert names the tenant from the environment, in UTC."""
    told = _operator_told(monkeypatch)
    _hosted_env(monkeypatch)
    real = GatewayD1Client

    def client(tenant_id: str) -> GatewayD1Client:
        made = real(tenant_id)
        made._http = httpx.Client(
            transport=httpx.MockTransport(lambda _r: httpx.Response(500, json={"errors": []}))
        )
        return made

    monkeypatch.setattr(tenant_gateway, "GatewayD1Client", client)
    monkeypatch.setattr("cyris.adapters.store.d1.RETRY_BACKOFF_SECONDS", 0)
    config_path, sources_path = _files(tmp_path)

    result = CliRunner().invoke(
        app, ["run", "--if-due", "--config", str(config_path), "--sources", str(sources_path)]
    )

    assert result.exit_code == 1
    [(tenant, alert)] = told
    assert tenant == "alpha"
    assert alert.kind == "could_not_start"
    assert alert.timezone == "UTC"
    assert "500" in alert.error


def test_a_hosted_run_whose_wiring_fails_tells_the_operator(
    hosted_cfg, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`build_deps` raising in a manual run: the reader's rule skips it, the operator's not."""
    from cyris.entrypoints import cli

    cfg, _ = hosted_cfg
    told = _operator_told(monkeypatch)

    def broken(*_args, **_kwargs):
        raise RuntimeError("wiring broke")

    monkeypatch.setattr("cyris.bootstrap.build_deps", broken)

    with pytest.raises(RuntimeError, match="wiring broke"):
        cli._run_pipeline(cfg, "evening", False, False, None, None, False)

    [(tenant, alert)] = told
    assert tenant == "alpha"
    assert alert.period == "evening"
    assert alert.error == "RuntimeError: wiring broke"


def test_a_hosted_dry_run_that_cannot_start_tells_nobody(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    told = _operator_told(monkeypatch)
    _hosted_env(monkeypatch)
    gateway = FakeGateway("alpha")
    _through(gateway, monkeypatch)
    seed_d1_settings(gateway.db, omit=["digest.max_articles_per_digest"])
    config_path, sources_path = _files(tmp_path)

    result = CliRunner().invoke(
        app,
        ["run", "--dry-run", "--period", "morning", "--config", str(config_path)]
        + ["--sources", str(sources_path)],
    )

    assert result.exit_code == 1
    assert told == []


def test_every_line_a_hosted_run_writes_names_its_tenant(tmp_path: Path) -> None:
    """The container's stdout and stderr are the run's only log, shared by every tenant."""
    env = {
        **os.environ,
        "CYRIS_STORE_TENANT_ID": "alpha",
        "CYRIS_STORE_HOSTED_DOMAIN": DOMAIN,
        # Refused at the load, so the run logs and echoes and stops; a dry run tells no
        # operator, so it sends no request either.
        "CYRIS_STORE_BACKEND": "sqlite",
    }
    missing = tmp_path / "missing"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from cyris.entrypoints.cli import app; app()",
            "run",
            "--dry-run",
            "--config",
            str(missing / "cyris.toml"),
            "--sources",
            str(missing / "sources.yaml"),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 1
    lines = [line for line in (result.stdout + result.stderr).splitlines() if line]
    assert any("Configuration error" in line for line in lines)
    assert lines
    for line in lines:
        assert line.startswith("tenant=alpha "), line


def test_a_self_hosted_runs_lines_carry_no_tenant(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CYRIS_STORE_")}
    missing = tmp_path / "missing"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from cyris.entrypoints.cli import app; app()",
            "run",
            "--config",
            str(missing / "cyris.toml"),
            "--sources",
            str(missing / "sources.yaml"),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert "tenant=" not in result.stdout + result.stderr


def test_a_tagged_stream_tags_each_line_once_however_it_is_written() -> None:
    import io

    out = io.StringIO()
    tagged = TaggedLines(out, "tenant=alpha ")

    tagged.write("one")
    tagged.write(" more\ntwo\n")
    tagged.write("\n")
    tagged.write("three\nfour")

    assert out.getvalue() == (
        "tenant=alpha one more\ntenant=alpha two\n\ntenant=alpha three\ntenant=alpha four"
    )


def test_a_tagged_streams_bytes_are_tagged_too() -> None:
    import io

    raw = io.BytesIO()
    text = io.TextIOWrapper(raw, encoding="utf-8", write_through=True)
    tagged = TaggedLines(text, "tenant=alpha ")

    tagged.write("text, then ")
    tagged.buffer.write(b"bytes\nmore bytes\n")
    typer.echo(b"echoed bytes", file=tagged)

    assert raw.getvalue() == (
        b"tenant=alpha text, then bytes\ntenant=alpha more bytes\ntenant=alpha echoed bytes\n"
    )


def test_a_json_commands_stdout_stays_untagged_and_its_stderr_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--json` stdout is a series a reader parses; the lines that explain it go to stderr."""
    import io

    from cyris.entrypoints import cli

    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setenv("CYRIS_STORE_TENANT_ID", "alpha")
    for argv, stdout_tagged in (
        (["cyris", "embed-compare", "--json"], False),
        (["cyris", "run"], True),
    ):
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", err)
        monkeypatch.setattr(sys, "argv", argv)

        cli.main()

        assert isinstance(sys.stdout, TaggedLines) is stdout_tagged
        assert isinstance(sys.stderr, TaggedLines)
    print('{"row": 1}', file=out)
    assert json.loads(out.getvalue()) == {"row": 1}
