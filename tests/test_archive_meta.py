"""The archive's per-issue article counts, read from D1 `usage_log`."""

from pathlib import Path

import pytest
from fakes import CompoundSelectLimitedD1, SqliteD1, make_config

from cyris import bootstrap
from cyris.adapters.store.archive_meta import D1ArchiveMeta
from cyris.config import AgentVaultConfig, Config


def _log(db: SqliteD1, logged_at: str, date: str | None, period: str | None, n: int) -> None:
    db.query(
        "INSERT INTO usage_log (logged_at, digest_date, period, articles_included)"
        " VALUES (?, ?, ?, ?)",
        [logged_at, date, period, n],
    )


EARLIER = ("2026-09-01T00:00:00Z", "2026-09-01", "morning", 20)
LATER = ("2026-09-01T03:00:00Z", "2026-09-01", "morning", 22)


@pytest.mark.parametrize("rows", [[EARLIER, LATER], [LATER, EARLIER]], ids=["in-order", "reversed"])
def test_an_issue_logged_twice_counts_its_latest_row(rows) -> None:
    db = SqliteD1()
    for row in rows:
        _log(db, *row)

    assert D1ArchiveMeta(db).article_counts() == {("2026-09-01", "morning"): 22}


def test_a_row_without_a_date_or_period_names_no_issue() -> None:
    db = SqliteD1()
    _log(db, "2026-09-01T00:00:00Z", None, "morning", 5)
    _log(db, "2026-09-01T00:00:00Z", "2026-09-01", None, 6)

    assert D1ArchiveMeta(db).article_counts() == {}


def test_an_empty_log_has_no_counts() -> None:
    assert D1ArchiveMeta(SqliteD1()).article_counts() == {}


def test_the_read_stays_inside_the_compound_select_limit() -> None:
    db = CompoundSelectLimitedD1()
    for n in range(8):
        _log(db, f"2026-09-0{n + 1}T00:00:00Z", f"2026-09-0{n + 1}", "evening", n)

    assert len(D1ArchiveMeta(db).article_counts()) == 8


def test_the_read_writes_nothing() -> None:
    class Spy(SqliteD1):
        def __init__(self) -> None:
            super().__init__()
            self.sent: list[str] = []

        def query(self, sql, params=None):
            self.sent.append(sql)
            return super().query(sql, params)

    db = Spy()
    _log(db, *EARLIER)
    db.sent.clear()

    D1ArchiveMeta(db).article_counts()

    assert db.sent
    assert [sql for sql in db.sent if not sql.lstrip().upper().startswith("SELECT")] == []


def _config(tmp_path: Path, *, d1: bool) -> Config:
    cfg = make_config(agent_vault=AgentVaultConfig(path=tmp_path / "vault"))
    if d1:
        cfg.app.store.backend = "d1"
        cfg.app.store.database_id = "db"
        cfg.app.store.account_id = "acct"
        cfg.app.store.api_token = "tok"
    return cfg


def test_a_d1_deployment_reads_counts_from_its_usage_log(tmp_path: Path, monkeypatch) -> None:
    db = SqliteD1()
    _log(db, "2026-09-01T12:00:00Z", "2026-09-01", "evening", 9)
    monkeypatch.setattr(bootstrap, "build_d1_client", lambda _cfg: db)

    deps = bootstrap.build_deps(_config(tmp_path, d1=True))

    assert deps.archive_counts() == {("2026-09-01", "evening"): 9}


def test_a_json_deployment_has_no_counts(tmp_path: Path) -> None:
    assert bootstrap.build_deps(_config(tmp_path, d1=False)).archive_counts() == {}
