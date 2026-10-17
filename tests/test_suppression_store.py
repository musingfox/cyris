"""Suppression rows: one per candidate, with the downvote, the cosines and the clock."""

from datetime import UTC, datetime

import pytest
from fakes import CountingD1, SqliteD1

from cyris.adapters.store.d1_store import D1ArticleStore
from cyris.adapters.store.suppressions import D1Suppressions
from cyris.domain.models import Article, Tier
from cyris.domain.suppression import Suppression, suppression_until

pytestmark = pytest.mark.unit

RUN_AT = "2026-10-17T06:00:00+00:00"
MODEL = "gemini-embedding-001"


def _suppression(url: str, *, up_cosine: float | None = 0.40) -> Suppression:
    return Suppression(url, "d1", 0.81, up_cosine, 4.2, "2026-10-24T06:00:00+00:00")


def _article(url: str) -> Article:
    return Article(
        id=url,
        title="t",
        content="c",
        url=url,
        published_at=datetime(2026, 10, 17, tzinfo=UTC),
        source_name="s",
        source_tier=Tier.FILTER,
    )


def _rows(db) -> list[dict]:
    return db.query("SELECT * FROM suppressions ORDER BY candidate_url").rows


def test_a_suppression_is_stored_with_its_reason_period_and_model() -> None:
    db = SqliteD1()

    D1Suppressions(db, MODEL).record(RUN_AT, "morning", [_suppression("c1")])

    [row] = _rows(db)
    assert row == {
        "run_at": RUN_AT,
        "period": "morning",
        "model": MODEL,
        "candidate_url": "c1",
        "reason": "topic_near_downvote",
        "nearest_down_url": "d1",
        "down_cosine": 0.81,
        "up_cosine": 0.40,
        "outlier_score": 4.2,
        "until": "2026-10-24T06:00:00+00:00",
    }


def test_recording_the_same_run_twice_keeps_one_row() -> None:
    db = SqliteD1()
    store = D1Suppressions(db, MODEL)

    store.record(RUN_AT, "morning", [_suppression("c1")])
    store.record(RUN_AT, "morning", [_suppression("c1")])

    assert len(_rows(db)) == 1


def test_a_missing_upvote_cosine_is_null_not_zero() -> None:
    db = SqliteD1()

    D1Suppressions(db, MODEL).record(RUN_AT, "morning", [_suppression("c1", up_cosine=None)])

    assert _rows(db)[0]["up_cosine"] is None


def test_rows_are_written_in_statements_of_ten() -> None:
    db = CountingD1()

    D1Suppressions(db, MODEL).record(RUN_AT, "morning", [_suppression(f"c{i}") for i in range(30)])

    assert db.query_count == 3
    assert len(_rows(db)) == 30


def test_no_rows_send_no_statement() -> None:
    db = CountingD1()

    D1Suppressions(db, MODEL).record(RUN_AT, "morning", [])

    assert db.query_count == 0


def test_the_clock_runs_seven_days_in_the_same_form() -> None:
    assert suppression_until(RUN_AT) == "2026-10-24T06:00:00+00:00"


def test_recording_leaves_the_article_pending_and_unstamped() -> None:
    db = SqliteD1()
    D1ArticleStore(db).save([_article("c1")])
    before = db.query(
        "SELECT state, triaged_at, rejection_reason FROM stored_articles WHERE url = ?", ["c1"]
    ).rows

    D1Suppressions(db, MODEL).record(RUN_AT, "morning", [_suppression("c1")])

    after = db.query(
        "SELECT state, triaged_at, rejection_reason FROM stored_articles WHERE url = ?", ["c1"]
    ).rows
    assert after == before
    assert after == [{"state": "pending", "triaged_at": None, "rejection_reason": None}]
