"""Load one hosted backup export into a new SQLite file, outside Cloudflare.

    python3 scripts/backup_to_sqlite.py EXPORT.jsonl.gz OUT.sqlite3

EXPORT is one file the hosted Worker's daily backup wrote, from the R2 bucket
(`<YYYY-MM-DD>/<tenant>.jsonl.gz`, or `_registry.jsonl.gz`); its format is ADR-0041's and
`workers/app/src/sqlite_dump.js`'s. OUT must not exist. The result is an ordinary SQLite file
holding every table, row, index, trigger and view the export carries, for reading the registry
by hand (docs/hosted-tenants.md) or keeping a tenant's data when the account is gone. Standard
library only, so it runs on any machine with Python.
"""

from __future__ import annotations

import base64
import gzip
import json
import sqlite3
import sys
from pathlib import Path

FORMAT = "cyris-sqlite-dump"
VERSION = 1
SCHEMA_KINDS = ("index", "trigger", "view")


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _value(value: object) -> tuple[object, str]:
    """A dumped value as a bound parameter, and the placeholder that keeps its SQLite type."""
    if isinstance(value, dict):
        if isinstance(value.get("blob"), str):
            return base64.b64decode(value["blob"]), "?"
        if "real" in value:
            return float(value["real"]), "CAST(? AS REAL)"
        raise ValueError(f"a dumped value is null, a number, a string, a blob or a real: {value}")
    return value, "?"


def load(export: Path, out: Path) -> dict[str, int]:
    """Every table of `export` into the new file `out`; answers each table's row count."""
    if out.exists():
        raise FileExistsError(f"{out} exists; name a new file")
    db = sqlite3.connect(out)
    try:
        counts = _load(export, db)
        db.commit()
    except BaseException:
        db.close()
        out.unlink(missing_ok=True)
        raise
    db.close()
    return counts


def _load(export: Path, db: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    later: list[str] = []
    table: tuple[str, str, int] | None = None
    header = end = None
    with gzip.open(export, "rt", encoding="utf-8", newline="\n") as lines:
        for line in lines:
            line = line.rstrip("\n")
            if not line:
                continue
            record = json.loads(line)
            if header is None:
                if record.get("format") != FORMAT or record.get("version") != VERSION:
                    raise ValueError(f"not a {FORMAT} version {VERSION}: {line[:200]}")
                header = record
            elif isinstance(record, list):
                if table is None or len(record) != table[2]:
                    raise ValueError(f"a row that matches no table: {line[:200]}")
                bound = [_value(v) for v in record]
                marks = ", ".join(mark for _, mark in bound)
                db.execute(
                    f"INSERT INTO {_quoted(table[0])} ({table[1]}) VALUES ({marks})",
                    [v for v, _ in bound],
                )
                counts[table[0]] += 1
            elif record.get("schema") in SCHEMA_KINDS:
                later.append(record["sql"])
            elif "columns" in record:
                db.execute(record["sql"])
                names = ", ".join(_quoted(c) for c in record["columns"])
                table = (record["table"], names, len(record["columns"]))
                counts[record["table"]] = 0
            elif "end" in record:
                end = record["end"]
            else:
                raise ValueError(
                    f"a dump line is a table, a row, a schema entry or the end: {line}"
                )
    if end is None:
        raise ValueError("the export is cut short: it has no end line")
    if end != {"tables": len(counts), "rows": sum(counts.values())}:
        raise ValueError(f"the end line counts {end}, not {len(counts)} tables")
    for statement in later:
        db.execute(statement)
    return counts


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    counts = load(Path(argv[0]), Path(argv[1]))
    for name, rows in counts.items():
        print(f"{rows:>8}  {name}")
    print(f"{len(counts)} tables into {argv[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
