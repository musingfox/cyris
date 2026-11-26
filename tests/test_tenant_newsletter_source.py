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
    MAX_NEW_SOURCES_PER_RUN,
    TENANT_ACK_CHUNK,
    UNMATCHED_HOLD_DAYS,
    TenantNewsletterSource,
    own_address_keys,
)
from cyris.adapters.hosted import hosted_policy
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

# The tenant's reader: mail from this address is a forward.
READER = own_address_keys("reader@gmail.example")


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
    _queue(gateway, [_issue("nl:1"), _issue("nl:2", sender="forwarding-noreply@google.com")])

    articles = await _source(_client("alpha", gateway)).fetch_articles(AFTER, BEFORE, SOURCES)

    assert [a.title for a in articles] == ["Issue nl:1"]
    assert articles[0].source_lane == Lane.READ
    # The system sender's mail arrived two hours ago, so it waits; the match is acked.
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


async def test_a_readers_forward_whose_sender_cannot_be_read_is_held_until_its_hold_ends() -> None:
    gateway = FakeGateway("alpha")
    # A forward from a client whose block we cannot read, which names its sender in the body.
    unreadable = "De : The Letter <letter@news.example>\n\nThis week's letter."
    _queue(
        gateway,
        [_issue("nl:1", sender="reader@gmail.example", subject="Fwd: Issue nl:1", text=unreadable)],
    )
    client = _client("alpha", gateway)
    later = NOW + timedelta(days=UNMATCHED_HOLD_DAYS)

    held = await _source(client, own_addresses=READER).fetch_articles(AFTER, BEFORE, SOURCES)
    assert (held, _held(gateway)) == ([], {"nl:1": NOW.isoformat()})
    gone = TenantNewsletterSource(client, now=lambda: later, own_addresses=READER)
    assert await gone.fetch_articles(AFTER, BEFORE, SOURCES) == []

    assert _ids(gateway) == []


