"""`scripts/backup_to_sqlite.py` reads a hosted backup export outside Cloudflare.

The export is the one `workers/app/src/sqlite_dump.js` writes: `sqlite_dump.test.js` holds that
module's dump of its golden database equal to `workers/app/test/sqlite_dump_golden.jsonl`, and
this file loads the same golden dump, so the two sides read one format (ADR-0041).
"""

import gzip
import importlib.util
import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "workers/app/test/sqlite_dump_golden.jsonl"

_spec = importlib.util.spec_from_file_location(
    "backup_to_sqlite", ROOT / "scripts/backup_to_sqlite.py"
)
backup_to_sqlite = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backup_to_sqlite)


def _export(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "2026-11-22" / "alpha.jsonl.gz"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(gzip.compress(text.encode("utf-8")))
    return path


def _rows(db: sqlite3.Connection, sql: str) -> list[tuple]:
    return db.execute(sql).fetchall()


def test_the_golden_export_loads_with_every_value_its_sqlite_type_and_the_schema(
    tmp_path: Path,
) -> None:
    out = tmp_path / "alpha.sqlite3"

    counts = backup_to_sqlite.load(_export(tmp_path, GOLDEN.read_text("utf-8")), out)

    assert counts == {"areas": 2, "audit": 0, 'odd "name"': 2}
    db = sqlite3.connect(out)
    assert _rows(db, "SELECT id, name, merged_into FROM areas ORDER BY id") == [
        (1, "Old", 2),
        (2, "New", None),
    ]
    odd = 'SELECT i, r, t, b, n, typeof(r), typeof(n) FROM "odd ""name""" ORDER BY rowid'
    assert _rows(db, odd) == [
        (1099511627776, 0.25, 'line one\nline "two" 一', b"\x00\xff\x10", 7.0, "real", "real"),
        (None, 3.0, "", b"", 42, "real", "integer"),
    ]
    assert _rows(db, "SELECT type, name FROM sqlite_master ORDER BY type, name") == [
        ("index", "idx_areas_name"),
        ("table", "areas"),
        ("table", "audit"),
        ("table", 'odd "name"'),
        ("trigger", "areas_audit"),
        ("view", "area_names"),
    ]
    # The trigger is made after the rows, so it fired on none of them.
    assert _rows(db, "SELECT count(*) FROM audit") == [(0,)]


def test_a_cut_short_export_leaves_no_file(tmp_path: Path) -> None:
    cut = "\n".join(GOLDEN.read_text("utf-8").splitlines()[:5]) + "\n"
    out = tmp_path / "cut.sqlite3"

    with pytest.raises(ValueError, match="cut short"):
        backup_to_sqlite.load(_export(tmp_path, cut), out)

    assert not out.exists()


def test_an_existing_file_is_never_written_over(tmp_path: Path) -> None:
    out = tmp_path / "kept.sqlite3"
    out.write_bytes(b"mine")

    with pytest.raises(FileExistsError):
        backup_to_sqlite.load(_export(tmp_path, GOLDEN.read_text("utf-8")), out)

    assert out.read_bytes() == b"mine"


def test_main_prints_each_tables_rows(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    out = tmp_path / "alpha.sqlite3"

    code = backup_to_sqlite.main([str(_export(tmp_path, GOLDEN.read_text("utf-8"))), str(out)])

    assert code == 0
    assert capsys.readouterr().out.splitlines()[-1] == f"3 tables into {out}"
    assert backup_to_sqlite.main([]) == 2
