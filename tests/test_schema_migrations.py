"""The step runner: each step runs once, in order, and a failed step stops the run."""

import pytest
from fakes import RecordingD1, SqliteD1

from cyris.adapters.store.d1 import SCHEMA_PATH, D1Error, apply_schema
from cyris.adapters.store.migrations import Step

pytestmark = pytest.mark.integration

STEP_A = Step("A", ("CREATE TABLE mig_a (x TEXT)",))
STEP_B = Step("B", ("CREATE TABLE mig_b (x TEXT)",))
STEP_BAD = Step("bad", ("SELECT * FROM nope",))
STEP_C = Step("C", ("CREATE TABLE mig_c (x TEXT)",))


def _tables(db: SqliteD1) -> set[str]:
    rows = db.query("SELECT name FROM sqlite_master WHERE type = 'table'").rows
    return {r["name"] for r in rows}


def _recorded(db: SqliteD1) -> list[str]:
    return [
        r["id"] for r in db.query("SELECT id FROM schema_migrations ORDER BY applied_at, id").rows
    ]


def test_each_pending_step_runs_once_and_is_recorded_in_order() -> None:
    db = SqliteD1(with_schema=False)

    apply_schema(db, steps=(STEP_A, STEP_B))

    assert _recorded(db) == ["A", "B"]
    assert {"mig_a", "mig_b"} <= _tables(db)


def test_a_second_boot_sends_no_step_sql() -> None:
    db = RecordingD1(with_schema=False)
    apply_schema(db, steps=(STEP_A, STEP_B))
    db.statements.clear()

    apply_schema(db, steps=(STEP_A, STEP_B))

    assert db.statements == [
        SCHEMA_PATH.read_text(encoding="utf-8"),
        "SELECT id FROM schema_migrations",
    ]


def test_a_failing_step_stops_the_run_and_is_not_recorded() -> None:
    db = SqliteD1(with_schema=False)

    with pytest.raises(D1Error, match="bad"):
        apply_schema(db, steps=(STEP_A, STEP_BAD, STEP_C))

    assert _recorded(db) == ["A"]
    assert "mig_c" not in _tables(db)


def test_a_recorded_id_unknown_to_the_code_is_left_alone() -> None:
    db = SqliteD1()
    db.query("INSERT INTO schema_migrations (id) VALUES (?)", ["zz_future"])
    before = db.query("SELECT applied_at FROM schema_migrations WHERE id = 'zz_future'").rows

    apply_schema(db, steps=(STEP_A,))

    assert set(_recorded(db)) == {"zz_future", "A"}
    assert (
        db.query("SELECT applied_at FROM schema_migrations WHERE id = 'zz_future'").rows == before
    )


def test_schema_migrations_has_an_id_and_an_applied_at_column() -> None:
    columns = {r["name"] for r in SqliteD1().query("PRAGMA table_info('schema_migrations')").rows}

    assert columns == {"id", "applied_at"}
