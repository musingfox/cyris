"""Each candidate a run newly suppresses leaves one durable row for the hold it started."""

from datetime import timedelta
from pathlib import Path

import pytest
from fakes import SqliteD1
from test_suppression_clock_run import (
    MODEL,
    SPREAD,
    T0,
    A,
    D,
    _fixed_clock,
    _vectors,
    _world,
)

from cyris.adapters.store.d1 import D1Error
from cyris.adapters.store.suppressions import D1Suppressions
from cyris.service_layer.run_digest import RunOptions, run_digest

pytestmark = pytest.mark.integration


def _rows_for(db, url: str) -> list[dict]:
    return db.query("SELECT * FROM suppressions WHERE candidate_url = ?", [url]).rows


class _FailingRecord:
    def __init__(self, real: D1Suppressions) -> None:
        self._real = real

    def active(self, now: str):
        return self._real.active(now)

    def record(self, run_at, period, rows):
        raise D1Error("down")


async def test_a_new_suppression_is_recorded_with_its_downvote_and_clock(
    tmp_path: Path, monkeypatch
) -> None:
    _fixed_clock(monkeypatch, T0)
    deps, _, db = _world(tmp_path, T0, _vectors(0.995, SPREAD))

    await run_digest(deps, RunOptions(period="morning"))

    [row] = _rows_for(db, A)
    assert row["nearest_down_url"] == D
    assert row["reason"] == "topic_near_downvote"
    assert row["until"] == (T0 + timedelta(days=7)).isoformat()
    assert row["model"] == MODEL


async def test_a_run_that_honours_a_hold_records_nothing_new(tmp_path: Path, monkeypatch) -> None:
    _fixed_clock(monkeypatch, T0)
    deps, _, db = _world(tmp_path, T0, _vectors(0.995, SPREAD))
    await run_digest(deps, RunOptions(period="morning"))

    later = T0 + timedelta(days=1)
    _fixed_clock(monkeypatch, later)
    deps, _, _ = _world(tmp_path / "2", later, _vectors(0.995, SPREAD), db=db)
    await run_digest(deps, RunOptions(period="morning"))

    assert len(_rows_for(db, A)) == 1


async def test_a_dry_run_writes_no_hold(tmp_path: Path, monkeypatch) -> None:
    _fixed_clock(monkeypatch, T0)
    deps, _, db = _world(tmp_path, T0, _vectors(0.995, SPREAD))

    await run_digest(deps, RunOptions(period="morning", dry_run=True))

    assert _rows_for(db, A) == []


async def test_a_failed_hold_write_still_drops_the_candidate(tmp_path: Path, monkeypatch) -> None:
    _fixed_clock(monkeypatch, T0)
    db = SqliteD1()
    deps, recorded, _ = _world(
        tmp_path,
        T0,
        _vectors(0.995, SPREAD),
        db=db,
        suppressions=_FailingRecord(D1Suppressions(db, MODEL)),
    )

    await run_digest(deps, RunOptions(period="morning"))

    [summary] = recorded
    assert summary["suppression_record_error"] == "down"
    assert summary["suppressed"] == 1
    assert summary["received"] == 9
    assert summary["status"] == "ok"


async def test_the_judged_count_is_what_judging_flagged_this_run(
    tmp_path: Path, monkeypatch
) -> None:
    _fixed_clock(monkeypatch, T0)
    deps, recorded, _ = _world(tmp_path, T0, _vectors(0.995, SPREAD))

    await run_digest(deps, RunOptions(period="morning"))

    assert recorded[-1]["suppressed"] == 1
