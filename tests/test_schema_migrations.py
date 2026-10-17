"""The step runner: each step runs once, in order, and a failed step stops the run."""

import pytest
from fakes import RecordingD1, SqliteD1

from cyris.adapters.store.d1 import SCHEMA_PATH, D1Error, apply_schema
from cyris.adapters.store.migrations import Requires, Step

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


def _sentinel_step(requires: Requires | None) -> Step:
    return Step("S", ("CREATE TABLE sentinel (x TEXT)",), requires)


def test_a_step_whose_column_is_already_gone_is_recorded_without_running() -> None:
    db = SqliteD1()

    apply_schema(db, steps=(_sentinel_step(Requires("stored_articles", "source_tier", True)),))

    assert "sentinel" not in _tables(db)
    assert _recorded(db) == ["S"]


def test_a_step_whose_column_is_still_there_runs() -> None:
    db = SqliteD1()
    db.query("ALTER TABLE stored_articles RENAME COLUMN source_lane TO source_tier")

    apply_schema(db, steps=(_sentinel_step(Requires("stored_articles", "source_tier", True)),))

    assert "sentinel" in _tables(db)
    assert _recorded(db) == ["S"]


_RENAME_STEP = Step(
    "R",
    ("ALTER TABLE stored_articles RENAME COLUMN source_tier TO source_lane",),
    Requires("stored_articles", "source_tier", True),
)


class _RacedD1(SqliteD1):
    """A database another instance migrates between our precondition check and our step."""

    def query(self, sql, params=None):
        if sql == _RENAME_STEP.statements[0] and self._has_old_column():
            super().query(sql)
        return super().query(sql, params)

    def _has_old_column(self) -> bool:
        rows = super().query("PRAGMA table_info('stored_articles')").rows
        return any(row["name"] == "source_tier" for row in rows)


def test_a_step_another_instance_applied_first_is_recorded_not_raised() -> None:
    db = _RacedD1()
    db.query("ALTER TABLE stored_articles RENAME COLUMN source_lane TO source_tier")

    apply_schema(db, steps=(_RENAME_STEP,))

    assert not db._has_old_column()
    assert _recorded(db) == ["R"]


def test_a_missing_table_never_satisfies_a_requirement() -> None:
    db = SqliteD1(with_schema=False)

    apply_schema(db, steps=(_sentinel_step(Requires("no_such_table", "x", False)),))

    assert "sentinel" not in _tables(db)
    assert _recorded(db) == ["S"]


def test_a_step_without_a_requirement_always_runs() -> None:
    db = SqliteD1()

    apply_schema(db, steps=(_sentinel_step(None),))

    assert "sentinel" in _tables(db)
