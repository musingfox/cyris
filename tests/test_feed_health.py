"""Each polled feed's health, read back from what the rss Worker and the store wrote."""

from datetime import UTC, datetime, timedelta

import pytest
from fakes import SqliteD1

from cyris.adapters.store.feed_health import (
    QUIET_DAYS,
    UNHEALTHY_FAILURE_STREAK,
    D1FeedHealth,
    FeedHealth,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def _db(*sources: tuple[str, str, str | None]) -> SqliteD1:
    db = SqliteD1()
    for name, kind, url in sources:
        db.query("INSERT INTO sources (name, url, type) VALUES (?, ?, ?)", [name, url, kind])
    return db


def _article(db: SqliteD1, url: str, source: str, first_seen: datetime) -> None:
    db.query(
        "INSERT INTO stored_articles (url, published_at, source_name, source_tier, first_seen_at) "
        "VALUES (?, ?, ?, 'filter', ?)",
        [url, first_seen.isoformat(), source, first_seen.isoformat()],
    )


def _polled(db: SqliteD1, name: str, failures: int, error: str | None = None) -> None:
    db.query(
        "INSERT INTO feed_health (name, consecutive_failures, last_error, last_failed_at, "
        "last_ok_at) VALUES (?, ?, ?, ?, ?)",
        [name, failures, error, "2026-10-08T11:00:00.000Z", "2026-10-01T11:00:00.000Z"],
    )


def test_a_feed_never_polled_with_no_article_reads_as_never_and_is_unhealthy() -> None:
    db = _db(("3Blue1Brown", "rss", "https://rsshub.test/youtube"))

    health = D1FeedHealth(db).read()

    assert health == {
        "3Blue1Brown": FeedHealth(
            name="3Blue1Brown",
            consecutive_failures=0,
            last_error=None,
            last_failed_at=None,
            last_ok_at=None,
            newest_article_at=None,
        )
    }
    assert health["3Blue1Brown"].problems(NOW) == ["no article stored yet"]


def test_the_poll_record_and_the_newest_stored_article_are_read_together() -> None:
    db = _db(("Feed", "rss", "https://f.test/rss"), ("Other", "rss", "https://o.test/rss"))
    _polled(db, "Feed", 2, "HTTP 429")
    _article(db, "https://f.test/1", "Feed", NOW - timedelta(days=3))
    _article(db, "https://f.test/2", "Feed", NOW - timedelta(days=1))
    _article(db, "https://o.test/1", "Other", NOW)

    feed = D1FeedHealth(db).read()["Feed"]

    assert feed.consecutive_failures == 2
    assert feed.last_error == "HTTP 429"
    assert feed.last_failed_at == "2026-10-08T11:00:00.000Z"
    assert feed.last_ok_at == "2026-10-01T11:00:00.000Z"
    assert feed.newest_article_at == (NOW - timedelta(days=1)).isoformat()


def test_only_the_feeds_the_worker_polls_are_read() -> None:
    db = _db(
        ("Feed", "rss", "https://f.test/rss"),
        ("Letter", "newsletter", None),
        ("No URL", "rss", None),
    )

    assert set(D1FeedHealth(db).read()) == {"Feed"}


def _health(failures: int = 0, error: str | None = None, newest: datetime | None = NOW):
    return FeedHealth(
        name="Feed",
        consecutive_failures=failures,
        last_error=error,
        last_failed_at=None,
        last_ok_at=None,
        newest_article_at=None if newest is None else newest.isoformat(),
    )


def test_a_streak_at_the_threshold_is_unhealthy_and_names_the_last_error() -> None:
    health = _health(UNHEALTHY_FAILURE_STREAK, "HTTP 503")
    assert health.problems(NOW) == [f"{UNHEALTHY_FAILURE_STREAK} failures in a row · HTTP 503"]


def test_a_streak_below_the_threshold_with_a_recent_article_is_healthy() -> None:
    assert _health(UNHEALTHY_FAILURE_STREAK - 1, "HTTP 429").problems(NOW) == []


def test_a_feed_quiet_for_the_whole_period_is_unhealthy() -> None:
    quiet = _health(newest=NOW - timedelta(days=QUIET_DAYS))
    recent = _health(newest=NOW - timedelta(days=QUIET_DAYS) + timedelta(hours=1))

    assert quiet.problems(NOW) == [f"no article in {QUIET_DAYS} days"]
    assert recent.problems(NOW) == []


def test_both_problems_are_named_when_both_hold() -> None:
    health = _health(5, "HTTP 503", newest=None)
    assert health.problems(NOW) == ["5 failures in a row · HTTP 503", "no article stored yet"]


def test_a_stored_time_in_the_workers_z_form_is_read_as_utc() -> None:
    health = FeedHealth("Feed", 0, None, None, None, "2026-09-01T00:00:00.000Z")
    assert health.problems(NOW) == [f"no article in {QUIET_DAYS} days"]
