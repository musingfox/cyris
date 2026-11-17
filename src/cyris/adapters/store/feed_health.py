"""Each polled RSS feed's health, for `cyris doctor` and `/settings`.

`workers/rss` writes `feed_health` on every poll; how long a feed has gone
without output needs no state of its own, because `stored_articles` already
says when each source's newest article arrived. A feed with no `feed_health`
row has never been polled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from cyris.adapters.store.d1 import D1Queryable

# One Substack 429 is routine; three hourly polls failing in a row is not.
UNHEALTHY_FAILURE_STREAK = 3
# Feeds publish weekly or monthly, so a shorter silence is noise.
QUIET_DAYS = 30

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
