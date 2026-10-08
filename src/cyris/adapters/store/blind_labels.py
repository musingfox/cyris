"""The blind-label sample in D1: the filter pool it is drawn from, and each answer.

D1 only, and outside `ArticleRepository`: a `json` deployment has no sample to
keep, and every implementation of that Protocol would otherwise owe these reads.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from cyris.adapters.store.d1 import D1Queryable, chunk_rows
from cyris.adapters.store.d1_store import _iso
from cyris.domain.models import ArticleState, Tier
from cyris.domain.tags import NEWS_TAG
from cyris.domain.triage import RejectReason

_SAMPLE_COLUMNS = (
    "url",
    "position",
    "news",
    "pipeline_state",
    "stratum_size",
    "seed",
    "pool_since",
    "drawn_at",
)

# What the pipeline left a filter row in: shown, cut by a cap or suppressed, or
# discarded by the filter itself. Any other state, or a rejection the filter did
# not make, is no verdict of the filter's and stays out of the pool.
_KEPT_STATES = (ArticleState.ACCEPTED, ArticleState.PENDING)


@dataclass(frozen=True)
class PoolRow:
    url: str
    state: ArticleState
    news: bool


@dataclass(frozen=True)
class SampleRow:
    url: str
    position: int
    news: bool
    pipeline_state: ArticleState
    stratum_size: int


@dataclass(frozen=True)
class Card:
    """What the labeling page shows of one item: nothing of the pipeline's verdict."""

    url: str
    title: str
    source: str
    content: str


@dataclass(frozen=True)
class LabeledRow:
    url: str
    title: str
    news: bool
    pipeline_state: ArticleState
    stratum_size: int
    up: bool


@dataclass(frozen=True)
class Vote:
    url: str
    title: str
    up: bool


class D1BlindLabels:
    """Read the pool, keep one sample, and record each answer against it."""

    def __init__(self, client: D1Queryable) -> None:
        self._db = client

    def pool(self, since: datetime) -> list[PoolRow]:
        """Filter rows first seen at or after `since` that no human has stamped, by URL."""
        kept = ", ".join("?" for _ in _KEPT_STATES)
        rows = self._db.query(
            "SELECT url, state, source_tags FROM stored_articles "
            "WHERE source_tier = ? AND first_seen_at >= ? AND triaged_at IS NULL "
            f"AND (state IN ({kept}) OR (state = ? AND rejection_reason = ?)) ORDER BY url",
            [
                str(Tier.FILTER),
                # The store's own form, so the cutoff compares as `first_seen_at` is written.
                _iso(since),
                *map(str, _KEPT_STATES),
                str(ArticleState.REJECTED),
                str(RejectReason.FILTERED),
            ],
        ).rows
        return [
            PoolRow(
                url=r["url"],
                state=ArticleState(r["state"]),
                news=NEWS_TAG in json.loads(r["source_tags"] or "[]"),
            )
            for r in rows
        ]

    def size(self) -> int:
        return self._db.query("SELECT COUNT(*) AS n FROM blind_labels").rows[0]["n"]

    def replace_sample(
        self, rows: list[SampleRow], *, seed: int, since: datetime, drawn_at: datetime
    ) -> None:
        """Drop whatever sample was kept, answers included, and keep `rows` instead."""
        self._db.query("DELETE FROM blind_labels")
        values = [
            [
                r.url,
                r.position,
                int(r.news),
                str(r.pipeline_state),
                r.stratum_size,
                seed,
                _iso(since),
                _iso(drawn_at),
            ]
            for r in rows
        ]
        row_slots = "(" + ", ".join("?" for _ in _SAMPLE_COLUMNS) + ")"
        for chunk in chunk_rows(values, len(_SAMPLE_COLUMNS)):
            self._db.query(
                f"INSERT INTO blind_labels ({', '.join(_SAMPLE_COLUMNS)}) VALUES "
                + ", ".join(row_slots for _ in chunk),
                [value for row in chunk for value in row],
            )

    def progress(self) -> tuple[int, int]:
        """(answered, total), a skip counting as answered."""
        row = self._db.query(
            "SELECT COUNT(label) AS answered, COUNT(*) AS total FROM blind_labels"
        ).rows[0]
        return row["answered"], row["total"]

    def next_card(self) -> Card | None:
        rows = self._db.query(
            "SELECT b.url, a.title, a.source_name, a.content FROM blind_labels b "
            "JOIN stored_articles a ON a.url = b.url "
            "WHERE b.label IS NULL ORDER BY b.position LIMIT 1"
        ).rows
        if not rows:
            return None
        r = rows[0]
        return Card(url=r["url"], title=r["title"], source=r["source_name"], content=r["content"])

    def is_open(self, url: str) -> bool:
        """Whether `url` is in the sample and still unanswered."""
        rows = self._db.query(
            "SELECT 1 FROM blind_labels WHERE url = ? AND label IS NULL", [url]
        ).rows
        return bool(rows)

    def record(self, url: str, label: str, at: datetime) -> bool:
        """Answer one open item; False when it is not in the sample or already answered."""
        return (
            self._db.query(
                "UPDATE blind_labels SET label = ?, labeled_at = ? WHERE url = ? AND label IS NULL",
                [label, _iso(at), url],
            ).changes
            > 0
        )

    def labeled(self) -> list[LabeledRow]:
        """Every item answered up or down, in sample order; skips are not labels."""
        rows = self._db.query(
            "SELECT b.url, a.title, b.news, b.pipeline_state, b.stratum_size, b.label "
            "FROM blind_labels b JOIN stored_articles a ON a.url = b.url "
            "WHERE b.label IN ('up', 'down') ORDER BY b.position"
        ).rows
        return [
            LabeledRow(
                url=r["url"],
                title=r["title"],
                news=bool(r["news"]),
                pipeline_state=ArticleState(r["pipeline_state"]),
                stratum_size=r["stratum_size"],
                up=r["label"] == "up",
            )
            for r in rows
        ]

    def human_votes(self) -> list[Vote]:
        """Every human-stamped verdict outside the sample, so no label seeds its own score.

        Left out by URL rather than by who stamped it: a digest vote that landed on
        a sampled article between the draw and its label is excluded too.
        """
        rows = self._db.query(
            "SELECT url, title, state FROM stored_articles "
            "WHERE triaged_at IS NOT NULL AND state IN (?, ?) "
            "AND url NOT IN (SELECT url FROM blind_labels)",
            [str(ArticleState.ACCEPTED), str(ArticleState.REJECTED)],
        ).rows
        return [
            Vote(url=r["url"], title=r["title"], up=r["state"] == str(ArticleState.ACCEPTED))
            for r in rows
        ]
