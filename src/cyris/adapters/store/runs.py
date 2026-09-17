"""One `digest_runs` row per run, and the newest non-preview one read back."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from cyris.adapters.store.d1 import D1Queryable

_LAST_COLUMNS = (
    "finished_at, status, period, dry_run, wall_seconds, fetched, failed_sources, "
    "build_sha, degraded"
)


class D1RunLog:
    """Stores what a run reported; the verdicts in it were made by the run, not here."""

    def __init__(self, client: D1Queryable, build_sha: str) -> None:
        self._db = client
        self._build_sha = build_sha

    def record(self, summary: dict) -> None:
        failed = summary.get("failed_sources")
        degraded = summary.get("degraded")
        self._db.query(
            "INSERT INTO digest_runs (finished_at, status, period, dry_run, wall_seconds, "
            "fetched, failed_sources, build_sha, degraded, summary) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                datetime.now(UTC).isoformat(timespec="seconds"),
                str(summary["status"]),
                str(summary["period"]),
                1 if summary["dry_run"] else 0,
                summary.get("wall_seconds"),
                summary.get("fetched"),
                None if failed is None else json.dumps(failed, ensure_ascii=False),
                self._build_sha,
                None if degraded is None else (1 if degraded else 0),
                json.dumps(summary, ensure_ascii=False, default=str),
            ],
        )

    def last(self) -> dict | None:
        rows = self._db.query(
            f"SELECT {_LAST_COLUMNS} FROM digest_runs WHERE dry_run = 0 "
            "ORDER BY finished_at DESC, id DESC LIMIT 1"
        ).rows
        if not rows:
            return None
        row = dict(rows[0])
        if row["failed_sources"] is not None:
            row["failed_sources"] = json.loads(row["failed_sources"])
        return row
