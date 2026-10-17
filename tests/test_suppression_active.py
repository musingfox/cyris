"""The run asks which candidates are still under an unexpired suppression."""

import pytest
from fakes import SqliteD1

from cyris.adapters.store.suppressions import D1Suppressions
from cyris.domain.suppression import Suppression

pytestmark = pytest.mark.unit

MODEL = "gemini-embedding-001"
NOW = "2026-10-20T06:00:00+00:00"


def _hold(url: str, until: str, *, down: str = "d1", down_cosine: float = 0.81) -> Suppression:
    return Suppression(url, down, down_cosine, 0.40, 4.2, until)


def test_a_candidate_whose_hold_is_running_is_returned() -> None:
    db = SqliteD1()
    store = D1Suppressions(db, MODEL)
    store.record("2026-10-17T06:00:00+00:00", "morning", [_hold("c1", "2026-10-24T06:00:00+00:00")])

    assert list(store.active(NOW)) == ["c1"]


def test_a_hold_ending_exactly_now_is_not_returned() -> None:
    db = SqliteD1()
    store = D1Suppressions(db, MODEL)
    store.record("2026-10-17T06:00:00+00:00", "morning", [_hold("c1", NOW)])

    assert store.active(NOW) == {}


def test_the_later_active_hold_wins_over_an_expired_one() -> None:
    db = SqliteD1()
    store = D1Suppressions(db, MODEL)
    store.record("2026-10-10T06:00:00+00:00", "morning", [_hold("c1", "2026-10-17T06:00:00+00:00")])
    store.record(
        "2026-10-18T06:00:00+00:00",
        "morning",
        [_hold("c1", "2026-10-25T06:00:00+00:00", down="d2", down_cosine=0.9)],
    )

    [held] = store.active(NOW).values()
    assert held.until == "2026-10-25T06:00:00+00:00"
    assert held.nearest_down_url == "d2"


def test_an_empty_table_returns_nothing() -> None:
    assert D1Suppressions(SqliteD1(), MODEL).active(NOW) == {}


def test_a_stored_hold_comes_back_as_it_was_written() -> None:
    db = SqliteD1()
    written = Suppression("c1", "d1", 0.81, 0.40, 4.2, "2026-10-24T06:00:00+00:00")
    D1Suppressions(db, MODEL).record("2026-10-17T06:00:00+00:00", "morning", [written])

    assert D1Suppressions(db, MODEL).active(NOW) == {"c1": written}


def test_a_hold_on_an_article_a_human_has_since_decided_is_not_returned() -> None:
    db = SqliteD1()
    db.query(
        "INSERT INTO stored_articles (url, title, published_at, source_name, source_tier, "
        "first_seen_at, state, triaged_at) VALUES (?, ?, ?, 'Source', 'filter', ?, ?, ?)",
        ["c1", "c1", NOW, NOW, "accepted", NOW],
    )
    store = D1Suppressions(db, MODEL)
    until = "2026-10-24T06:00:00+00:00"
    store.record("2026-10-17T06:00:00+00:00", "morning", [_hold("c1", until), _hold("c2", until)])

    assert list(store.active(NOW)) == ["c2"]
