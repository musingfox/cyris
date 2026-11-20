"""A hosted run's newsletter edge: its own Durable Object's queue, read and acked via its gateway.

The hosted Worker's email() handler stores each message sent to a tenant's random address in that
tenant's `newsletters` table (workers/app/src/hosted_inbound.js, ADR-0034). A run pulls it through
the gateway its container was given, matches senders as the KV source does, and acks by deleting
what it processed, in its own object only: a gateway answers one tenant and refuses any other with
403, so one tenant's run can neither read nor ack another's mail. A message no source matches
waits `UNMATCHED_HOLD_DAYS` for its source. The sources are the rows the operator's
`newsletter-source` route writes (workers/app/test/newsletter_source_cases.json).
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from test_tenant_gateway import FakeGateway

from cyris.adapters.fetch.newsletter_worker_source import (
    TENANT_ACK_CHUNK,
    UNMATCHED_HOLD_DAYS,
    TenantNewsletterSource,
)
from cyris.adapters.store.d1 import apply_schema
from cyris.adapters.store.source_store import D1SourceStore
from cyris.adapters.store.tenant_gateway import GatewayD1Client, TenantDbError, apply_hosted_schema
from cyris.domain.models import Lane, SourceConfig

pytestmark = pytest.mark.integration

AFTER = datetime(2026, 11, 18, 0, tzinfo=UTC)
BEFORE = datetime(2026, 11, 18, 8, tzinfo=UTC)
# The run's clock: two hours after the issues below arrived.
NOW = datetime(2026, 11, 18, 8, tzinfo=UTC)

CASES = json.loads(
    (Path(__file__).parents[1] / "workers/app/test/newsletter_source_cases.json").read_text()
)

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


def _source(client: GatewayD1Client, **kwargs) -> TenantNewsletterSource:
    return TenantNewsletterSource(client, now=lambda: NOW, **kwargs)


def _ids(gateway: FakeGateway) -> list[str]:
    return [r["id"] for r in gateway.db.query("SELECT id FROM newsletters ORDER BY id").rows]


async def test_a_run_pulls_its_own_queue_and_acks_what_it_processed() -> None:
    gateway = FakeGateway("alpha")
    _queue(gateway, [_issue("nl:1"), _issue("nl:2", sender="stranger@else.example")])

    articles = await _source(_client("alpha", gateway)).fetch_articles(AFTER, BEFORE, SOURCES)

    assert [a.title for a in articles] == ["Issue nl:1"]
    assert articles[0].source_lane == Lane.READ
    # The unmatched sender arrived two hours ago, so it waits for a source; the match is acked.
    assert _ids(gateway) == ["nl:2"]
    assert {r.url.path for r in gateway.requests} == {"/t/alpha/query"}
    assert gateway.refused == []


async def test_a_preview_reads_the_queue_and_leaves_it() -> None:
    gateway = FakeGateway("alpha")
    _queue(gateway, [_issue("nl:1")])

    articles = await _source(_client("alpha", gateway), ack=False).fetch_articles(
        AFTER, BEFORE, SOURCES
    )

    assert len(articles) == 1
    assert _ids(gateway) == ["nl:1"]


async def test_an_ack_of_many_is_chunked_under_a_statements_bound_values() -> None:
    gateway = FakeGateway("alpha")
    _queue(gateway, [_issue(f"nl:{i:03}") for i in range(TENANT_ACK_CHUNK * 2 + 1)])

    await _source(_client("alpha", gateway)).fetch_articles(AFTER, BEFORE, SOURCES)

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
    source = _source(alphas_client_at_bravos_door)

    with pytest.raises(TenantDbError):
        await source.fetch_articles(AFTER, BEFORE, SOURCES)
    source._ack(["nl:1"])

    assert _ids(bravo) == ["nl:1"]
    assert {r.url.path for r in bravo.requests} == {"/t/alpha/query"}
    assert not await source.health_check()


async def test_a_hosted_issue_never_takes_a_link_that_may_name_its_recipient() -> None:
    gateway = FakeGateway("alpha")
    _queue(gateway, [_issue("nl:1")])

    [article] = await _source(_client("alpha", gateway)).fetch_articles(AFTER, BEFORE, SOURCES)

    assert article.url.startswith("newsletter:")
    assert article.ref_urls == ["https://news.example"]
    assert TOKENED not in article.ref_urls


async def test_a_failed_pull_raises_into_failed_sources_and_acks_nothing() -> None:
    refused = httpx.Response(400, json={"errors": [{"message": "x"}]})
    gateway = FakeGateway("alpha", failures=[refused])
    _queue(gateway, [_issue("nl:1")])

    with pytest.raises(TenantDbError):
        await _source(_client("alpha", gateway)).fetch_articles(AFTER, BEFORE, SOURCES)

    assert _ids(gateway) == ["nl:1"]


async def test_unmatched_mail_waits_for_its_source_and_is_ingested_once_it_is_added() -> None:
    gateway = FakeGateway("alpha")
    _queue(gateway, [_issue("nl:1")])
    client = _client("alpha", gateway)

    before_the_source = await _source(client).fetch_articles(AFTER, BEFORE, {})
    after_the_source = await _source(client).fetch_articles(AFTER, BEFORE, SOURCES)

    assert before_the_source == []
    assert [a.title for a in after_the_source] == ["Issue nl:1"]
    assert _ids(gateway) == []


async def test_unmatched_mail_past_its_hold_is_acked() -> None:
    gateway = FakeGateway("alpha")
    arrived = NOW - timedelta(days=UNMATCHED_HOLD_DAYS)
    _queue(
        gateway,
        [
            _issue("nl:old", sender="x@else.example", received_at=arrived.isoformat()),
            _issue(
                "nl:new",
                sender="x@else.example",
                received_at=(arrived + timedelta(minutes=1)).isoformat(),
            ),
            _issue("nl:bad", sender="x@else.example", received_at="not a time"),
        ],
    )

    await _source(_client("alpha", gateway)).fetch_articles(AFTER, BEFORE, SOURCES)

    assert _ids(gateway) == ["nl:new"]


def _seeded_with_case_source(tenant: str) -> tuple[FakeGateway, GatewayD1Client]:
    """A seeded tenant to which the operator added the first case's newsletter source."""
    gateway = FakeGateway(tenant)
    apply_schema(gateway.db)
    row = CASES["written"][0]["row"]
    gateway.db.query(
        "INSERT INTO sources (name, url, type, config) VALUES (?, ?, ?, ?)",
        [row["name"], row["url"], row["type"], json.dumps(row["config"])],
    )
    return gateway, _client(tenant, gateway)


