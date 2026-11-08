"""Boot creates the area tables empty and loads every area row into the config."""

import json
from pathlib import Path

import pytest
from fakes import SqliteD1

from cyris.adapters.store.d1 import apply_schema

pytestmark = pytest.mark.integration

CONFIG = """
[agent_vault]
path = "{vault}"

[store]
backend = "{backend}"
database_id = "db"
"""


def _count(db: SqliteD1, table: str) -> int:
    return db.query(f"SELECT count(*) AS n FROM {table}").rows[0]["n"]


def _load(tmp_path: Path, backend: str, monkeypatch, db=None):
    from cyris.bootstrap import load_effective_config

    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setattr("cyris.adapters.store.d1.D1Client", lambda **_: db)
    config_path = tmp_path / "cyris.toml"
    config_path.write_text(
        CONFIG.format(vault=json.dumps(str(tmp_path))[1:-1], backend=backend),
        encoding="utf-8",
    )
    return load_effective_config(config_path, tmp_path / "sources.yaml")


def test_the_schema_twice_creates_empty_area_tables() -> None:
    db = SqliteD1()
    apply_schema(db)

    assert _count(db, "areas") == 0
    assert _count(db, "area_birth_threshold") == 0


def test_a_d1_boot_loads_every_area_row(tmp_path: Path, monkeypatch) -> None:
    db = SqliteD1()
    db.query(
        "INSERT INTO areas (id, name, position, status, merged_into, proposal_articles, "
        "proposal_issues, last_proposed_run) VALUES (1, 'Tech', 0, 'active', NULL, 0, 0, NULL)"
    )

    cfg = _load(tmp_path, "d1", monkeypatch, db)

    assert [(a.id, a.name, a.status) for a in cfg.areas] == [(1, "Tech", "active")]
