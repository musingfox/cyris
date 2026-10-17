"""A candidate the vote pass held back, and how long the hold lasts.

A suppression is not a vote. Nothing here stamps `triaged_at` or writes a rejection:
the article stays an ordinary pending row, and only a reader's own action decides it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# A week is the cycle a reader's own digests repeat over, so a candidate held back
# on one run is held through the next week's runs. A product choice, graded A in
# docs/architecture.md §5.
SUPPRESSION_CLOCK_DAYS = 7

REASON = "topic_near_downvote"


@dataclass(frozen=True)
class Suppression:
    candidate_url: str
    nearest_down_url: str
    down_cosine: float
    up_cosine: float | None
    outlier_score: float
    until: str


def suppression_until(run_at: str) -> str:
    """The instant the hold set by a run started at `run_at` expires, in the same form."""
    started = datetime.fromisoformat(run_at)
    return (started + timedelta(days=SUPPRESSION_CLOCK_DAYS)).isoformat()
