"""Each issue's pipeline input URLs in D1: what a rerun rebuilds the issue from."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from cyris.adapters.store.d1 import D1Queryable


class D1DigestInputStore:
    def __init__(self, client: D1Queryable) -> None:
        self._db = client

    def save(self, date: str, period: str, urls: list[str]) -> None:
        self._db.query(
            "INSERT OR REPLACE INTO digest_inputs (date, period, urls, saved_at) "
            "VALUES (?, ?, ?, ?)",
            [
                date,
                period,
                json.dumps(urls),
                datetime.now(UTC).isoformat(timespec="seconds"),
            ],
        )

    def load(self, date: str, period: str) -> list[str] | None:
        rows = self._db.query(
            "SELECT urls FROM digest_inputs WHERE date = ? AND period = ?",
            [date, period],
        ).rows
        if not rows:
            return None
        return json.loads(rows[0]["urls"])

    def delete(self, date: str, period: str) -> None:
        self._db.query(
            "DELETE FROM digest_inputs WHERE date = ? AND period = ?",
            [date, period],
        )
