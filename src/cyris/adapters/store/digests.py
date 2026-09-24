"""Each issue's final DigestContent in D1, with the one render input it lacks."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from cyris.adapters.store.d1 import D1Queryable
from cyris.domain.models import DigestContent


class StoredDigest(BaseModel):
    model_config = ConfigDict(frozen=True)

    content: DigestContent
    raw_page: bool


class D1DigestStore:
    def __init__(self, client: D1Queryable) -> None:
        self._db = client

    def save(self, content: DigestContent, *, raw_page: bool) -> None:
        self._db.query(
            "INSERT INTO digests (date, period, content, raw_page, saved_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                content.date,
                content.period,
                content.model_dump_json(),
                int(raw_page),
                datetime.now(UTC).isoformat(timespec="seconds"),
            ],
        )

    def load(self, date: str, period: str) -> StoredDigest | None:
        rows = self._db.query(
            "SELECT content, raw_page FROM digests WHERE date = ? AND period = ?",
            [date, period],
        ).rows
        if not rows:
            return None
        row = rows[0]
        return StoredDigest(
            content=DigestContent.model_validate_json(row["content"]),
            raw_page=bool(row["raw_page"]),
        )
