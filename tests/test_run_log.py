"""`digest_runs`: one row per run, and the newest real one read back."""

import json
import re

import pytest
from fakes import SqliteD1

from cyris.adapters.store.runs import D1RunLog

pytestmark = pytest.mark.unit


def _rows(db: SqliteD1) -> list[dict]:
    return db.query("SELECT * FROM digest_runs ORDER BY id").rows


def test_a_summary_becomes_one_row() -> None:
    db = SqliteD1()

    D1RunLog(db, "abc1234").record(
        {
            "status": "ok",
            "period": "morning",
            "dry_run": False,
            "wall_seconds": 3.5,
            "fetched": 12,
            "failed_sources": ["X"],
            "degraded": True,
            "llm": {"model": "m"},
        }
    )

    [row] = _rows(db)
    assert row["status"] == "ok"
    assert row["period"] == "morning"
    assert row["dry_run"] == 0
    assert row["wall_seconds"] == 3.5
    assert row["fetched"] == 12
    assert json.loads(row["failed_sources"]) == ["X"]
    assert row["build_sha"] == "abc1234"
    assert row["degraded"] == 1
    assert json.loads(row["summary"])["llm"]["model"] == "m"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00", row["finished_at"])


def test_a_run_that_died_early_leaves_its_missing_fields_null() -> None:
    db = SqliteD1()

    D1RunLog(db, "").record(
        {"status": "error", "period": "evening", "dry_run": True, "wall_seconds": 0.1}
    )

    [row] = _rows(db)
    assert row["fetched"] is None
    assert row["failed_sources"] is None
    assert row["degraded"] is None
    assert row["dry_run"] == 1
    assert row["status"] == "error"


def test_no_recorded_run_reads_as_none() -> None:
    assert D1RunLog(SqliteD1(), "").last() is None


def test_a_run_that_found_nothing_is_still_the_last_run() -> None:
    log = D1RunLog(SqliteD1(), "")
    log.record(
        {
            "status": "no_articles",
            "period": "morning",
            "dry_run": False,
            "fetched": 0,
            "failed_sources": [],
        }
    )

    last = log.last()
    assert last["status"] == "no_articles"
    assert last["fetched"] == 0
    assert last["failed_sources"] == []


def test_a_preview_is_never_the_last_run() -> None:
    log = D1RunLog(SqliteD1(), "")
    log.record({"status": "ok", "period": "morning", "dry_run": False})
    log.record({"status": "no_pending", "period": "morning", "dry_run": True})

    assert log.last()["status"] == "ok"


def test_two_runs_in_one_second_read_back_the_later() -> None:
    log = D1RunLog(SqliteD1(), "")
    log.record({"status": "ok", "period": "morning", "dry_run": False})
    log.record({"status": "no_pending", "period": "morning", "dry_run": False})

    assert log.last()["status"] == "no_pending"
