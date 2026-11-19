"""A hosted run's RSS edge: its own Durable Object's buffer, read through the run's gateway.

The hosted Worker polls each tenant's feeds into that tenant's `articles` hourly
(workers/app/src/tenant_buffer.js). A run reads its window from there, by buffer entry, and maps
each row exactly as `CloudflareRssSource` maps the rss Worker's, so a feed the run does not know
is skim with no tags either way, and a read that fails raises into `failed_sources`.
"""

from datetime import UTC, datetime

import httpx
import pytest
import respx
from test_tenant_gateway import FakeGateway

from cyris.adapters.fetch.rss_worker_source import (
    WORKER_ROW_CEILING,
    CloudflareRssSource,
    TenantRssSource,
)
from cyris.adapters.store import d1
from cyris.adapters.store.d1 import apply_schema
from cyris.adapters.store.tenant_gateway import (
    GatewayD1Client,
    TenantDbError,
    apply_hosted_schema,
)
from cyris.domain.models import Lane, SourceConfig
from cyris.service_layer.fetching import fetch_all_articles

pytestmark = pytest.mark.integration

AFTER = datetime(2026, 11, 18, 0, tzinfo=UTC)
BEFORE = datetime(2026, 11, 18, 8, tzinfo=UTC)
WORKER = "https://cyris-rss.test"

SOURCES = {
    "Known": SourceConfig(
        name="Known", url="https://known.test/feed", lane=Lane.READ, tags=["science"]
    ),
}


def _row(url: str, source: str, fetched_at: str, **extra: str) -> dict:
    """A buffer row as the hosted poll writes it: `fetched_at` is the poll's `toISOString()`."""
    return {
        "url": url,
        "guid": f"guid:{url}",
        "title": "Caf&eacute; &amp; more",
        "content": "<p>Body <b>text</b></p>",
        "author": "Ada",
        "published_at": "2026-11-17T22:00:00.000Z",
        "source_name": source,
        "fetched_at": fetched_at,
        "image_url": None,
        **extra,
    }


def _tenant(tenant: str, rows: list[dict], **gateway_args) -> tuple[FakeGateway, GatewayD1Client]:
    """A tenant's database as seeded and polled, and the gateway client its run holds."""
    gateway = FakeGateway(tenant, **gateway_args)
    apply_schema(gateway.db)
    apply_hosted_schema(gateway.db)
    columns = list(rows[0]) if rows else []
    for row in rows:
        gateway.db.query(
            f"INSERT INTO articles ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
            [row[c] for c in columns],
        )
    client = GatewayD1Client(tenant)
    client._http = httpx.Client(transport=httpx.MockTransport(gateway))
    return gateway, client


async def test_a_run_reads_its_window_by_buffer_entry_from_its_own_object() -> None:
    _, client = _tenant(
        "alpha",
        [
            _row("https://known.test/before", "Known", "2026-11-17T23:59:59.999Z"),
            _row("https://known.test/first", "Known", "2026-11-18T00:00:00.000Z"),
            _row("https://known.test/last", "Known", "2026-11-18T07:59:59.999Z"),
            _row("https://known.test/after", "Known", "2026-11-18T08:00:00.000Z"),
        ],
    )

    articles = await TenantRssSource(client).fetch_articles(AFTER, BEFORE, SOURCES)

    # Newest buffer entry first, the half-open window the rss Worker's `GET /articles` reads.
    assert [a.url for a in articles] == ["https://known.test/last", "https://known.test/first"]


async def test_the_query_goes_only_to_this_runs_tenant() -> None:
    gateway, client = _tenant(
        "alpha", [_row("https://known.test/1", "Known", "2026-11-18T01:00:00.000Z")]
    )

    await TenantRssSource(client).fetch_articles(AFTER, BEFORE, SOURCES)

    assert {request.url.path for request in gateway.requests} == {"/t/alpha/query"}
    assert gateway.refused == []


@respx.mock
async def test_rows_map_onto_articles_exactly_as_the_rss_workers_do() -> None:
    """A known feed takes its lane and tags; a feed the run does not know is skim, untagged."""
    rows = [
        _row("https://known.test/1", "Known", "2026-11-18T02:00:00.000Z"),
        _row(
            "https://stranger.test/1",
            "Stranger",
            "2026-11-18T01:00:00.000Z",
            image_url="https://stranger.test/1.jpg",
        ),
    ]
    _, client = _tenant("alpha", rows)
    # What the rss Worker answers for the same rows: its SELECT list, which has no fetched_at.
    worker_rows = [{k: v for k, v in row.items() if k != "fetched_at"} for row in rows]
    route = respx.get(f"{WORKER}/articles").mock(return_value=httpx.Response(200, json=worker_rows))

    hosted = await TenantRssSource(client).fetch_articles(AFTER, BEFORE, SOURCES)
    worker = await CloudflareRssSource(WORKER, "tok").fetch_articles(AFTER, BEFORE, SOURCES)

    assert route.call_count == 1
    assert hosted == worker
    known, stranger = hosted
    assert (known.source_lane, known.source_tags) == (Lane.READ, ["science"])
    assert (stranger.source_lane, stranger.source_tags) == (Lane.SKIM, [])
    assert known.title == "Café & more"
    assert known.content == "Body text"
    assert stranger.image_url == "https://stranger.test/1.jpg"


async def test_a_read_that_fails_lands_the_source_in_failed_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(d1.time, "sleep", lambda _: None)
    reset = httpx.Response(503, json={"errors": [{"message": "the object was reset"}]})
    _, client = _tenant("alpha", [], failures=[reset] * 10)
    source = TenantRssSource(client)

    with pytest.raises(TenantDbError, match="the object was reset"):
        await source.fetch_articles(AFTER, BEFORE, SOURCES)
    articles, failed = await fetch_all_articles([source], AFTER, BEFORE, SOURCES)

    assert (articles, failed) == ([], ["TenantRssSource"])


async def test_a_buffer_never_polled_is_an_empty_window_not_a_failure() -> None:
    _, client = _tenant("alpha", [])

    assert await TenantRssSource(client).fetch_articles(AFTER, BEFORE, SOURCES) == []
    assert await TenantRssSource(client).health_check() is True


async def test_the_read_asks_for_the_rss_workers_row_ceiling() -> None:
    gateway, client = _tenant("alpha", [])

    await TenantRssSource(client).fetch_articles(AFTER, BEFORE, SOURCES)

    import json

    [request] = gateway.requests
    assert json.loads(request.content)["params"] == [
        "2026-11-18T00:00:00.000Z",
        "2026-11-18T08:00:00.000Z",
        WORKER_ROW_CEILING,
    ]
