"""The step runner: each step runs once, in order, and a failed step stops the run."""

import json

import pytest
from fakes import RecordingD1, SqliteD1
from legacy_schema import columns as _columns
from legacy_schema import old_db as _old_db
from legacy_schema import recorded as _recorded

from cyris.adapters.store.d1 import SCHEMA_PATH, D1Error, apply_schema
from cyris.adapters.store.migrations import LANE_STEP, Requires, Step

pytestmark = pytest.mark.integration

STEP_A = Step("A", ("CREATE TABLE mig_a (x TEXT)",))
STEP_B = Step("B", ("CREATE TABLE mig_b (x TEXT)",))
STEP_BAD = Step("bad", ("SELECT * FROM nope",))
STEP_C = Step("C", ("CREATE TABLE mig_c (x TEXT)",))


def _tables(db: SqliteD1) -> set[str]:
    rows = db.query("SELECT name FROM sqlite_master WHERE type = 'table'").rows
    return {r["name"] for r in rows}


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


def _insert(db: SqliteD1, url: str, tier: str, **extra) -> None:
    columns = {"state": "pending", "triaged_at": None, "score": None, **extra}
    db.query(
        "INSERT INTO stored_articles (url, published_at, source_name, source_tier, first_seen_at, "
        "state, triaged_at, score) VALUES (?, ?, 'Source', ?, ?, ?, ?, ?)",
        [
            url,
            "2026-10-01T00:00:00+00:00",
            tier,
            "2026-10-01T00:00:00+00:00",
            columns["state"],
            columns["triaged_at"],
            columns["score"],
        ],
    )


def _lanes(db: SqliteD1) -> dict[str, str]:
    rows = db.query("SELECT url, source_lane FROM stored_articles").rows
    return {r["url"]: r["source_lane"] for r in rows}


def test_each_old_tier_value_becomes_its_lane() -> None:
    db = _old_db()
    _insert(db, "https://example.com/filter", "filter")
    _insert(db, "https://example.com/summarize", "summarize")
    _insert(db, "https://example.com/fan", "fan")

    apply_schema(db)

    assert _lanes(db) == {
        "https://example.com/filter": "skim",
        "https://example.com/summarize": "read",
        "https://example.com/fan": "follow",
    }
    assert "source_tier" not in _columns(db)


def test_the_rewrite_leaves_every_other_column_of_the_row_alone() -> None:
    db = _old_db()
    _insert(
        db,
        "https://example.com/a",
        "summarize",
        state="accepted",
        triaged_at="2026-10-01T00:00:00Z",
        score=0.7,
    )
    before = db.query("SELECT * FROM stored_articles").rows[0]

    apply_schema(db)

    after = db.query("SELECT * FROM stored_articles").rows[0]
    assert {k: v for k, v in after.items() if k != "source_lane"} == {
        k: v for k, v in before.items() if k != "source_tier"
    }


def test_the_store_reads_the_migrated_rows() -> None:
    from cyris.adapters.store.d1_store import D1ArticleStore

    db = _old_db()
    for n in range(3):
        _insert(db, f"https://example.com/{n}", "filter")

    apply_schema(db)

    assert D1ArticleStore(db).count_by_state() == {"pending": 3}


def test_an_empty_database_gets_the_lane_column_and_no_rewrite() -> None:
    db = RecordingD1(with_schema=False)
    apply_schema(db)
    fresh = SqliteD1()

    assert _columns(db) == _columns(fresh)
    assert "0001_tier_to_lane" in [
        r["id"] for r in db.query("SELECT id FROM schema_migrations").rows
    ]
    assert not [s for s in db.statements if "SET source_tier" in s]


def _insert_source(db: SqliteD1, name: str, config: str) -> None:
    db.query("INSERT INTO sources (name, config) VALUES (?, ?)", [name, config])


def _configs(db: SqliteD1) -> dict[str, str]:
    return {r["name"]: r["config"] for r in db.query("SELECT name, config FROM sources").rows}


def test_each_source_config_gets_its_lane_and_a_malformed_one_is_left_alone() -> None:
    db = _old_db()
    _insert_source(db, "a", '{"tier":"summarize","tags":["a"],"language":"en"}')
    _insert_source(db, "b", '{"tier":"fan"}')
    _insert_source(db, "c", '{"tags":[]}')
    _insert_source(db, "d", "not json{")

    apply_schema(db)

    configs = _configs(db)
    assert json.loads(configs["a"]) == {"lane": "read", "tags": ["a"], "language": "en"}
    assert json.loads(configs["b"]) == {"lane": "follow"}
    assert json.loads(configs["c"]) == {"tags": []}
    assert configs["d"] == "not json{"
    assert not [c for c in configs.values() if "tier" in c]


def test_the_store_lists_the_migrated_lanes() -> None:
    from cyris.adapters.store.source_store import D1SourceStore
    from cyris.domain.models import Lane

    db = _old_db()
    _insert_source(db, "a", '{"tier":"summarize"}')
    _insert_source(db, "b", '{"tier":"fan"}')

    apply_schema(db)

    listed = D1SourceStore(db).list_sources()
    assert (listed["a"].lane, listed["b"].lane) == (Lane.READ, Lane.FOLLOW)


def test_an_unknown_tier_value_passes_through_as_its_lane() -> None:
    db = _old_db()
    _insert_source(db, "w", '{"tier":"weird"}')

    apply_schema(db)

    assert json.loads(_configs(db)["w"]) == {"lane": "weird"}


def test_a_second_apply_leaves_the_configs_as_they_were() -> None:
    db = _old_db()
    _insert_source(db, "a", '{"tier":"summarize"}')
    apply_schema(db)
    once = _configs(db)

    apply_schema(db)

    assert _configs(db) == once


def _seeded_old_db() -> SqliteD1:
    db = _old_db()
    _insert(db, "https://example.com/filter", "filter", triaged_at="2026-10-01T00:00:00Z")
    _insert(db, "https://example.com/summarize", "summarize", score=0.7)
    _insert(db, "https://example.com/fan", "fan")
    _insert_source(db, "a", '{"tier":"summarize","tags":["a"]}')
    _insert_source(db, "b", '{"tier":"fan"}')
    return db


def _final_state(db: SqliteD1) -> tuple[dict[str, str], dict[str, str]]:
    return _lanes(db), _configs(db)


def _clean_run_state() -> tuple[dict[str, str], dict[str, str]]:
    db = _seeded_old_db()
    apply_schema(db)
    return _final_state(db)


@pytest.mark.parametrize("k", [0, 1, 2, 3])
def test_an_interrupted_lane_migration_finishes_on_the_next_boot(k: int) -> None:
    db = _seeded_old_db()
    for statement in LANE_STEP.statements[:k]:
        db.query(statement)

    apply_schema(db)

    assert _final_state(db) == _clean_run_state()
    recorded = [r["id"] for r in db.query("SELECT id FROM schema_migrations").rows]
    assert recorded == ["0001_tier_to_lane"]


def test_the_whole_script_run_before_the_record_is_not_run_twice() -> None:
    db = _seeded_old_db()
    db.query(";\n".join(LANE_STEP.statements))

    apply_schema(db)
    apply_schema(db)

    assert _final_state(db) == _clean_run_state()
    recorded = [r["id"] for r in db.query("SELECT id FROM schema_migrations").rows]
    assert recorded == ["0001_tier_to_lane"]
