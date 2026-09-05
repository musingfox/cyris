"""Every table the schema creates has a row in §4.

§4 exists to stop a feature from quietly finding a new home for state, and
`tests/test_local_writes.py` holds that line for local disk. D1 had no
equivalent: four of the ten tables happened to be named in an assertion written
beside their own store (`test_tag_store.py`, `test_story_store.py`), and the
eleventh table would have depended on whoever added it remembering the doc.

This reads the schema instead, so the reminder arrives as a failing test.
"""

import re
from pathlib import Path

SCHEMA = Path("src/cyris/adapters/store/schema.sql")
ARCHITECTURE = Path("docs/architecture.md")


def _table_names(sql: str) -> list[str]:
    return re.findall(r"CREATE TABLE\s+(?:IF NOT EXISTS\s+)?(\w+)", sql)


def _residency_table(doc: str) -> str:
    return doc.split("## 4. Data residency", 1)[1].split("## 5.", 1)[0]


def _rows_missing(sql: str, doc: str) -> list[str]:
    residency = _residency_table(doc)
    return [name for name in _table_names(sql) if f"`{name}`" not in residency]


def test_every_schema_table_is_a_residency_row() -> None:
    assert _rows_missing(SCHEMA.read_text(), ARCHITECTURE.read_text()) == []


def test_a_new_table_without_a_row_is_named() -> None:
    sql = SCHEMA.read_text() + "\nCREATE TABLE IF NOT EXISTS reading_history (url TEXT);\n"

    assert _rows_missing(sql, ARCHITECTURE.read_text()) == ["reading_history"]
