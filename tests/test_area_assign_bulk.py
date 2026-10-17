"""POST /api/areas/assign places many sources at once, and refuses a bad request before writing."""

import pytest
from aiohttp.test_utils import TestClient, TestServer
from fakes import SqliteD1

from cyris.adapters.store.areas import D1AreaStore
from cyris.adapters.store.source_store import D1SourceStore
from cyris.domain.models import SourceConfig
from cyris.entrypoints.triage_server import TriageServer

pytestmark = pytest.mark.integration


async def _client(server: TriageServer) -> TestClient:
    client = TestClient(TestServer(server._app))
    await client.start_server()
    return client


def _area(db: SqliteD1, name: str, status: str, position: int = 0, articles: int = 0) -> int:
    db.query(
        "INSERT INTO areas (name, position, status, proposal_articles) VALUES (?, ?, ?, ?)",
        [name, position, status, articles],
    )
    return db.query("SELECT max(id) AS id FROM areas").rows[0]["id"]


def _source(db: SqliteD1, name: str, **fields) -> None:
    D1SourceStore(db).upsert(SourceConfig(name=name, url=f"https://{name}.example/f", **fields))


def _area_ids(db: SqliteD1, name: str) -> int | None:
    return D1SourceStore(db).list_sources()[name].area_id


def _statuses(db: SqliteD1) -> dict[str, str]:
    return {row["name"]: row["status"] for row in db.query("SELECT name, status FROM areas").rows}


@pytest.fixture
def db() -> SqliteD1:
    db = SqliteD1()
    _area(db, "Tech", "active", 0)
    _area(db, "Dropped", "dismissed")
    _area(db, "Merged", "merged")
    _area(db, "Energy", "proposed")
    _source(db, "A")
    _source(db, "B")
    return db


def _server(db: SqliteD1) -> TriageServer:
    return TriageServer(area_store=D1AreaStore(db), source_store=D1SourceStore(db))


async def _post(db: SqliteD1, body, server: TriageServer | None = None):
    client = await _client(server or _server(db))
    try:
        response = await client.post("/api/areas/assign", json=body)
        return response.status, await response.json()
    finally:
        await client.close()


async def test_an_unknown_source_is_refused_with_its_name_and_writes_nothing(db: SqliteD1) -> None:
    status, data = await _post(db, {"assignments": {"Ghost": 1, "A": 1}})

    assert status == 400
    assert data == {
        "ok": False,
        "error": "Unknown sources: Ghost",
        "field": "assignments",
        "names": ["Ghost"],
    }
    assert _area_ids(db, "A") is None


async def test_a_dismissed_missing_or_merged_target_is_refused_and_writes_nothing(
    db: SqliteD1,
) -> None:
    dropped = _area(db, "Gone soon", "dismissed")
    energy = _area(db, "Also proposed", "proposed")

    status, _ = await _post(db, {"assignments": {"A": dropped}})
    assert status == 409
    assert _area_ids(db, "A") is None

    status, _ = await _post(db, {"assignments": {"A": 999}})
    assert status == 404
    assert _area_ids(db, "A") is None

    status, _ = await _post(db, {"assignments": {"A": energy, "B": dropped}})
    assert status == 409
    assert _statuses(db)["Also proposed"] == "proposed"
    assert _area_ids(db, "A") is None
    assert _area_ids(db, "B") is None


@pytest.mark.parametrize(
    "body",
    [
        {"assignments": {"A": "1"}},
        {"assignments": {"A": True}},
        {"assignments": {"A": 1.5}},
        [1, 2],
        {},
        {"assignments": [["A", 1]]},
    ],
)
async def test_a_malformed_body_is_refused_as_assignments_and_writes_nothing(
    db: SqliteD1, body
) -> None:
    status, data = await _post(db, body)

    assert status == 400
    assert data["field"] == "assignments"
    assert _area_ids(db, "A") is None


async def test_without_an_area_table_the_answer_matches_the_order_route(db: SqliteD1) -> None:
    server = TriageServer(source_store=D1SourceStore(db))
    client = await _client(server)
    try:
        assign = await client.post("/api/areas/assign", json={"assignments": {"A": 1}})
        order = await client.put("/api/areas/order", json={"ids": []})
        assign_body, order_body = await assign.json(), await order.json()
    finally:
        await client.close()

    assert assign.status == order.status == 409
    assert assign_body == order_body


async def test_each_named_source_gets_its_area_and_every_other_field_survives(db: SqliteD1) -> None:
    food = _area(db, "Food", "active", 1)
    D1SourceStore(db).upsert(
        SourceConfig(
            name="A",
            url="https://a.example/f",
            tags=["x", "y"],
            homepage="https://a.example",
            email_match="a@example.test",
        )
    )
    _source(db, "C")

    status, data = await _post(db, {"assignments": {"A": food, "B": None}})

    assert status == 200
    assert data == {"ok": True, "assigned": 2, "activated": []}
    stored = D1SourceStore(db).list_sources()
    assert stored["A"].area_id == food
    assert stored["A"].tags == ["x", "y"]
    assert stored["A"].homepage == "https://a.example"
    assert stored["A"].email_match == "a@example.test"
    assert stored["A"].lane == SourceConfig(name="A").lane
    assert stored["B"].area_id is None
    assert stored["C"].area_id is None