@pytest.mark.parametrize("case", CASES["written"], ids=lambda case: case["row"]["name"])
def test_each_row_the_route_writes_is_a_fetchable_newsletter_source(case: dict) -> None:
    gateway = FakeGateway("alpha")
    apply_schema(gateway.db)
    row = case["row"]
    gateway.db.query(
        "INSERT INTO sources (name, url, type, config) VALUES (?, ?, ?, ?)",
        [row["name"], row["url"], row["type"], json.dumps(row["config"])],
    )

    [source] = D1SourceStore(_client("alpha", gateway)).list_sources().values()

    assert source.fetchable
    assert source.type == "newsletter"
    assert source.lane == row["config"]["lane"]
    assert source.email_match == row["config"]["email_match"]


async def test_one_issue_two_tenants_subscribed_to_reaches_both_runs_articles() -> None:
    """Each tenant holds the source the operator's route wrote, and each run ingests the issue."""
    sender = CASES["written"][0]["row"]["config"]["email_match"].removeprefix("from:")
    got = {}
    for tenant in ("alpha", "bravo"):
        gateway, client = _seeded_with_case_source(tenant)
        _queue(gateway, [_issue("nl:same", sender=sender)])
        sources = D1SourceStore(client).list_sources()

        got[tenant] = await _source(client).fetch_articles(AFTER, BEFORE, sources)

        assert _ids(gateway) == []
        assert {r.url.path for r in gateway.requests} == {f"/t/{tenant}/query"}

    assert [a.title for a in got["alpha"]] == ["Issue nl:same"]
    assert [(a.title, a.url, a.source_name) for a in got["bravo"]] == [
        (a.title, a.url, a.source_name) for a in got["alpha"]
    ]
