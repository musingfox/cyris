"""`build_deps` binds the run recorder to D1, stamped with the image's sha."""

from pathlib import Path

import pytest
from fakes import SqliteD1, make_config

from cyris import bootstrap
from cyris.config import AgentVaultConfig, Config

_SUMMARY = {"status": "ok", "period": "morning", "dry_run": False}


def _d1_config(tmp_path: Path) -> Config:
    cfg = make_config(agent_vault=AgentVaultConfig(path=tmp_path / "vault"))
    cfg.app.store.backend = "d1"
    cfg.app.store.database_id = "db"
    cfg.app.store.account_id = "acct"
    cfg.app.store.api_token = "tok"
    return cfg


@pytest.fixture
def db(monkeypatch) -> SqliteD1:
    db = SqliteD1()
    monkeypatch.setattr(bootstrap, "build_d1_client", lambda _cfg: db)
    return db


def test_a_d1_run_row_carries_the_image_sha(tmp_path: Path, monkeypatch, db) -> None:
    monkeypatch.setenv("CYRIS_GIT_SHA", "deadbeef")

    bootstrap.build_deps(_d1_config(tmp_path)).record_run(_SUMMARY)

    assert db.query("SELECT build_sha FROM digest_runs").rows == [{"build_sha": "deadbeef"}]


def test_an_image_built_without_a_sha_records_an_empty_one(tmp_path: Path, monkeypatch, db) -> None:
    monkeypatch.delenv("CYRIS_GIT_SHA", raising=False)

    bootstrap.build_deps(_d1_config(tmp_path)).record_run(_SUMMARY)

    assert db.query("SELECT build_sha FROM digest_runs").rows == [{"build_sha": ""}]


def test_a_json_backend_records_no_run(tmp_path: Path) -> None:
    cfg = make_config(agent_vault=AgentVaultConfig(path=tmp_path / "vault"))

    assert bootstrap.build_deps(cfg).record_run is None