async def test_unmatched_mail_past_its_hold_is_acked() -> None:
    gateway = FakeGateway("alpha")
    arrived = NOW - timedelta(days=UNMATCHED_HOLD_DAYS)
    _queue(
        gateway,
        [
            _issue(
                "nl:old", sender="forwarding-noreply@google.com", received_at=arrived.isoformat()
            ),
            _issue(
                "nl:new",
                sender="forwarding-noreply@google.com",
                received_at=(arrived + timedelta(minutes=1)).isoformat(),
            ),
            _issue("nl:bad", sender="forwarding-noreply@google.com", received_at="not a time"),
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


async def test_a_sender_written_in_mixed_case_matches_the_routes_lower_case_source() -> None:
    """record.js stores From as written; the operator's route writes `email_match` lowercased."""
    gateway, client = _seeded_with_case_source("alpha")
    _queue(gateway, [_issue("nl:1", sender="Letter@News.example")])

    articles = await _source(client).fetch_articles(
        AFTER, BEFORE, D1SourceStore(client).list_sources()
    )

    assert [a.title for a in articles] == ["Issue nl:1"]
    assert _ids(gateway) == []


async def test_a_manual_forward_matches_its_original_sender_in_any_case() -> None:
    gateway, client = _seeded_with_case_source("alpha")
    _queue(
        gateway,
        [
            _issue(
                "nl:1",
                sender="reader@gmail.example",
                subject="Fwd: Issue nl:1",
                text="---------- Forwarded message ---------\nFrom: <LETTER@news.example>\nBody",
            )
        ],
    )

    articles = await _source(client, own_addresses=READER).fetch_articles(
        AFTER, BEFORE, D1SourceStore(client).list_sources()
    )

    assert [a.source_name for a in articles] == ["The Letter"]


async def test_an_empty_sender_matches_no_source() -> None:
    gateway, client = _seeded_with_case_source("alpha")
    _queue(gateway, [_issue("nl:1", sender="")])

    articles = await _source(client).fetch_articles(
        AFTER, BEFORE, D1SourceStore(client).list_sources()
    )

    assert articles == []


def _held(gateway: FakeGateway) -> dict[str, str | None]:
    rows = gateway.db.query("SELECT id, held_at FROM newsletters ORDER BY id").rows
    return {r["id"]: r["held_at"] for r in rows}


async def test_a_run_stamps_what_it_holds_so_the_worker_may_evict_it_and_a_preview_does_not() -> (
    None
):
    gateway = FakeGateway("alpha")
    _queue(gateway, [_issue("nl:1", sender="forwarding-noreply@google.com"), _issue("nl:2")])
    client = _client("alpha", gateway)

    await _source(client, ack=False).fetch_articles(AFTER, BEFORE, SOURCES)
    after_preview = _held(gateway)
    await _source(client).fetch_articles(AFTER, BEFORE, SOURCES)
    first = _held(gateway)
    later = TenantNewsletterSource(client, now=lambda: NOW + timedelta(hours=12))
    await later.fetch_articles(AFTER, BEFORE, SOURCES)

    assert after_preview == {"nl:1": None, "nl:2": None}
    # The matched issue is acked; the unmatched one is held from the first run that saw it.
    assert first == {"nl:1": NOW.isoformat()}
    assert _held(gateway) == first


async def test_an_unknown_sender_becomes_a_tenant_source_and_its_issue_is_ingested() -> None:
    gateway, client = _seeded_with_case_source("alpha")
    _queue(gateway, [_issue("nl:1", sender="Writer@Else.example")])
    store = D1SourceStore(client)

    articles = await _source(client, create_source=store.insert).fetch_articles(
        AFTER, BEFORE, store.list_sources()
    )

    assert [a.source_name for a in articles] == ["writer@else.example"]
    assert store.list_sources()["writer@else.example"].email_match == "from:writer@else.example"
    assert _ids(gateway) == []
    assert {r.url.path for r in gateway.requests} == {"/t/alpha/query"}


async def test_a_sender_no_source_could_be_made_for_is_held_as_before() -> None:
    gateway, client = _seeded_with_case_source("alpha")
    _queue(gateway, [_issue("nl:1", sender="writer@else.example")])

    articles = await _source(client, create_source=lambda _source: False).fetch_articles(
        AFTER, BEFORE, D1SourceStore(client).list_sources()
    )

    assert articles == []
    assert _held(gateway) == {"nl:1": NOW.isoformat()}


async def test_a_tenant_at_the_policys_source_cap_gets_no_new_source() -> None:
    gateway = FakeGateway("alpha")
    cap = hosted_policy()["max_sources"]
    assert cap > MAX_NEW_SOURCES_PER_RUN
    full = {
        f"feed{i}": SourceConfig(name=f"feed{i}", url=f"https://f{i}.example/rss")
        for i in range(cap)
    }
    _queue(gateway, [_issue("nl:1", sender="writer@else.example")])
    made: list[SourceConfig] = []

    articles = await _source(
        _client("alpha", gateway), create_source=lambda s: made.append(s) or True
    ).fetch_articles(AFTER, BEFORE, full)

    assert made == []
    assert articles == []
    assert _held(gateway) == {"nl:1": NOW.isoformat()}


async def test_stopped_sources_hold_no_place_under_the_policys_source_cap() -> None:
    gateway = FakeGateway("alpha")
    cap = hosted_policy()["max_sources"]
    stopped = {
        f"spam{i}@x.example": SourceConfig(
            name=f"spam{i}@x.example",
            type="newsletter",
            lane=Lane.READ,
            email_match=f"from:spam{i}@x.example",
            review="declined",
        )
        for i in range(cap)
    }
    _queue(gateway, [_issue("nl:1", sender="writer@else.example")])
    made: list[SourceConfig] = []

    articles = await _source(
        _client("alpha", gateway), create_source=lambda s: made.append(s) or True
    ).fetch_articles(AFTER, BEFORE, stopped)

    assert [s.name for s in made] == ["writer@else.example"]
    assert [a.source_name for a in articles] == ["writer@else.example"]
    assert _held(gateway) == {}


async def test_mail_from_a_declined_source_is_deleted_not_held() -> None:
    gateway, client = _seeded_with_case_source("alpha")
    store = D1SourceStore(client)
    store.insert(
        SourceConfig(
            name="writer@else.example",
            type="newsletter",
            lane=Lane.READ,
            email_match="from:writer@else.example",
            review="declined",
        )
    )
    _queue(gateway, [_issue("nl:1", sender="writer@else.example")])

    articles = await _source(client, create_source=store.insert).fetch_articles(
        AFTER, BEFORE, store.list_sources()
    )

    assert articles == []
    assert _ids(gateway) == []
    assert set(store.list_sources()) == {"The Letter", "writer@else.example"}
