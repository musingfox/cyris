"""Normalized article tags in D1."""

from datetime import UTC, datetime

from cyris.adapters.store.d1 import D1Queryable, chunk_rows
from cyris.domain.tags import normalize_tag

_VOCAB_PARAMS = 2  # name, created_at
_MEMBER_PARAMS = 3  # article_url, tag, tagged_at


class D1TagStore:
    def __init__(self, client: D1Queryable) -> None:
        self._db = client

    def save(self, url_to_tags: dict[str, list[str]]) -> int:
        """Persist normalized tags; returns rows written across `tags` and `article_tags`.

        The vocabulary is INSERT OR IGNORE — a name's `created_at` marks its first
        sighting. Memberships are INSERT OR REPLACE so `tagged_at` follows the
        latest write instead of going stale. Writes are chunked against D1's
        bound-parameter budget, not sent row by row.
        """
        now = datetime.now(UTC).isoformat()
        vocabulary: set[str] = set()
        memberships: list[list[str]] = []
        for url, tags in url_to_tags.items():
            normalized = {value for tag in tags if (value := normalize_tag(tag)) is not None}
            vocabulary.update(normalized)
            memberships.extend([url, tag, now] for tag in sorted(normalized))

        written = 0
        vocab_rows = [[name, now] for name in sorted(vocabulary)]
        for chunk in chunk_rows(vocab_rows, _VOCAB_PARAMS):
            sql = "INSERT OR IGNORE INTO tags (name, created_at) VALUES " + (
                ", ".join("(?, ?)" for _ in chunk)
            )
            written += self._db.query(sql, [v for row in chunk for v in row]).changes
        for chunk in chunk_rows(memberships, _MEMBER_PARAMS):
            sql = "INSERT OR REPLACE INTO article_tags (article_url, tag, tagged_at) VALUES " + (
                ", ".join("(?, ?, ?)" for _ in chunk)
            )
            written += self._db.query(sql, [v for row in chunk for v in row]).changes
        return written
