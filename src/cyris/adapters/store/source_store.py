"""Source definitions in D1, so adding a feed is a write rather than a rebuild.

`sources.yaml` remains the editable format and the fallback. This table is what
the pipeline and the RSS Worker read at runtime, and `cyris sources push` is what
puts the file's contents here.
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
        sources = {}
        for row in rows:
            data = json.loads(row.get("config") or "{}")
            data.update(name=row["name"], url=row.get("url"), type=row.get("type") or "rss")
            sources[row["name"]] = SourceConfig.model_validate(data)
        return sources

    def upsert(self, source: SourceConfig) -> None:
        """Write one source, creating or replacing the row `name` owns."""
        self._db.query(
            "INSERT INTO sources (name, url, type, config) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET url = excluded.url, "
            "type = excluded.type, config = excluded.config",
            _columns(source),
        )

    def delete(self, name: str) -> int:
        """Retire one source.

        Emptying the table entirely hands the pipeline back to `sources.yaml`
        (`config._sources_from_d1`), so the last delete resurrects the file's
        list rather than fetching nothing. That is the documented fallback, not
        a bug to engineer around.
        """
        return self._db.query("DELETE FROM sources WHERE name = ?", [name]).changes

    def replace_all(self, sources: dict[str, SourceConfig]) -> int:
        """Make the table match `sources` exactly, removals included.

        A push is the whole list, not a merge: a source deleted from
        `sources.yaml` has to stop being polled, and a merge would keep it.
        """
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


def _columns(source: SourceConfig) -> list:
    """name/url/type are columns; the rest rides as JSON."""
    config = source.model_dump(mode="json", exclude={"name", "url", "type"})
    return [source.name, source.url, source.type, json.dumps(config)]
