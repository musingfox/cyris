"""LocalD1Client: the D1 seam over a SQLite file, answering as D1 answers."""

import os
import sqlite3
import threading
from pathlib import Path

import pytest

from cyris.adapters.store import local_d1
from cyris.adapters.store.d1 import SCHEMA_PATH, D1Error
from cyris.adapters.store.local_d1 import LocalD1Client, split_statements

pytestmark = pytest.mark.unit


@pytest.fixture
def client(tmp_path: Path) -> LocalD1Client:
    c = LocalD1Client(tmp_path / "state" / "cyris.sqlite3")
    c.query("CREATE TABLE t (k TEXT PRIMARY KEY, v INTEGER NOT NULL)")
    yield c
    c.close()


def test_opens_the_file_in_wal_with_normal_sync_and_creates_its_directory(tmp_path):
    path = tmp_path / "nested" / "dir" / "cyris.sqlite3"
    c = LocalD1Client(path)

    assert path.exists()
    assert c.query("PRAGMA journal_mode").rows == [{"journal_mode": "wal"}]
    # 1 is NORMAL.
    assert c.query("PRAGMA synchronous").rows == [{"synchronous": 1}]


def test_one_statement_returns_rows_and_changes(client):
    written = client.query("INSERT INTO t (k, v) VALUES (?, ?), (?, ?)", ["a", 1, "b", 2])
    read = client.query("SELECT k, v FROM t ORDER BY k")

    assert written.changes == 2
    assert read.rows == [{"k": "a", "v": 1}, {"k": "b", "v": 2}]
    assert read.changes == 0


def test_each_statement_commits_itself(client):
    """Another process sees a write as soon as query returns, as with a REST POST."""
    client.query("INSERT INTO t (k, v) VALUES ('a', 1)")

    other = sqlite3.connect(client.path)
    assert other.execute("SELECT v FROM t").fetchall() == [(1,)]
    other.close()


def test_a_script_runs_every_statement(client):
    result = client.query(
        "-- a comment; with a semicolon\n"
        "INSERT INTO t (k, v) VALUES ('x;y', 1);\n"
        "INSERT INTO t (k, v) VALUES ('z', 2);\n"
        "SELECT k FROM t ORDER BY k;\n"
    )

    assert result.rows == [{"k": "x;y"}, {"k": "z"}]
    assert result.changes == 2


def test_a_script_that_fails_part_way_writes_nothing(client):
    """D1 runs one POST as a unit, so a failed step leaves no half of it behind."""
    with pytest.raises(D1Error, match="UNIQUE"):
        client.query("INSERT INTO t (k, v) VALUES ('a', 1);\nINSERT INTO t (k, v) VALUES ('a', 2);")

    assert client.query("SELECT COUNT(*) AS n FROM t").rows == [{"n": 0}]
    # And the connection is back in autocommit for the next call.
    client.query("INSERT INTO t (k, v) VALUES ('b', 1)")
    assert client.query("SELECT k FROM t").rows == [{"k": "b"}]


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t (k, v) VALUES ('a', 1);\nCOMMIT;\nINSERT INTO t (k, v) VALUES ('a', 2);",
        "INSERT INTO t (k, v) VALUES ('a', 1);\n-- done\nEND TRANSACTION;",
        "SAVEPOINT s; INSERT INTO t (k, v) VALUES ('a', 1); RELEASE s;",
        "INSERT INTO t (k, v) VALUES ('a', 1); /* undo */ ROLLBACK;",
        "BEGIN; INSERT INTO t (k, v) VALUES ('a', 1);",
        "BEGIN",
    ],
)
def test_transaction_control_is_refused_before_anything_runs(client, sql):
    """A call is one transaction already; a COMMIT inside it would land half the script."""
    with pytest.raises(D1Error, match="transaction"):
        client.query(sql)

    assert client.query("SELECT COUNT(*) AS n FROM t").rows == [{"n": 0}]


def test_a_trigger_body_that_ends_with_end_is_not_transaction_control(client):
    client.query(
        "CREATE TRIGGER bump AFTER INSERT ON t BEGIN UPDATE t SET v = v + 1; END;\n"
        "INSERT INTO t (k, v) VALUES ('a', 1);"
    )
    assert client.query("SELECT v FROM t").rows == [{"v": 2}]


def test_a_script_with_parameters_is_refused_before_it_runs(client):
    with pytest.raises(D1Error, match="several statements"):
        client.query("INSERT INTO t (k, v) VALUES (?, 1); INSERT INTO t VALUES ('b', 2)", ["a"])

    assert client.query("SELECT COUNT(*) AS n FROM t").rows == [{"n": 0}]


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t (k, v) VALUES (?, ?); -- one row",
        "INSERT INTO t (k, v) VALUES (?, ?); /* one row */\n",
        "-- one row\nINSERT INTO t (k, v) VALUES (?, ?);",
    ],
)
def test_one_statement_with_a_comment_binds_its_parameters(client, sql):
    """A comment is no second statement, so the bindings belong to the one there is."""
    assert client.query(sql, ["a", 1]).changes == 1
    assert client.query("SELECT k, v FROM t").rows == [{"k": "a", "v": 1}]


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        ("SELEC 1", None),
        ("SELECT * FROM missing", None),
        ("INSERT INTO t (k, v) VALUES (?, ?)", ["only-one"]),
        ("INSERT INTO t (k, v) VALUES ('a', NULL)", None),
    ],
)
def test_sqlite_errors_surface_as_d1_errors(client, sql, params):
    """The stores catch D1Error; a raw sqlite3.Error would escape every one of them."""
    with pytest.raises(D1Error):
        client.query(sql, params)


