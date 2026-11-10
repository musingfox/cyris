"""How long a topic stays on the recent-topic list: an upvote slows its leaving, under a
ceiling of `K` lifetimes after its last article (ADR-0026). Here L = H = 7 and K = 3.
"""

from datetime import UTC, datetime, timedelta

import pytest

from cyris.domain import topics
from cyris.domain.models import TopicSummary, TopicVote
from cyris.domain.topic_rank import topic_preferences

pytestmark = pytest.mark.unit

L = H = 7
K = 3
SEEN = datetime(2026, 11, 1, 8, tzinfo=UTC)
STORY = TopicSummary(id="story", name="Story", last_seen=SEEN)
SECOND = timedelta(seconds=1)


def _vote(direction: int, at: datetime = SEEN) -> TopicVote:
    return TopicVote(topic_id="story", direction=direction, clicked_at=at)


def _on_list(after: timedelta, *votes: TopicVote) -> bool:
    at = SEEN + after
    prefs = topic_preferences(votes, at, H)
    return "story" in topics.recent_expiry([STORY], prefs, at, lifetime_days=L)


def test_with_no_vote_a_topic_leaves_at_its_lifetime() -> None:
    assert _on_list(timedelta(days=L) - SECOND)
    assert not _on_list(timedelta(days=L))


def test_one_fresh_upvote_keeps_a_topic_past_its_lifetime_and_it_leaves_before_the_ceiling():
    up = _vote(1)

    # The vote fades as the topic ages, so it leaves where 7 × 2 ** 0.5 ** (d / 7) = d: 9.24 days.
    assert _on_list(timedelta(days=L + 1), up)
    assert _on_list(timedelta(days=9.2), up)
    assert not _on_list(timedelta(days=9.3), up)


def test_many_upvotes_hold_a_topic_until_exactly_the_ceiling() -> None:
    ups = [_vote(1, SEEN + n * SECOND) for n in range(20)]

    assert _on_list(timedelta(days=K * L) - SECOND, *ups)
    assert not _on_list(timedelta(days=K * L), *ups)
    assert topics.topic_expiry(SEEN, 20.0, lifetime_days=L) == SEEN + timedelta(days=K * L)


def test_a_downvote_never_shortens_a_topic_below_its_lifetime() -> None:
    down = _vote(-1)

    assert _on_list(timedelta(days=L) - SECOND, down)
    assert not _on_list(timedelta(days=L), down)
    assert topics.topic_expiry(SEEN, -3.0, lifetime_days=L) == SEEN + timedelta(days=L)


def test_an_upvote_on_an_archived_page_cannot_lift_a_topic_past_the_ceiling() -> None:
    cast = SEEN + timedelta(days=2 * L)
    ups = [_vote(1, cast + n * SECOND) for n in range(10)]

    assert not _on_list(timedelta(days=2 * L) - SECOND, *ups)
    assert _on_list(timedelta(days=2 * L, hours=1), *ups)
    assert _on_list(timedelta(days=K * L) - SECOND, *ups)
    assert not _on_list(timedelta(days=K * L), *ups)
    assert not _on_list(timedelta(days=K * L + 1), *ups)
