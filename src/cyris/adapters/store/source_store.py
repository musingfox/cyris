"""Source definitions in D1, so adding a feed is a write rather than a rebuild.

`sources.yaml` remains the editable format. Under a D1 store this table is the
only list the pipeline reads at runtime, and `cyris sources push` is what puts
the file's contents here.
"""

from __future__ import annotations

import json
import logging

from cyris.adapters.store.d1 import D1Queryable, chunk_rows
from cyris.domain.models import SourceConfig

logger = logging.getLogger(__name__)

# name, url, type, config
_PARAMS_PER_ROW = 4


class D1SourceStore:
    """Read and replace the `sources` table."""

    def __init__(self, client: D1Queryable) -> None:
        self._db = client

    def list_sources(self) -> dict[str, SourceConfig]:
        """Every source, keyed by name. An empty table means "not configured yet"."""
        rows = self._db.query("SELECT name, url, type, config FROM sources ORDER BY name").rows
        return {row["name"]: source_from_row(row) for row in rows}

    def upsert(self, source: SourceConfig) -> None:
        """Write one source, creating or replacing the row `name` owns."""
        self._db.query(
            "INSERT INTO sources (name, url, type, config) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET url = excluded.url, "
            "type = excluded.type, config = excluded.config",
            _columns(source),
        )

    def insert(self, source: SourceConfig) -> bool:
        """Write one source only if no row owns its name; whether a row was written."""
        written = self._db.query(
            "INSERT INTO sources (name, url, type, config) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name) DO NOTHING",
            _columns(source),
        ).changes
        return written > 0

    def set_review(self, name: str, review: str | None) -> int:
        """Set one source's `review`, or drop it for None; how many rows changed, 0 or 1."""
        if review is None:
            sql = "UPDATE sources SET config = json_remove(coalesce(config, '{}'), '$.review') "
            return self._db.query(f"{sql}WHERE name = ?", [name]).changes
        sql = "UPDATE sources SET config = json_set(coalesce(config, '{}'), '$.review', ?) "
        return self._db.query(f"{sql}WHERE name = ?", [review, name]).changes

    def delete(self, name: str) -> int:
        """Retire one source.

        A D1 deployment fetches exactly this table, so deleting the last row
        leaves it with no sources: the next run stops rather than falling back to
        `sources.yaml`.
        """
        return self._db.query("DELETE FROM sources WHERE name = ?", [name]).changes

    def replace_all(self, sources: dict[str, SourceConfig]) -> int:
        """Make the table match `sources`, removals included, except the reader's decisions.

        A push is the whole list, not a merge: a source deleted from
        `sources.yaml` has to stop being polled, and a merge would keep it. A row with a
        review is the exception, the one thing the file cannot say: a stopped row is what keeps
        its sender's mail out (ADR-0050), so a push that dropped it would let that sender's next
        mail make the source again, a new one is waiting for the reader, and a kept one is the
        reader's decision to read it. Such a row stays,
        and a source the file names keeps the review its row held, whatever the file says.
        """
        held = {name: s for name, s in self.list_sources().items() if s.review is not None}
        sources = {
            name: s.model_copy(update={"review": held[name].review if name in held else None})
            for name, s in sources.items()
        } | {name: s for name, s in held.items() if name not in sources}
        self._db.query("DELETE FROM sources")
        if not sources:
            return 0

        rows = [_columns(source) for source in sources.values()]

        written = 0
        for chunk in chunk_rows(rows, _PARAMS_PER_ROW):
            values = ", ".join("(?, ?, ?, ?)" for _ in chunk)
            sql = f"INSERT INTO sources (name, url, type, config) VALUES {values}"
            written += self._db.query(sql, [v for row in chunk for v in row]).changes
        logger.info("Pushed %d sources to D1", written)
        return written


def source_from_row(row: dict) -> SourceConfig:
    """One `sources` row as its source; a ValueError when the row is not one."""
    data = json.loads(row.get("config") or "{}")
    data.update(name=row["name"], url=row.get("url"), type=row.get("type") or "rss")
    return SourceConfig.model_validate(data)


def _columns(source: SourceConfig) -> list:
    """name/url/type are columns; the rest rides as JSON."""
    # `review` is written only when it is set, so an ordinary source's row reads as it always has.
    exclude = {"name", "url", "type"} | ({"review"} if source.review is None else set())
    config = source.model_dump(mode="json", exclude=exclude)
    return [source.name, source.url, source.type, json.dumps(config)]
