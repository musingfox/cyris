"""Tracked topics in D1, written row by row from /settings.

A D1 deployment's only list; `cyris.toml` holds a `json` deployment's. An empty
table is a deployment that tracks nothing, never a missing setting.
"""

from __future__ import annotations

import logging

from pydantic import ValidationError

from cyris.adapters.store.d1 import D1Queryable
from cyris.domain.models import TrackedTopic

logger = logging.getLogger(__name__)


class D1TrackedTopicStore:
    """Read, upsert and delete rows of `tracked_topics`."""

    def __init__(self, client: D1Queryable) -> None:
        self._db = client

    def list_topics(self) -> list[TrackedTopic]:
        """Every valid topic, by name. A row that fails its rule is logged and left out."""
        rows = self._db.query(
            "SELECT name, description, threshold, model FROM tracked_topics ORDER BY name"
        ).rows
        topics = []
        for row in rows:
            try:
                topics.append(TrackedTopic.model_validate(row))
            except ValidationError as e:
                logger.warning("Ignoring tracked topic %r: %s", row.get("name"), e)
        return topics

    def upsert(self, topic: TrackedTopic) -> None:
        """Write one topic, creating or replacing the row `name` owns."""
        self._db.query(
            "INSERT INTO tracked_topics (name, description, threshold, model) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
            "description = excluded.description, threshold = excluded.threshold, "
            "model = excluded.model",
            [topic.name, topic.description, topic.threshold, topic.model],
        )

    def delete(self, name: str) -> int:
        """Remove one topic; the number of rows that went, 0 or 1."""
        return self._db.query("DELETE FROM tracked_topics WHERE name = ?", [name]).changes
