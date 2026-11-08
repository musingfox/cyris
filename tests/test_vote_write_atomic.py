"""A human vote lands in one statement, so a run cannot slip between verdict and stamp."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fakes import SqliteD1

from cyris.adapters.promotions import record_votes
from cyris.adapters.store.d1_store import D1ArticleStore
from cyris.domain.models import Article, ArticleState, Lane
from cyris.domain.triage import RejectReason

pytestmark = pytest.mark.integration

AT = datetime(2026, 11, 7, 9, tzinfo=UTC)
LIKED = "https://example.com/liked"
DISLIKED = "https://example.com/disliked"


class RunAfterEveryStatement:
    """A D1 client that lets a digest run land right after each statement the vote sends."""

    def __init__(self, db: SqliteD1, verdicts: dict[str, ArticleState]) -> None:
        self._db = db
        self._run = D1ArticleStore(db)
        self._verdicts = verdicts
        self._running = False

    def query(self, sql: str, params: list[Any] | None = None):
        result = self._db.query(sql, params)
        if not self._running:
            self._running = True
            try:
                self._run.update_states(
                    {url: (state, None) for url, state in self._verdicts.items()}, "2026-11-07"
                )
            finally:
                self._running = False
        return result


def _article(url: str) -> Article:
    return Article(
        id=url,
        title="Title",
        url=url,
        content="Content",
        published_at=AT,
        source_name="Source",
        source_lane=Lane.SKIM,
    )


def _store_under_a_racing_run(verdicts: dict[str, ArticleState]) -> D1ArticleStore:
    db = SqliteD1()
    D1ArticleStore(db).save([_article(url) for url in verdicts], now=AT)
    return D1ArticleStore(RunAfterEveryStatement(db, verdicts))


def test_a_down_vote_survives_a_run_that_accepts_the_article() -> None:
    store = _store_under_a_racing_run({DISLIKED: ArticleState.ACCEPTED})

    record_votes(store, accepted=[], rejected=[DISLIKED], at=AT)

    [row] = store.get_by_urls([DISLIKED])
    assert row.state == ArticleState.REJECTED
    assert row.rejection_reason == RejectReason.NOT_INTERESTED
    assert row.triaged_at == AT


def test_an_up_vote_survives_a_run_that_rejects_the_article() -> None:
    store = _store_under_a_racing_run({LIKED: ArticleState.REJECTED})

    record_votes(store, accepted=[LIKED], rejected=[], at=AT)

    [row] = store.get_by_urls([LIKED])
    assert row.state == ArticleState.ACCEPTED
    assert row.rejection_reason is None
    assert row.triaged_at == AT


def test_a_mixed_ballot_survives_a_run_that_reverses_every_article() -> None:
    store = _store_under_a_racing_run(
        {LIKED: ArticleState.REJECTED, DISLIKED: ArticleState.ACCEPTED}
    )

    record_votes(store, accepted=[LIKED], rejected=[DISLIKED], at=AT)

    states = {row.url: (row.state, row.triaged_at) for row in store.get_by_urls([LIKED, DISLIKED])}
    assert states == {
        LIKED: (ArticleState.ACCEPTED, AT),
        DISLIKED: (ArticleState.REJECTED, AT),
    }


def test_a_url_saved_again_months_later_is_one_row_and_it_takes_the_vote() -> None:
    """The URL is the primary key, so no second row can stay behind unvoted."""
    store = D1ArticleStore(SqliteD1())
    store.save([_article(DISLIKED)], now=AT - timedelta(days=30))
    store.save([_article(DISLIKED)], now=AT)
    assert len(store.get_by_urls([DISLIKED])) == 1

    record_votes(store, accepted=[], rejected=[DISLIKED], at=AT)

    rows = store.get_by_urls([DISLIKED])
    assert [(r.state, r.rejection_reason, r.triaged_at) for r in rows] == [
        (ArticleState.REJECTED, RejectReason.NOT_INTERESTED, AT)
    ]
