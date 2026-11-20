"""A hosted run's newsletter edge: its own Durable Object's queue, read and acked via its gateway.

The hosted Worker's email() handler stores each message sent to a tenant's random address in that
tenant's `newsletters` table (workers/app/src/hosted_inbound.js, ADR-0034). A run pulls it through
the gateway its container was given, matches senders as the KV source does, and acks by deleting
what it processed, in its own object only: a gateway answers one tenant and refuses any other with
403, so one tenant's run can neither read nor ack another's mail.
"""

import json
from datetime import UTC, datetime

import httpx
import pytest
from test_tenant_gateway import FakeGateway

from cyris.adapters.fetch.newsletter_worker_source import (
    TENANT_ACK_CHUNK,
    TenantNewsletterSource,
)
from cyris.adapters.store.tenant_gateway import GatewayD1Client, TenantDbError, apply_hosted_schema
from cyris.domain.models import Lane, SourceConfig

pytestmark = pytest.mark.integration

AFTER = datetime(2026, 11, 18, 0, tzinfo=UTC)
BEFORE = datetime(2026, 11, 18, 8, tzinfo=UTC)

SOURCES = {
    "Letter": SourceConfig(
        name="Letter",
        lane=Lane.READ,
        tags=["tech"],
        homepage="https://news.example",
        email_match="from:letter@news.example",
    )
}

TOKENED = "https://esp.example/view/eyJzdWJzY3JpYmVyIjoicmVhZGVyIn0"


def _issue(id_: str, sender: str = "letter@news.example", **extra: str) -> dict:
    """A row as the email() handler stores one (workers/app/src/tenant_inbound.js)."""
    return {
        "id": id_,
        "sender": sender,
        "subject": f"Issue {id_}",
        "html": "",
        "text": f"View in browser ({TOKENED})\n\nThis week's letter, {id_}.",
        "date": "2026-11-18T06:00:00.000Z",
        "headers": "[]",
        "raw_size": 400,
        "received_at": f"2026-11-18T06:00:0{len(id_) % 10}.000Z",
        **extra,
    }


def _queue(gateway: FakeGateway, rows: list[dict]) -> None:
    apply_hosted_schema(gateway.db)
    for row in rows:
        columns = list(row)
        marks = ", ".join("?" * len(columns))
        gateway.db.query(
            f"INSERT INTO newsletters ({', '.join(columns)}) VALUES ({marks})",
            [row[c] for c in columns],
        )


def _client(tenant: str, gateway: FakeGateway) -> GatewayD1Client:
    client = GatewayD1Client(tenant)
    client._http = httpx.Client(transport=httpx.MockTransport(gateway))
    return client


def _ids(gateway: FakeGateway) -> list[str]:
    return [r["id"] for r in gateway.db.query("SELECT id FROM newsletters ORDER BY id").rows]


async def test_a_run_pulls_its_own_queue_and_acks_what_it_processed() -> None:
    gateway = FakeGateway("alpha")
    _queue(gateway, [_issue("nl:1"), _issue("nl:2", sender="stranger@else.example")])

    articles = await TenantNewsletterSource(_client("alpha", gateway)).fetch_articles(
        AFTER, BEFORE, SOURCES
    )

    assert [a.title for a in articles] == ["Issue nl:1"]
    assert articles[0].source_lane == Lane.READ
    # The unmatched sender is acked too, so it cannot pile up, as the KV source acks one.
    assert _ids(gateway) == []
    assert {r.url.path for r in gateway.requests} == {"/t/alpha/query"}
    assert gateway.refused == []


async def test_a_preview_reads_the_queue_and_leaves_it() -> None:
    gateway = FakeGateway("alpha")
    _queue(gateway, [_issue("nl:1")])

    articles = await TenantNewsletterSource(_client("alpha", gateway), ack=False).fetch_articles(
        AFTER, BEFORE, SOURCES
    )

    assert len(articles) == 1
    assert _ids(gateway) == ["nl:1"]


async def test_an_ack_of_many_is_chunked_under_a_statements_bound_values() -> None:
    gateway = FakeGateway("alpha")
    _queue(gateway, [_issue(f"nl:{i:03}") for i in range(TENANT_ACK_CHUNK * 2 + 1)])

    await TenantNewsletterSource(_client("alpha", gateway)).fetch_articles(AFTER, BEFORE, SOURCES)

    assert _ids(gateway) == []
    deletes = [
        json.loads(r.content)["params"]
        for r in gateway.requests
        if json.loads(r.content)["sql"].startswith("DELETE")
    ]
    assert [len(params) for params in deletes] == [TENANT_ACK_CHUNK, TENANT_ACK_CHUNK, 1]


async def test_tenant_a_can_neither_pull_nor_ack_tenant_bs_mail() -> None:
    """Alpha's run holds a client for alpha; bravo's gateway refuses every request it makes."""
    bravo = FakeGateway("bravo")
    _queue(bravo, [_issue("nl:1")])
    alphas_client_at_bravos_door = _client("alpha", bravo)
    source = TenantNewsletterSource(alphas_client_at_bravos_door)

    with pytest.raises(TenantDbError):
        await source.fetch_articles(AFTER, BEFORE, SOURCES)
    source._ack(["nl:1"])

    assert _ids(bravo) == ["nl:1"]
    assert {r.url.path for r in bravo.requests} == {"/t/alpha/query"}
    assert not await source.health_check()


async def test_a_hosted_issue_never_takes_the_recipients_web_version_link() -> None:
    gateway = FakeGateway("alpha")
    _queue(gateway, [_issue("nl:1")])

    [article] = await TenantNewsletterSource(_client("alpha", gateway)).fetch_articles(
        AFTER, BEFORE, SOURCES
    )

    assert article.url.startswith("newsletter:")
    assert article.ref_urls == ["https://news.example"]
    assert TOKENED not in article.ref_urls


async def test_a_failed_pull_raises_into_failed_sources_and_acks_nothing() -> None:
    refused = httpx.Response(400, json={"errors": [{"message": "x"}]})
    gateway = FakeGateway("alpha", failures=[refused])
    _queue(gateway, [_issue("nl:1")])

    with pytest.raises(TenantDbError):
        await TenantNewsletterSource(_client("alpha", gateway)).fetch_articles(
            AFTER, BEFORE, SOURCES
        )

    assert _ids(gateway) == ["nl:1"]