async def test_an_empty_assignment_writes_nothing(db: SqliteD1) -> None:
    status, data = await _post(db, {"assignments": {}})

    assert status == 200
    assert data == {"ok": True, "assigned": 0, "activated": []}
    assert _area_ids(db, "A") is None


async def test_proposals_named_in_a_request_activate_in_list_order_after_the_actives(
    db: SqliteD1,
) -> None:
    _area(db, "Second", "active", 1)
    _area(db, "Third", "active", 2)
    prop_p = _area(db, "P", "proposed", articles=9)
    prop_q = _area(db, "Q", "proposed", articles=2)
    _source(db, "C")

    server = _server(db)
    client = await _client(server)
    try:
        listed = [a["id"] for a in (await (await client.get("/api/areas")).json())["areas"]]
    finally:
        await client.close()
    status, data = await _post(db, {"assignments": {"A": prop_q, "B": prop_p}}, server)

    assert status == 200
    expected = [i for i in listed if i in (prop_p, prop_q)]
    assert [a["id"] for a in data["activated"]] == expected == [prop_p, prop_q]
    rows = {r["id"]: r for r in db.query("SELECT id, status, position FROM areas").rows}
    assert (rows[prop_p]["status"], rows[prop_p]["position"]) == ("active", 3)
    assert (rows[prop_q]["status"], rows[prop_q]["position"]) == ("active", 4)


async def test_a_request_with_only_active_or_no_area_activates_nothing(db: SqliteD1) -> None:
    _source(db, "C")

    status, data = await _post(db, {"assignments": {"A": 1, "C": None}})

    assert status == 200
    assert data["activated"] == []


async def test_a_proposal_activated_before_the_request_is_assigned_without_activation(
    db: SqliteD1,
) -> None:
    db.query("UPDATE areas SET status = 'active' WHERE name = 'Energy'")

    status, data = await _post(db, {"assignments": {"A": 4}})

    assert status == 200
    assert data["activated"] == []
    assert _area_ids(db, "A") == 4


async def test_a_proposal_no_source_points_at_stays_proposed(db: SqliteD1) -> None:
    status, _ = await _post(db, {"assignments": {"A": 1}})

    assert status == 200
    assert _statuses(db)["Energy"] == "proposed"
    assert _area_ids(db, "A") == 1
    assert _area_ids(db, "B") is None


def _failing_on_call(method, number: int):
    calls = {"n": 0}

    def wrapper(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == number:
            raise RuntimeError("disk gone")
        return method(*args, **kwargs)

    return wrapper


async def test_a_write_failing_midway_reports_what_landed(db: SqliteD1) -> None:
    prop = _area(db, "P", "proposed", articles=3)
    _source(db, "C")
    sources = D1SourceStore(db)
    sources.upsert = _failing_on_call(sources.upsert, 2)
    server = TriageServer(area_store=D1AreaStore(db), source_store=sources)

    status, data = await _post(db, {"assignments": {"A": prop, "B": prop, "C": prop}}, server)

    assert status == 500
    assert data == {
        "ok": False,
        "error": "disk gone",
        "assigned": ["A"],
        "activated": [{"id": prop, "name": "P"}],
    }
    assert _statuses(db)["P"] == "active"
    assert _area_ids(db, "A") == prop
    assert _area_ids(db, "B") is None
    assert _area_ids(db, "C") is None


async def test_the_same_request_without_the_failure_lands_everything(db: SqliteD1) -> None:
    prop = _area(db, "P", "proposed", articles=3)
    _source(db, "C")

    status, data = await _post(db, {"assignments": {"A": prop, "B": prop, "C": prop}})

    assert status == 200
    assert data == {"ok": True, "assigned": 3, "activated": [{"id": prop, "name": "P"}]}
    assert _area_ids(db, "C") == prop


async def test_a_failing_activation_reports_nothing_landed(db: SqliteD1) -> None:
    prop = _area(db, "P", "proposed", articles=3)
    area_store = D1AreaStore(db)
    area_store.activate = _failing_on_call(area_store.activate, 1)
    server = TriageServer(area_store=area_store, source_store=D1SourceStore(db))

    status, data = await _post(db, {"assignments": {"A": prop}}, server)

    assert status == 500
    assert data["assigned"] == []
    assert data["activated"] == []
    assert _statuses(db)["P"] == "proposed"
    assert _area_ids(db, "A") is None
