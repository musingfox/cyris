"""Each polled RSS feed's health, for `cyris doctor` and `/settings`.

`workers/rss` writes `feed_health` on every poll, and `cyris ingest` writes it on a local
install (`D1FeedHealthWriter`); how long a feed has gone without output needs no state of
its own, because `stored_articles` already says when each source's newest article arrived.
A feed with no `feed_health` row has never been polled.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Literal

from cyris.adapters.store.d1 import D1Queryable

# Both numbers live in feed_health_rules.json, which the hosted Worker's sources list reads too.
_RULES = json.loads((files("cyris") / "feed_health_rules.json").read_text(encoding="utf-8"))
# One Substack 429 is routine; three hourly polls failing in a row is not.
UNHEALTHY_FAILURE_STREAK: int = _RULES["unhealthy_failure_streak"]
# Feeds publish weekly or monthly, so a shorter silence is noise.
QUIET_DAYS: int = _RULES["quiet_days"]

# The feeds the Worker polls are exactly the ones its own query selects
# (workers/rss/src/feeds.js), so this reads the same set.
_READ = """
SELECT s.name, h.consecutive_failures, h.last_error, h.last_failed_at, h.last_ok_at,
       a.newest_article_at
  FROM sources s
  LEFT JOIN feed_health h ON h.name = s.name
  LEFT JOIN (SELECT source_name, max(first_seen_at) AS newest_article_at
               FROM stored_articles GROUP BY source_name) a ON a.source_name = s.name
 WHERE s.type = 'rss' AND s.url IS NOT NULL
 ORDER BY s.name
"""


# Why a feed is unhealthy: failing polls, no article ever stored, or none for QUIET_DAYS.
FeedProblem = Literal["failing", "never_stored", "quiet"]


@dataclass(frozen=True)
class FeedHealth:
    name: str
    consecutive_failures: int
    last_error: str | None
    last_failed_at: str | None
    last_ok_at: str | None
    newest_article_at: str | None

    def findings(self, now: datetime) -> list[FeedProblem]:
        """Why this feed is unhealthy, one code per reason; empty when it is not.

        `/settings` words each in the page's language; `problems` words them for `cyris doctor`.
        """
        found: list[FeedProblem] = []
        if self.consecutive_failures >= UNHEALTHY_FAILURE_STREAK:
            found.append("failing")
        if self.newest_article_at is None:
            found.append("never_stored")
        elif now - _utc(self.newest_article_at) >= timedelta(days=QUIET_DAYS):
            found.append("quiet")
        return found

    @property
    def error_line(self) -> str:
        """The last poll error on one line; empty when there is none."""
        return " ".join((self.last_error or "").split())

    def problems(self, now: datetime) -> list[str]:
        """Why this feed is unhealthy, one phrase per reason; empty when it is not."""
        found = []
        for problem in self.findings(now):
            if problem == "failing":
                streak = f"{self.consecutive_failures} failures in a row"
                found.append(f"{streak} · {self.error_line}" if self.error_line else streak)
            elif problem == "never_stored":
                found.append("no article stored yet")
            else:
                found.append(f"no article in {QUIET_DAYS} days")
        return found


def _utc(stamp: str) -> datetime:
    parsed = datetime.fromisoformat(stamp)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class D1FeedHealth:
    """Reads `feed_health` beside each source's newest stored article."""

    def __init__(self, client: D1Queryable) -> None:
        self._db = client

    def read(self) -> dict[str, FeedHealth]:
        """Every RSS feed the Worker polls, keyed by source name."""
        return {
            row["name"]: FeedHealth(
                name=row["name"],
                consecutive_failures=row["consecutive_failures"] or 0,
                last_error=row["last_error"],
                last_failed_at=row["last_failed_at"],
                last_ok_at=row["last_ok_at"],
                newest_article_at=row["newest_article_at"],
            )
            for row in self._db.query(_READ).rows
        }


# One polled feed's outcome: its source name and the error that failed the poll, None when it
# succeeded. `workers/rss/src/buffer.js` records the same pair.
FeedOutcome = tuple[str, str | None]

# The two upserts of `recordHealth` in workers/rss/src/buffer.js. The shared cases in
# workers/rss/test/feed_health_cases.json hold the rows they leave equal.
_RECORD_OK = """
INSERT INTO feed_health (name, consecutive_failures, last_ok_at) VALUES (?, 0, ?)
ON CONFLICT(name) DO UPDATE SET consecutive_failures = 0, last_ok_at = excluded.last_ok_at
"""
_RECORD_FAILED = """
INSERT INTO feed_health (name, consecutive_failures, last_error, last_failed_at)
VALUES (?, 1, ?, ?)
ON CONFLICT(name) DO UPDATE SET
  consecutive_failures = feed_health.consecutive_failures + 1,
  last_error = excluded.last_error,
  last_failed_at = excluded.last_failed_at
"""


class D1FeedHealthWriter:
    """Upserts one `feed_health` row per polled feed, by the rss Worker's rules.

    A success resets the streak and sets `last_ok_at`, keeping the last error and its time;
    a failure adds one to the streak and sets the error and `last_failed_at`.
    """

    def __init__(self, client: D1Queryable) -> None:
        self._db = client

    def record(self, outcomes: Sequence[FeedOutcome], at: datetime) -> None:
        stamp = at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        for name, error in outcomes:
            if error is None:
                self._db.query(_RECORD_OK, [name, stamp])
            else:
                self._db.query(_RECORD_FAILED, [name, error, stamp])
