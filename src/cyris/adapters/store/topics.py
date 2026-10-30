"""Topics, their members and tags, and the votes on them, in D1."""

from __future__ import annotations

from datetime import UTC, datetime

from cyris.adapters.store.d1 import D1Queryable, chunk_rows
from cyris.domain.models import TopicRecord, TopicSummary, TopicTag, TopicVote

# `WHERE article_url IN (?, ...)`, kept under D1's 100 bound parameters.
_URLS_PER_STATEMENT = 90


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


class D1TopicStore:
    """TopicRepository over Cloudflare D1."""

    def __init__(self, client: D1Queryable) -> None:
        self._db = client

    def save_topics(
        self, digest_date: str, period: str, topics: list[TopicRecord], seen_at: datetime
    ) -> int:
        """Insert first, then delete what this save did not write, as `D1StoryStore` does.

        D1's HTTP API has no transaction, so a failure part-way leaves the union of the
        old and new rows rather than an issue with no topics.
        """
        stamp = _iso(seen_at)
        topic_rows = [[t.id, t.name, stamp, stamp] for t in topics]
        member_rows = [[t.id, url, digest_date, period, stamp] for t in topics for url in t.urls]
        tag_rows = [[t.id, tag.tag, str(tag.kind), stamp] for t in topics for tag in t.tags]

        written = 0
        for chunk in chunk_rows(topic_rows, 4):
            sql = (
                "INSERT INTO topics (id, name, first_seen, last_seen) VALUES "
                + ", ".join("(?, ?, ?, ?)" for _ in chunk)
                + " ON CONFLICT(id) DO UPDATE SET name = excluded.name, "
                "last_seen = max(topics.last_seen, excluded.last_seen)"
            )
            written += self._db.query(sql, [v for row in chunk for v in row]).changes
        for chunk in chunk_rows(member_rows, 5):
            sql = (
                "INSERT OR REPLACE INTO topic_members "
                "(topic_id, article_url, digest_date, period, seen_at) VALUES "
                + ", ".join("(?, ?, ?, ?, ?)" for _ in chunk)
            )
            written += self._db.query(sql, [v for row in chunk for v in row]).changes
        for chunk in chunk_rows(tag_rows, 4):
            sql = (
                "INSERT OR REPLACE INTO topic_tags (topic_id, tag, kind, seen_at) VALUES "
                + ", ".join("(?, ?, ?, ?)" for _ in chunk)
            )
            written += self._db.query(sql, [v for row in chunk for v in row]).changes

        self._db.query(
            "DELETE FROM topic_members WHERE digest_date = ? AND period = ? AND seen_at <> ?",
            [digest_date, period, stamp],
        )
        self._db.query(
            "DELETE FROM topic_tags WHERE seen_at <> ? AND topic_id IN (SELECT topic_id "
            "FROM topic_members WHERE digest_date = ? AND period = ? AND seen_at = ?)",
            [stamp, digest_date, period, stamp],
        )
        orphan = (
            "SELECT id FROM topics WHERE id NOT IN (SELECT topic_id FROM topic_members) "
            "AND id NOT IN (SELECT topic_id FROM topic_votes)"
        )
        self._db.query(f"DELETE FROM topic_tags WHERE topic_id IN ({orphan})")
        self._db.query(f"DELETE FROM topics WHERE id IN ({orphan})")
        return written

    def topics_of_urls(self, urls: list[str]) -> dict[str, str]:
        found: dict[str, tuple[str, str]] = {}
        unique = list(dict.fromkeys(urls))
        for i in range(0, len(unique), _URLS_PER_STATEMENT):
            batch = unique[i : i + _URLS_PER_STATEMENT]
            sql = (
                "SELECT article_url, topic_id, seen_at FROM topic_members "
                f"WHERE article_url IN ({', '.join('?' * len(batch))})"
            )
            for row in self._db.query(sql, batch).rows:
                url = row["article_url"]
                if url not in found or row["seen_at"] > found[url][0]:
                    found[url] = (row["seen_at"], row["topic_id"])
        return {url: topic for url, (_, topic) in found.items()}

    def record_topic_votes(self, votes: list[TopicVote]) -> int:
        rows = [[v.topic_id, v.direction, _iso(v.clicked_at)] for v in votes]
        written = 0
        for chunk in chunk_rows(rows, 3):
            sql = (
                "INSERT OR IGNORE INTO topic_votes (topic_id, direction, clicked_at) VALUES "
                + ", ".join("(?, ?, ?)" for _ in chunk)
            )
            written += self._db.query(sql, [v for row in chunk for v in row]).changes
        return written

    def topic_votes(self) -> list[TopicVote]:
        rows = self._db.query("SELECT topic_id, direction, clicked_at FROM topic_votes").rows
        return [TopicVote.model_validate(r) for r in rows]

    def recent_topics(self, since: datetime, until: datetime) -> list[TopicSummary]:
        rows = self._db.query(
            "SELECT id, name, last_seen FROM topics WHERE last_seen >= ? AND last_seen <= ? "
            "ORDER BY last_seen, id",
            [_iso(since), _iso(until)],
        ).rows
        return self._with_tags(rows)

    def topics_by_ids(self, ids: list[str]) -> list[TopicSummary]:
        rows: list[dict] = []
        unique = list(dict.fromkeys(ids))
        for i in range(0, len(unique), _URLS_PER_STATEMENT):
            batch = unique[i : i + _URLS_PER_STATEMENT]
            marks = ", ".join("?" * len(batch))
            sql = f"SELECT id, name, last_seen FROM topics WHERE id IN ({marks})"
            rows += self._db.query(sql, batch).rows
        return self._with_tags(rows)

    def _with_tags(self, rows: list[dict]) -> list[TopicSummary]:
        tags: dict[str, list[TopicTag]] = {}
        ids = [r["id"] for r in rows]
        for i in range(0, len(ids), _URLS_PER_STATEMENT):
            batch = ids[i : i + _URLS_PER_STATEMENT]
            sql = (
                "SELECT topic_id, tag, kind FROM topic_tags "
                f"WHERE topic_id IN ({', '.join('?' * len(batch))}) ORDER BY tag"
            )
            for r in self._db.query(sql, batch).rows:
                tags.setdefault(r["topic_id"], []).append(TopicTag(tag=r["tag"], kind=r["kind"]))
        return [
            TopicSummary(
                id=r["id"], name=r["name"], last_seen=r["last_seen"], tags=tags.get(r["id"], [])
            )
            for r in rows
        ]
