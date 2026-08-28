"""Pre-truncation story membership in D1."""

import json

from cyris.adapters.store.d1 import D1Queryable
from cyris.domain.models import StoryRecord


class D1StoryStore:
    def __init__(self, client: D1Queryable) -> None:
        self._db = client

    def save(self, digest_date: str, period: str, records: list[StoryRecord]) -> None:
        """Replace the window's story rows: delete-then-insert per (date, period)."""
        self._db.query(
            "DELETE FROM story_members WHERE story_id IN "
            "(SELECT id FROM stories WHERE digest_date = ? AND period = ?)",
            [digest_date, period],
        )
        self._db.query(
            "DELETE FROM stories WHERE digest_date = ? AND period = ?",
            [digest_date, period],
        )
        for record in records:
            self._db.query(
                "INSERT INTO stories (id, digest_date, period, heading, tags) "
                "VALUES (?, ?, ?, ?, ?)",
                [record.id, digest_date, period, record.heading, json.dumps(record.tags)],
            )
            for url in record.urls:
                self._db.query(
                    "INSERT OR IGNORE INTO story_members (story_id, article_url) VALUES (?, ?)",
                    [record.id, url],
                )
