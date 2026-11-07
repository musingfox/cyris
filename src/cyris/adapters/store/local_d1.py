"""The D1 seam over a SQLite file on this machine, for a local install (ADR-0025).

D1 is SQLite, so every D1 store runs on this client unchanged: it answers
`D1Queryable.query` the way `D1Client` answers it over REST. A statement commits
on its own, as one REST POST does, and a call carrying several statements runs
them as one transaction, as D1 runs one POST.

The file is opened by the local server and by each scheduled run at once, so it
is in WAL mode, where readers do not block the one writer. It must never be
opened by a host process and a container through one bind mount: that corrupts
it (ADR-0025), which is why `[store] sqlite_path` may not sit in `agent-vault/`.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from cyris.adapters.store.d1 import D1Error, QueryResult

logger = logging.getLogger(__name__)

# How long a statement waits on another process's write lock before failing. A
# run's writes are single statements of at most 100 bound parameters, so a lock
# is held for milliseconds; five seconds is far past any wait a healthy file sees.
BUSY_TIMEOUT_SECONDS = 5

# The first-open journal-mode switch, retried apart from the busy timeout above.
FIRST_OPEN_ATTEMPTS = 50
FIRST_OPEN_RETRY_SECONDS = 0.1


# The statements that open, close or mark a transaction. A call is already one
# transaction (a statement commits itself, a script commits whole), so one of these
# inside it would commit or undo part of a call: D1 refuses them as well.
TRANSACTION_KEYWORDS = frozenset({"BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE"})
_FIRST_WORD = re.compile(r"[A-Za-z]+")


class LocalD1Error(D1Error):
    """A D1Error from the local file, which names the file so a command can point at it."""

    def __init__(self, path: Path, message: str) -> None:
        super().__init__(message)
        self.path = path


class LocalD1Client:
    """Execute SQL against a local SQLite file, with D1's answers and errors."""

    def __init__(self, path: Path) -> None:
        # Every way the file cannot be opened is a D1Error naming it, as a query's
        # failure is: the commands report a D1Error on one line, and anything else
        # escapes them as a traceback.
        self.path = path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # isolation_level=None: sqlite3 opens no transaction of its own, so each
            # statement commits itself, as one REST POST does. check_same_thread=False
            # because the server answers from worker threads; the lock serialises them.
            self._conn = sqlite3.connect(
                path, isolation_level=None, check_same_thread=False, timeout=BUSY_TIMEOUT_SECONDS
            )
        except (OSError, sqlite3.Error) as e:
            raise LocalD1Error(path, f"could not open {path}: {e}") from e
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        try:
            self._set_journal_mode()
            # NORMAL is durable across a process kill in WAL mode, and loses at most
            # the last commits on a power cut, never the file.
            self._conn.execute("PRAGMA synchronous=NORMAL")
        except (sqlite3.Error, D1Error) as e:
            self._conn.close()
            if isinstance(e, D1Error):
                raise
            raise LocalD1Error(path, f"could not open {path}: {e}") from e

    def _set_journal_mode(self) -> None:
        # WHY a loop of its own: two processes first-opening one new file (the
        # server and a run it starts) failed this PRAGMA with `database is locked`
        # 2 of 20 times with the busy timeout already set, because switching to
        # WAL takes a lock the busy handler does not wait on. Once one process has
        # switched, the mode is stored in the file and the PRAGMA returns at once.
        # Only a lock is waited out: a read-only file stays read-only.
        for attempt in range(FIRST_OPEN_ATTEMPTS):
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError as e:
                if not _locked(e):
                    raise
                if attempt == FIRST_OPEN_ATTEMPTS - 1:
                    raise LocalD1Error(
                        self.path, f"could not open {self.path} in WAL mode: {e}"
                    ) from e
                time.sleep(FIRST_OPEN_RETRY_SECONDS)

    def query(self, sql: str, params: list[Any] | None = None) -> QueryResult:
        statements = list(split_statements(sql))
        if any(_controls_transaction(statement) for statement in statements):
            # Refused before anything runs: a COMMIT halfway would land part of the call.
            raise LocalD1Error(
                self.path, "transaction control is not allowed: each call is one transaction"
            )
        with self._lock:
            try:
                if len(statements) <= 1:
                    cursor = self._conn.execute(sql, params or [])
                    rows = [dict(row) for row in cursor.fetchall()]
                    return QueryResult(rows=rows, changes=max(cursor.rowcount, 0))
                if params:
                    # D1 binds one parameter list to one statement; a script with
                    # bindings is a caller bug, refused before anything runs.
                    raise LocalD1Error(
                        self.path, "parameters cannot be bound to several statements"
                    )
                return self._run_script(statements)
            except sqlite3.Error as e:
                raise LocalD1Error(self.path, str(e)) from e

    def _run_script(self, statements: list[str]) -> QueryResult:
        """Several statements in one call, all or none, as D1 runs one POST."""
        rows: list[dict[str, Any]] = []
        changes = 0
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in statements:
                cursor = self._conn.execute(statement)
                rows.extend(dict(row) for row in cursor.fetchall())
                changes += max(cursor.rowcount, 0)
        except BaseException:
            # The failed statement is the answer; a rollback that fails too (the
            # transaction already gone, a disk error) must not replace it.
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                logger.warning("ROLLBACK after a failed script failed too", exc_info=True)
            raise
        self._conn.execute("COMMIT")
        return QueryResult(rows=rows, changes=changes)

    def close(self) -> None:
        self._conn.close()


def _locked(error: sqlite3.OperationalError) -> bool:
    """Whether another connection holds the lock: SQLITE_BUSY or SQLITE_LOCKED, extended or not."""
    return (error.sqlite_errorcode & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


def split_statements(sql: str) -> Iterator[str]:
    """Each complete statement in `sql`, cut where SQLite itself says one ends.

    A `;` inside a string, a comment or a trigger body does not end a statement,
    and `sqlite3.complete_statement` is SQLite's own tokenizer answering that. A
    tail with no closing `;` is a statement too, unless it holds only comments and
    whitespace, as a comment after the last `;` does.
    """
    start = 0
    for end, char in enumerate(sql):
        if char == ";" and sqlite3.complete_statement(sql[start : end + 1]):
            yield sql[start : end + 1]
            start = end + 1
    if not _only_comments(sql[start:]):
        yield sql[start:]


def _only_comments(sql: str) -> bool:
    """Whether `sql` is nothing but whitespace, `-- ...` lines and `/* ... */` blocks."""
    return not _past_comments(sql)


def _past_comments(sql: str) -> str:
    """`sql` from its first token on: leading whitespace and comments dropped."""
    rest = sql.lstrip()
    while rest.startswith(("--", "/*")):
        line = rest.startswith("--")
        rest = (rest.partition("\n") if line else rest[2:].partition("*/"))[2].lstrip()
    return rest


def _controls_transaction(statement: str) -> bool:
    """Whether a statement begins, ends or marks a transaction, by its first keyword.

    A trigger's `BEGIN ... END` is inside a `CREATE` statement, so it is not this.
    """
    match = _FIRST_WORD.match(_past_comments(statement))
    return match is not None and match.group(0).upper() in TRANSACTION_KEYWORDS
