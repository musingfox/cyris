"""Tracked topics have one home per backend, and an empty list is a valid one."""

import json
from pathlib import Path

import pytest
from fakes import SqliteD1

from cyris.adapters.store.tracked_topics import D1TrackedTopicStore
from cyris.domain.models import TrackedTopic

pytestmark = pytest.mark.integration

CONFIG = """
[agent_vault]
path = "{vault}"

[store]
backend = "{backend}"
database_id = "db"
{topics}
"""

FILE_TOPIC = """
[[tracked_topics]]
name = "From File"
description = "A topic written in cyris.toml"
threshold = 0.7
model = "gemini-embedding-001"
"""


def _topic(name: str = "Anthropic", **overrides) -> TrackedTopic:
    fields = {
        "name": name,
        "description": "The company that makes Claude",
        "threshold": 0.72,
        "model": "gemini-embedding-001",
        **overrides,
    }
    return TrackedTopic(**fields)


@pytest.fixture
def store() -> D1TrackedTopicStore:
    return D1TrackedTopicStore(SqliteD1())


def test_an_empty_table_is_no_topics(store: D1TrackedTopicStore) -> None:
    assert store.list_topics() == []


def test_round_trips_every_field_in_name_order(store: D1TrackedTopicStore) -> None:
    store.upsert(_topic("曼報"))
    store.upsert(_topic("Anthropic", threshold=0.5, model="@cf/baai/bge-m3"))

    assert store.list_topics() == [
        _topic("Anthropic", threshold=0.5, model="@cf/baai/bge-m3"),
        _topic("曼報"),
    ]


def test_an_upsert_replaces_the_row_its_name_owns(store: D1TrackedTopicStore) -> None:
    store.upsert(_topic())
    store.upsert(_topic(description="Rewritten", threshold=0.9))

    assert store.list_topics() == [_topic(description="Rewritten", threshold=0.9)]


def test_delete_removes_one_topic_and_says_whether_it_existed(store: D1TrackedTopicStore) -> None:
    store.upsert(_topic("A"))
    store.upsert(_topic("B"))

    assert store.delete("A") == 1
    assert store.delete("A") == 0
    assert [t.name for t in store.list_topics()] == ["B"]


def test_a_row_that_fails_its_rule_is_left_out_not_raised(caplog) -> None:
    db = SqliteD1()
    db.query(
        "INSERT INTO tracked_topics (name, description, threshold, model) VALUES (?, ?, ?, ?)",
        ["Broken", "x", 7.0, "m"],
    )
    D1TrackedTopicStore(db).upsert(_topic())

    assert [t.name for t in D1TrackedTopicStore(db).list_topics()] == ["Anthropic"]
    assert "Broken" in caplog.text


def _load(tmp_path: Path, backend: str, monkeypatch, db=None, topics: str = ""):
    from cyris.bootstrap import load_effective_config

    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setattr("cyris.adapters.store.d1.D1Client", lambda **_: db)
    config_path = tmp_path / "cyris.toml"
    config_path.write_text(
        CONFIG.format(vault=json.dumps(str(tmp_path))[1:-1], backend=backend, topics=topics),
        encoding="utf-8",
    )
    return load_effective_config(config_path, tmp_path / "sources.yaml")


def test_a_json_deployment_reads_its_topics_from_the_file(tmp_path: Path, monkeypatch) -> None:
    cfg = _load(tmp_path, "json", monkeypatch, topics=FILE_TOPIC)

    assert [t.name for t in cfg.tracked_topics] == ["From File"]
    assert cfg.tracked_topics[0].threshold == 0.7


def test_a_file_without_topics_tracks_nothing_and_stops_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = _load(tmp_path, "json", monkeypatch)

    assert cfg.tracked_topics == []
    assert not any("tracked" in key for key in cfg.missing_settings)


def test_a_file_naming_one_topic_twice_fails_the_load(tmp_path: Path, monkeypatch) -> None:
    with pytest.raises(ValueError, match="From File"):
        _load(tmp_path, "json", monkeypatch, topics=FILE_TOPIC * 2)


def test_a_d1_deployment_reads_only_the_table(tmp_path: Path, monkeypatch) -> None:
    db = SqliteD1()
    D1TrackedTopicStore(db).upsert(_topic("From D1"))

    cfg = _load(tmp_path, "d1", monkeypatch, db, topics=FILE_TOPIC)

    assert [t.name for t in cfg.tracked_topics] == ["From D1"]


def test_an_empty_table_is_no_topics_not_the_file(tmp_path: Path, monkeypatch) -> None:
    cfg = _load(tmp_path, "d1", monkeypatch, SqliteD1(), topics=FILE_TOPIC)

    assert cfg.tracked_topics == []


def test_a_fresh_database_is_read_after_its_tables_exist(tmp_path: Path, monkeypatch) -> None:
    cfg = _load(tmp_path, "d1", monkeypatch, SqliteD1(with_schema=False))

    assert cfg.tracked_topics == []
