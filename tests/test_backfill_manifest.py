"""Backfilling D1 pages_manifest from a local html directory."""

import importlib
import sys
from pathlib import Path

import pytest
from fakes import SqliteD1

from cyris.adapters.output.pages_deploy import asset_hash
from cyris.adapters.output.pages_manifest import D1PagesManifest

_SCRIPTS = str(Path(__file__).parent.parent / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
backfill = importlib.import_module("backfill_pages_manifest")


class RecordingD1:
    """SqliteD1 that also records every statement: tests assert both the stored
    result (via a real load) and the delete-then-insert SQL sequence."""

    def __init__(self):
        self._real = SqliteD1()
        self.queries = []

    def query(self, sql, params=None):
        self.queries.append((sql, params or []))
        return self._real.query(sql, params)


@pytest.fixture
def fake_d1(monkeypatch):
    fake = RecordingD1()
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    monkeypatch.setattr(backfill, "D1Client", lambda *args, **kwargs: fake)
    return fake


def test_the_stored_hashes_match_the_production_formula(tmp_path, fake_d1, capsys):
    (tmp_path / "a.html").write_bytes(b"hello")
    (tmp_path / "b-raw.html").write_bytes(b"world")

    code = backfill.main(["--html-dir", str(tmp_path), "--database-id", "x"])

    assert code == 0
    assert D1PagesManifest(fake_d1).load() == {
        "/a.html": asset_hash(b"hello", "html"),
        "/b-raw.html": asset_hash(b"world", "html"),
    }
    assert capsys.readouterr().out.strip() == "pages_manifest: 2 file(s)"


def test_the_manifest_is_replaced_wholesale(tmp_path, fake_d1):
    (tmp_path / "a.html").write_bytes(b"hello")

    backfill.main(["--html-dir", str(tmp_path), "--database-id", "x"])

    sqls = [sql for sql, _ in fake_d1.queries]
    assert sqls[0] == "DELETE FROM pages_manifest"
    assert sqls[1].startswith("INSERT INTO pages_manifest (path, hash, updated_at) VALUES")


def test_an_empty_directory_writes_nothing(tmp_path, fake_d1, capsys):
    code = backfill.main(["--html-dir", str(tmp_path), "--database-id", "x"])

    assert code != 0
    assert "no files" in capsys.readouterr().err
    assert fake_d1.queries == []


def test_a_missing_account_id_is_named_on_stderr(tmp_path, fake_d1, monkeypatch, capsys):
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID")
    (tmp_path / "a.html").write_bytes(b"hello")

    code = backfill.main(["--html-dir", str(tmp_path), "--database-id", "x"])

    assert code != 0
    assert "CLOUDFLARE_ACCOUNT_ID" in capsys.readouterr().err
    assert fake_d1.queries == []


def test_a_missing_token_is_named_on_stderr(tmp_path, fake_d1, monkeypatch, capsys):
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN")
    (tmp_path / "a.html").write_bytes(b"hello")

    code = backfill.main(["--html-dir", str(tmp_path), "--database-id", "x"])

    assert code != 0
    assert "CLOUDFLARE_API_TOKEN" in capsys.readouterr().err
    assert fake_d1.queries == []
