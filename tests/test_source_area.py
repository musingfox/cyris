"""A source carries the id of its area, in D1, in sources.yaml and through sources push."""

import json

import pydantic
import pytest
import yaml
from fakes import SqliteD1

from cyris.adapters.store.source_store import D1SourceStore
from cyris.config import SourcesConfig
from cyris.domain.models import SourceConfig

pytestmark = pytest.mark.integration


def _source(name: str = "a", **fields) -> SourceConfig:
    return SourceConfig(name=name, url=f"https://{name}.example/feed", **fields)


def test_an_area_id_round_trips_through_the_config_json() -> None:
    db = SqliteD1()
    store = D1SourceStore(db)

    store.upsert(_source(area_id=3))

    assert store.list_sources()["a"].area_id == 3
    raw = db.query("SELECT config FROM sources WHERE name = 'a'").rows[0]["config"]
    assert json.loads(raw)["area_id"] == 3


def test_a_stored_row_without_the_key_loads_as_no_area() -> None:
    db = SqliteD1()
    db.query(
        "INSERT INTO sources (name, url, type, config) VALUES (?, ?, ?, ?)",
        ["a", "https://a.example/feed", "rss", json.dumps({"tags": ["x"]})],
    )

    assert D1SourceStore(db).list_sources()["a"].area_id is None


def test_sources_push_stores_the_yaml_area_and_none_when_absent() -> None:
    db = SqliteD1()
    store = D1SourceStore(db)

    with_area = SourcesConfig.model_validate(
        yaml.safe_load("sources:\n  - {name: a, url: 'https://a.example/f', area_id: 3}\n")
    )
    store.replace_all({s.name: s for s in with_area.sources})
    assert store.list_sources()["a"].area_id == 3

    without = SourcesConfig.model_validate(
        yaml.safe_load("sources:\n  - {name: a, url: 'https://a.example/f'}\n")
    )
    store.replace_all({s.name: s for s in without.sources})
    assert store.list_sources()["a"].area_id is None


def test_a_non_integer_area_id_in_yaml_fails_validation_naming_the_field() -> None:
    with pytest.raises(pydantic.ValidationError, match="area_id"):
        SourcesConfig.model_validate(
            yaml.safe_load("sources:\n  - {name: a, url: 'https://a.example/f', area_id: tech}\n")
        )


async def _post_client(db: SqliteD1 | None):
    from aiohttp.test_utils import TestClient, TestServer

    from cyris.adapters.store.areas import D1AreaStore
    from cyris.entrypoints.triage_server import TriageServer

    if db is None:
        server = TriageServer()
    else:
        server = TriageServer(source_store=D1SourceStore(db), area_store=D1AreaStore(db))
    client = TestClient(TestServer(server._app))
    await client.start_server()
    return client


def _area_row(db: SqliteD1, area_id: int, status: str) -> None:
    db.query(
        "INSERT INTO areas (id, name, position, status) VALUES (?, ?, 0, ?)",
        [area_id, f"Area {area_id}", status],
    )


def _body(**fields) -> dict:
    return {"name": "a", "url": "https://a.example/f", "tags": ["x"], **fields}


async def test_a_save_without_an_area_key_keeps_the_stored_area() -> None:
    db = SqliteD1()
    _area_row(db, 1, "active")
    D1SourceStore(db).upsert(_source(area_id=1))
    client = await _post_client(db)
    try:
        response = await client.post("/api/sources", json=_body(tags=["changed"]))
    finally:
        await client.close()

    assert response.status == 200
    stored = D1SourceStore(db).list_sources()["a"]
    assert (stored.area_id, stored.tags) == (1, ["changed"])


async def test_a_null_area_clears_it_and_an_active_id_sets_it() -> None:
    db = SqliteD1()
    _area_row(db, 2, "active")
    D1SourceStore(db).upsert(_source(area_id=1))
    client = await _post_client(db)
    try:
        cleared = await client.post("/api/sources", json=_body(area_id=None))
        assert cleared.status == 200
        assert D1SourceStore(db).list_sources()["a"].area_id is None

        set_ = await client.post("/api/sources", json=_body(area_id=2))
        assert set_.status == 200
    finally:
        await client.close()

    assert D1SourceStore(db).list_sources()["a"].area_id == 2


async def test_an_area_that_is_not_active_is_refused_and_nothing_changes() -> None:
    db = SqliteD1()
    _area_row(db, 4, "proposed")
    D1SourceStore(db).upsert(_source(area_id=None))
    client = await _post_client(db)
    try:
        response = await client.post("/api/sources", json=_body(area_id=4))
        body = await response.json()
    finally:
        await client.close()

    assert response.status == 400
    assert body["field"] == "area_id"
    assert D1SourceStore(db).list_sources()["a"].area_id is None


async def test_a_partial_server_refuses_an_area_on_save() -> None:
    client = await _post_client(None)
    try:
        response = await client.post("/api/sources", json=_body(area_id=1))
    finally:
        await client.close()

    assert response.status == 409
