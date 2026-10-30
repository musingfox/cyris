"""Reading the topic store for option I: the recent-topic list and an issue's scores."""

from datetime import UTC, datetime, timedelta

import pytest

from cyris.domain.models import TopicKind, TopicRecord, TopicSummary, TopicTag, TopicVote
from cyris.service_layer.topic_order import recent_topic_list, score_issue_topics

pytestmark = pytest.mark.unit

AT = datetime(2026, 10, 30, 8, tzinfo=UTC)
H = L = 7


class FakeTopics:
    def __init__(self, topics: list[TopicSummary], votes: list[TopicVote]) -> None:
        self.topics = topics
        self.votes = votes

    def topic_votes(self) -> list[TopicVote]:
        return self.votes

    def recent_topics(self, since, until) -> list[TopicSummary]:
        return [t for t in self.topics if since <= t.last_seen <= until]

    def topics_by_ids(self, ids) -> list[TopicSummary]:
        return [t for t in self.topics if t.id in ids]


def _summary(tid: str, days_ago: float, *entities: str) -> TopicSummary:
    tags = [TopicTag(tag=e, kind=TopicKind.ENTITY) for e in entities]
    return TopicSummary(id=tid, name=tid, last_seen=AT - timedelta(days=days_ago), tags=tags)


def _vote(tid: str, direction: int, days_ago: float) -> TopicVote:
    return TopicVote(topic_id=tid, direction=direction, clicked_at=AT - timedelta(days=days_ago))


def test_the_recent_list_is_the_last_l_days_plus_topics_still_voted_up() -> None:
    store = FakeTopics(
        [
            _summary("fresh", 2),
            _summary("ended", 10),
            _summary("voted-up", 20),
            _summary("faded-up", 30),
            _summary("voted-down", 20),
        ],
        [_vote("voted-up", 1, 20), _vote("faded-up", 1, 30), _vote("voted-down", -1, 20)],
    )

    ids = [t.id for t in recent_topic_list(store, AT, half_life_days=H, lifetime_days=L)]

    # 20 days is under four half-lives of 7 (0.14 > 0.1); 30 days is over (0.05).
    assert ids == ["fresh", "voted-up"]


def test_an_issue_topic_takes_the_preference_of_an_earlier_voted_topic_through_an_entity() -> None:
    store = FakeTopics(
        [_summary("earlier", 1, "kmt"), _summary("other", 1, "kmt")],
        [_vote("earlier", 1, 1)],
    )
    issue = [
        TopicRecord(
            id="today",
            name="Today",
            urls=["u1", "u2"],
            tags=[TopicTag(tag="kmt", kind=TopicKind.ENTITY)],
        ),
        TopicRecord(id="quiet", name="Quiet", urls=["u3"]),
    ]

    scores = score_issue_topics(store, issue, AT, half_life_days=H, lifetime_days=L)

    # kmt is carried by three topics of the population: earlier, other and today.
    assert scores["u1"] == scores["u2"] == pytest.approx(0.5 ** (1 / H) / 3)
    assert scores["u3"] == 0.0


def test_a_voted_topic_that_has_ended_still_lends_its_vote_but_is_not_counted_in_df() -> None:
    store = FakeTopics([_summary("old", 10, "kmt")], [_vote("old", 1, 10)])
    issue = [
        TopicRecord(
            id="today", name="Today", urls=["u1"], tags=[TopicTag(tag="kmt", kind=TopicKind.ENTITY)]
        )
    ]

    scores = score_issue_topics(store, issue, AT, half_life_days=H, lifetime_days=L)

    assert scores["u1"] == pytest.approx(0.5 ** (10 / H))
