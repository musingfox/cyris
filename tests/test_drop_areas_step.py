"""The step that drops the area skeleton: an old store keeps its sources and loses the rest."""

import json
import sqlite3

import pytest
from legacy_schema import LEGACY_SCHEMA_SQL

from cyris.adapters.store.d1 import apply_schema
from cyris.adapters.store.local_d1 import LocalD1Client
from cyris.adapters.store.migrations import DROP_AREAS_STEP, MIGRATIONS
from cyris.adapters.store.source_store import D1SourceStore

pytestmark = pytest.mark.integration

AREA_TABLES = {"areas", "area_birth_threshold"}

# name -> (url, type, config as stored). The configs are what an install that used
# areas holds: one source filed under an area, one never filed, one config past reading.
OLD_SOURCES = {
    "Filed": ("https://filed.example/feed", "rss", '{"lane":"read","tags":["ai"],"area_id":2}'),
    "Unfiled": ("https://unfiled.example/feed", "rss", '{"lane":"skim"}'),
    "Letter": (None, "newsletter", '{"lane":"follow","email_match":"from:a@b.c","area_id":1}'),
    "Broken": ("https://broken.example/feed", "rss", "not json"),
}


def _old_store(path) -> None:
    """A store as it stood with areas in use: two areas, one merged into the other, a threshold."""
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA_SQL)
    conn.executemany(
        "INSERT INTO sources (name, url, type, config) VALUES (?, ?, ?, ?)",
        [(name, *row) for name, row in OLD_SOURCES.items()],
    )
    conn.execute("INSERT INTO areas (id, name, status) VALUES (2, 'Tech', 'active')")
    conn.execute("INSERT INTO areas (id, name, status, merged_into) VALUES (1, 'AI', 'merged', 2)")
    conn.execute("INSERT INTO area_birth_threshold (id, articles, issues) VALUES (1, 10, 3)")
    conn.commit()
    conn.close()


def _tables(client: LocalD1Client) -> set[str]:
    rows = client.query("SELECT name FROM sqlite_master WHERE type = 'table'").rows
    return {row["name"] for row in rows}


def _sources(client: LocalD1Client) -> dict[str, tuple]:
    rows = client.query("SELECT name, url, type, config FROM sources ORDER BY name").rows
    return {row["name"]: (row["url"], row["type"], row["config"]) for row in rows}


@pytest.fixture
def old_client(tmp_path):
    path = tmp_path / "cyris.sqlite3"
    _old_store(path)
    client = LocalD1Client(path)
    # D1 enforces foreign keys, and `areas.merged_into` names another `areas` row.
    client.query("PRAGMA foreign_keys = ON")
    yield client
    client.close()


def test_an_old_store_loses_the_area_tables_and_keeps_every_source(old_client) -> None:
    assert _tables(old_client) >= AREA_TABLES

    apply_schema(old_client)

    assert not AREA_TABLES & _tables(old_client)
    after = _sources(old_client)
    assert set(after) == set(OLD_SOURCES)
    for name, (url, source_type, config) in OLD_SOURCES.items():
        assert after[name][:2] == (url, source_type)
        if config == "not json":
            assert after[name][2] == config
        else:
            assert json.loads(after[name][2]) == {
                k: v for k, v in json.loads(config).items() if k != "area_id"
            }


def test_the_sources_still_load_after_the_drop(old_client) -> None:
    apply_schema(old_client)
    # A config past reading stops the store's list whatever the step does.
    old_client.query("DELETE FROM sources WHERE name = 'Broken'")

    listed = D1SourceStore(old_client).list_sources()

    assert set(listed) == {"Filed", "Unfiled", "Letter"}
    assert listed["Filed"].tags == ["ai"]


def test_the_drop_is_recorded_once_and_a_second_boot_changes_nothing(old_client) -> None:
    apply_schema(old_client)
    once = _sources(old_client)

    apply_schema(old_client)

    assert _sources(old_client) == once
    rows = old_client.query("SELECT id FROM schema_migrations").rows
    assert [row["id"] for row in rows].count(DROP_AREAS_STEP.id) == 1


@pytest.mark.parametrize("k", range(len(DROP_AREAS_STEP.statements)))
def test_a_drop_cut_short_is_finished_by_the_next_boot(old_client, k: int) -> None:
    apply_schema(old_client, steps=MIGRATIONS[: MIGRATIONS.index(DROP_AREAS_STEP)])
    for statement in DROP_AREAS_STEP.statements[:k]:
        old_client.query(statement)

    apply_schema(old_client)

    assert not AREA_TABLES & _tables(old_client)
    assert all("area_id" not in config for _, _, config in _sources(old_client).values())


def test_a_new_store_never_holds_the_area_tables(tmp_path) -> None:
    client = LocalD1Client(tmp_path / "new.sqlite3")

    apply_schema(client)

    assert not AREA_TABLES & _tables(client)
    rows = client.query("SELECT id FROM schema_migrations").rows
    assert DROP_AREAS_STEP.id in {row["id"] for row in rows}
    client.close()
