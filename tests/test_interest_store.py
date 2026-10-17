"""The interest graph's D1 inputs: triaged votes and the tags behind active holds."""

import pytest
from fakes import SqliteD1

from cyris.adapters.store.interest import D1InterestReader
from cyris.domain.interest_graph import VotedTag

pytestmark = pytest.mark.unit

NOW = "2026-10-17T08:00:00+00:00"
LATER = "2026-10-20T08:00:00+00:00"
EARLIER = "2026-10-10T08:00:00+00:00"


def _article(db: SqliteD1, url: str, state: str, triaged: bool, tags: list[str]) -> None:
    db.query(
        "INSERT INTO stored_articles (url, title, published_at, source_name, source_tier, "
        "first_seen_at, state, triaged_at) VALUES (?, ?, ?, 'Source', 'filter', ?, ?, ?)",
        [url, url, NOW, NOW, state, NOW if triaged else None],
    )
    for tag in tags:
        db.query(
            "INSERT INTO article_tags (article_url, tag, tagged_at) VALUES (?, ?, ?)",
            [url, tag, NOW],
        )


def _hold(db: SqliteD1, candidate: str, down: str, until: str) -> None:
    db.query(
        "INSERT INTO suppressions (run_at, period, model, candidate_url, reason, "
        "nearest_down_url, down_cosine, up_cosine, outlier_score, until) "
        "VALUES (?, 'morning', 'm', ?, 'topic_near_downvote', ?, 0.9, NULL, 0.1, ?)",
        [NOW, candidate, down, until],
    )


def test_voted_rows_are_the_stamped_accepted_and_rejected_articles() -> None:
    db = SqliteD1()
    _article(db, "acc", "accepted", True, ["x", "y"])
    _article(db, "rej", "rejected", True, ["y"])
    _article(db, "pend", "pending", False, ["x"])
    _article(db, "filt", "rejected", False, ["z"])
    _article(db, "pend-stamped", "pending", True, ["x"])

    voted = D1InterestReader(db).read(NOW).voted

    assert sorted((v.article_url, v.tag) for v in voted) == [
        ("acc", "x"),
        ("acc", "y"),
        ("rej", "y"),
    ]
    assert all(isinstance(v, VotedTag) for v in voted)


def test_clocks_hold_the_tags_of_each_active_suppression_only() -> None:
    db = SqliteD1()
    _article(db, "rej", "rejected", True, ["y"])
    _hold(db, "cand-1", "rej", LATER)
    _hold(db, "cand-2", "rej", EARLIER)

    clocks = D1InterestReader(db).read(NOW).clocks

    assert [(c.tag, c.until.isoformat()) for c in clocks] == [("y", LATER)]


def test_a_suppression_whose_nearest_article_has_no_tags_gives_no_clock() -> None:
    db = SqliteD1()
    _article(db, "bare", "rejected", True, [])
    _hold(db, "cand", "bare", LATER)

    assert D1InterestReader(db).read(NOW).clocks == []


def test_no_rows_give_empty_inputs() -> None:
    inputs = D1InterestReader(SqliteD1()).read(NOW)

    assert inputs.voted == []
    assert inputs.clocks == []
    assert inputs.active_holds == 0


def test_active_holds_count_every_unexpired_hold_tagged_or_not() -> None:
    db = SqliteD1()
    _article(db, "bare", "rejected", True, [])
    _hold(db, "cand-1", "bare", LATER)
    _hold(db, "cand-2", "bare", EARLIER)

    assert D1InterestReader(db).read(NOW).active_holds == 1


def test_a_hold_on_an_article_a_human_has_since_decided_is_not_live() -> None:
    db = SqliteD1()
    _article(db, "rej", "rejected", True, ["y"])
    _article(db, "decided", "accepted", True, [])
    _article(db, "still-held", "pending", False, [])
    _hold(db, "decided", "rej", LATER)
    _hold(db, "still-held", "rej", LATER)

    inputs = D1InterestReader(db).read(NOW)

    assert inputs.active_holds == 1
    assert [c.tag for c in inputs.clocks] == ["y"]