def test_the_shipped_schema_applies_twice(tmp_path):
    """schema.sql is one call of many statements, and every one is IF NOT EXISTS."""
    c = LocalD1Client(tmp_path / "cyris.sqlite3")
    schema = SCHEMA_PATH.read_text(encoding="utf-8")

    c.query(schema)
    c.query(schema)

    tables = {r["name"] for r in c.query("SELECT name FROM sqlite_master WHERE type='table'").rows}
    assert {"stored_articles", "settings", "sources", "digest_runs"} <= tables


def test_two_clients_on_one_file_share_writes(tmp_path):
    path = tmp_path / "cyris.sqlite3"
    first, second = LocalD1Client(path), LocalD1Client(path)
    first.query("CREATE TABLE t (k TEXT)")

    second.query("INSERT INTO t VALUES ('from-second')")

    assert first.query("SELECT k FROM t").rows == [{"k": "from-second"}]


def test_first_open_waits_out_a_lock_the_busy_timeout_does_not(tmp_path, monkeypatch):
    """The journal-mode switch is retried, so a file another process holds still opens."""
    monkeypatch.setattr(local_d1, "BUSY_TIMEOUT_SECONDS", 0)
    path = tmp_path / "cyris.sqlite3"
    holder = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN EXCLUSIVE")
    release = threading.Timer(0.3, lambda: holder.execute("COMMIT"))
    release.start()

    c = LocalD1Client(path)

    release.join()
    holder.close()
    assert c.query("PRAGMA journal_mode").rows == [{"journal_mode": "wal"}]


def test_first_open_gives_up_with_a_d1_error(tmp_path, monkeypatch):
    monkeypatch.setattr(local_d1, "BUSY_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(local_d1, "FIRST_OPEN_ATTEMPTS", 2)
    monkeypatch.setattr(local_d1, "FIRST_OPEN_RETRY_SECONDS", 0)
    path = tmp_path / "cyris.sqlite3"
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")

    with pytest.raises(D1Error, match="WAL"):
        LocalD1Client(path)

    holder.execute("ROLLBACK")
    holder.close()


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT 1", ["SELECT 1"]),
        ("SELECT 1;", ["SELECT 1;"]),
        ("SELECT 1;\n\n", ["SELECT 1;"]),
        ("SELECT 1; -- a note", ["SELECT 1;"]),
        ("SELECT 1; /* a */ -- b\n", ["SELECT 1;"]),
        ("SELECT 1; -- a\nSELECT 2", ["SELECT 1;", " -- a\nSELECT 2"]),
        ("SELECT ';';SELECT 2", ["SELECT ';';", "SELECT 2"]),
        ("-- x; y\nSELECT 1; /* a;b */ SELECT 2;", ["-- x; y\nSELECT 1;", " /* a;b */ SELECT 2;"]),
        (
            "CREATE TRIGGER g AFTER INSERT ON t BEGIN UPDATE t SET v = 1; END; SELECT 1;",
            ["CREATE TRIGGER g AFTER INSERT ON t BEGIN UPDATE t SET v = 1; END;", " SELECT 1;"],
        ),
    ],
)
def test_statements_split_where_sqlite_ends_them(sql, expected):
    assert list(split_statements(sql)) == expected


def _read_only_database(tmp_path: Path) -> Path:
    path = tmp_path / "read-only.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (k TEXT)")
    conn.commit()
    conn.close()
    path.chmod(0o444)
    return path


def _not_a_database(tmp_path: Path) -> Path:
    path = tmp_path / "not-a-database.sqlite3"
    path.write_text("this text is not a SQLite database " * 20)
    return path


@pytest.mark.parametrize("make", [_read_only_database, _not_a_database])
def test_a_file_no_wait_can_open_fails_at_once(tmp_path, monkeypatch, make):
    """Only a lock is waited out: a read-only or foreign file stays so however long one waits."""
    if make is _read_only_database and os.geteuid() == 0:
        pytest.skip("root writes a read-only file")
    sleeps: list[float] = []
    monkeypatch.setattr(local_d1.time, "sleep", sleeps.append)
    path = make(tmp_path)

    with pytest.raises(D1Error, match=str(path)):
        LocalD1Client(path)

    assert sleeps == []


def test_every_failure_names_the_file(client):
    """A command points the reader at the file only when the error says which one it is."""
    with pytest.raises(local_d1.LocalD1Error) as raised:
        client.query("SELECT * FROM missing")
    assert raised.value.path == client.path


def test_a_failing_rollback_does_not_hide_why_the_script_failed(client):
    """The statement that failed is the reader's answer; a rollback's own failure is not."""

    class RollbackFails:
        def __init__(self, conn) -> None:
            self._conn = conn

        @property
        def total_changes(self) -> int:
            return self._conn.total_changes

        def execute(self, sql, *args):
            if sql == "ROLLBACK":
                raise sqlite3.OperationalError("disk I/O error during rollback")
            return self._conn.execute(sql, *args)

        def close(self) -> None:
            self._conn.close()

    client._conn = RollbackFails(client._conn)

    with pytest.raises(D1Error, match="UNIQUE"):
        client.query("INSERT INTO t (k, v) VALUES ('a', 1);\nINSERT INTO t (k, v) VALUES ('a', 2);")


def test_a_statement_opening_with_with_counts_the_rows_it_wrote(client):
    client.query("CREATE TABLE counted (n INTEGER)")
    client.query("INSERT INTO counted VALUES (1), (2)")

    sql = "WITH step AS (SELECT 1 AS by) UPDATE counted SET n = n + (SELECT by FROM step)"
    assert client.query(sql).changes == 2
    assert client.query(f"{sql}; {sql}").changes == 4
