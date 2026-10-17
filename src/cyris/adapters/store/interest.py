"""The interest graph's D1 inputs: the tags of human-voted articles and of active holds."""

from dataclasses import dataclass
from datetime import datetime

from cyris.adapters.store.d1 import D1Queryable
from cyris.adapters.store.suppressions import LIVE_HOLD_FROM, LIVE_HOLD_WHERE
from cyris.domain.interest_graph import TagClock, VotedTag
from cyris.domain.models import ArticleState


@dataclass(frozen=True)
class InterestInputs:
    voted: list[VotedTag]
    clocks: list[TagClock]
    active_holds: int


class D1InterestReader:
    def __init__(self, client: D1Queryable) -> None:
        self._db = client

    def read(self, now: str) -> InterestInputs:
        voted = self._db.query(
            "SELECT a.url AS article_url, t.tag AS tag, a.state AS state "
            "FROM stored_articles a JOIN article_tags t ON t.article_url = a.url "
            "WHERE a.triaged_at IS NOT NULL AND a.state IN (?, ?) ORDER BY a.url, t.tag",
            [ArticleState.ACCEPTED, ArticleState.REJECTED],
        ).rows
        clocks = self._db.query(
            "SELECT t.tag AS tag, s.until AS until "
            f"FROM {LIVE_HOLD_FROM} JOIN article_tags t ON t.article_url = s.nearest_down_url "
            f"WHERE {LIVE_HOLD_WHERE} ORDER BY s.until, t.tag",
            [now],
        ).rows
        active_holds = self._db.query(
            f"SELECT COUNT(*) AS n FROM {LIVE_HOLD_FROM} WHERE {LIVE_HOLD_WHERE}",
            [now],
        ).rows[0]["n"]
        return InterestInputs(
            voted=[
                VotedTag(
                    article_url=row["article_url"], tag=row["tag"], state=ArticleState(row["state"])
                )
                for row in voted
            ],
            clocks=[
                TagClock(tag=row["tag"], until=datetime.fromisoformat(row["until"]))
                for row in clocks
            ],
            active_holds=active_holds,
        )
