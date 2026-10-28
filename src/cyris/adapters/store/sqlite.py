"""Blocking query client over a local SQLite file.

`[store] backend = "sqlite"` runs the same SQL the D1 adapters run against a file
instead of Cloudflare's REST endpoint. D1 *is* SQLite, so nothing above this seam
changes: every `D1*Store` takes this the way it takes `D1Client`, and
`apply_schema` creates the same tables from the same `schema.sql`.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

from cyris.adapters.store.d1 import QueryResult

# A tick and the settings UI are two processes over one file, so a write can
# briefly meet another one. Long enough to wait it out; short enough that a
# stuck writer surfaces as an error instead of a hung run.
BUSY_TIMEOUT_MS = 5000


class SqliteClient:
    """`D1Queryable` over a file, serialised by one lock and the file's own lock."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None is autocommit: D1's query endpoint has no
        # transaction spanning statements either, so one statement is one
        # transaction on both. WAL lets the UI read while a run writes.
        self._conn = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
            timeout=BUSY_TIMEOUT_MS / 1000,
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        # D1 is the durability oracle here, not this file: a local deployment can
        # rebuild from the feeds, and FULL costs an fsync per statement.
        self._conn.execute("PRAGMA synchronous=NORMAL")

    def query(self, sql: str, params: list[Any] | None = None) -> QueryResult:
        with self._lock:
            try:
                cursor = self._conn.execute(sql, params or [])
            except sqlite3.ProgrammingError as error:
                if "one statement at a time" not in str(error):
                    raise
                # `apply_schema` sends the whole schema file and a migration step
                # joins its statements; D1 runs those, `execute` cannot.
                if params:
                    raise ValueError("a multi-statement script cannot bind parameters") from error
                before = self._conn.total_changes
                self._conn.executescript(sql)
                return QueryResult(rows=[], changes=self._conn.total_changes - before)
            rows = [dict(row) for row in cursor.fetchall()]
            # -1 for a SELECT, which D1 also reports as 0.
            return QueryResult(rows=rows, changes=max(cursor.rowcount, 0))

    def close(self) -> None:
        self._conn.close()
