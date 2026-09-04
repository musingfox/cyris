"""Whether this D1 has ever been the one publishing this Pages project."""

from __future__ import annotations

from datetime import UTC, datetime

from cyris.adapters.store.d1 import D1Queryable


class D1PagesDeployReceipt:
    """Durable mark that this database owns the deploy path for a Pages project."""

    def __init__(self, client: D1Queryable) -> None:
        self._db = client

    def record(self, project: str) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        self._db.query(
            "INSERT OR IGNORE INTO pages_deploy_receipt (project, created_at) VALUES (?, ?)",
            [project, now],
        )

    def exists(self, project: str) -> bool:
        rows = self._db.query(
            "SELECT 1 FROM pages_deploy_receipt WHERE project = ? LIMIT 1",
            [project],
        ).rows
        return bool(rows)
