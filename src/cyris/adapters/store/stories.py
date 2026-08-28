"""Pre-truncation story membership in D1."""

from datetime import UTC, datetime

from cyris.adapters.store.d1 import D1Queryable, chunk_rows
from cyris.domain.models import StoryRecord

_STORY_PARAMS = 5  # id, digest_date, period, heading, created_at
_MEMBER_PARAMS = 2  # story_id, article_url


class D1StoryStore:
    def __init__(self, client: D1Queryable) -> None:
        self._db = client

    def save(self, digest_date: str, period: str, records: list[StoryRecord]) -> int:
        """Replace the window's story rows: delete-then-insert per (date, period).

        Returns the number of rows written across `stories` and `story_members`.
        Writes are chunked against D1's bound-parameter budget, not sent row by row.
        """
        self._db.query(
            "DELETE FROM story_members WHERE story_id IN "
            "(SELECT id FROM stories WHERE digest_date = ? AND period = ?)",
            [digest_date, period],
        )
        self._db.query(
            "DELETE FROM stories WHERE digest_date = ? AND period = ?",
            [digest_date, period],
        )

        now = datetime.now(UTC).isoformat()
        story_rows = [[r.id, digest_date, period, r.heading, now] for r in records]
        member_rows = [[r.id, url] for r in records for url in r.urls]

        written = 0
        for chunk in chunk_rows(story_rows, _STORY_PARAMS):
            sql = "INSERT INTO stories (id, digest_date, period, heading, created_at) VALUES " + (
                ", ".join("(?, ?, ?, ?, ?)" for _ in chunk)
            )
            written += self._db.query(sql, [v for row in chunk for v in row]).changes
        for chunk in chunk_rows(member_rows, _MEMBER_PARAMS):
            sql = "INSERT OR IGNORE INTO story_members (story_id, article_url) VALUES " + (
                ", ".join("(?, ?)" for _ in chunk)
            )
            written += self._db.query(sql, [v for row in chunk for v in row]).changes
        return written
