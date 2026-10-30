"""Option I, on synthetic issues shaped like the preference POC's scenarios (ADR-0023).

Three issues a day apart. Topic ids carry across issues here, as they will once the
filter assigns topics, so a vote on one issue can be seen moving the next two.
"""

from datetime import UTC, datetime, timedelta

import pytest

from cyris.domain.models import TopicKind, TopicTag, TopicVote
from cyris.domain.topic_rank import (
    RECENT_VOTE_CUTOFF,
    entity_tags,
    kept_by_upvote,
    score_topics,
    topic_preferences,
    vote_weight,
)

pytestmark = pytest.mark.unit

H = 7.0
T1 = datetime(2026, 10, 28, 8, tzinfo=UTC)
ISSUES = [T1, T1 + timedelta(days=1), T1 + timedelta(days=2)]

E, D, R = TopicKind.ENTITY, TopicKind.DOMAIN, TopicKind.REGION
TAGS: dict[str, list[TopicTag]] = {
    "lottery": [TopicTag(tag="lottery co", kind=E), TopicTag(tag="taiwan", kind=R)],
    "typhoon": [TopicTag(tag="cwa", kind=E), TopicTag(tag="taiwan", kind=R)],
    "local-election": [
        TopicTag(tag="kmt", kind=E),
        TopicTag(tag="dpp", kind=E),
        TopicTag(tag="election", kind=D),
        TopicTag(tag="taiwan", kind=R),
    ],
    "brazil-election": [TopicTag(tag="lula", kind=E), TopicTag(tag="election", kind=D)],
    "us-midterms": [TopicTag(tag="gop", kind=E), TopicTag(tag="election", kind=D)],
    "kmt-chair": [TopicTag(tag="kmt", kind=E)],
    # One entity tag on many topics (broad), one on two (narrow).
    "genie": [TopicTag(tag="deepmind", kind=E), TopicTag(tag="google", kind=E)],
    "gemini-app": [TopicTag(tag="deepmind", kind=E), TopicTag(tag="google", kind=E)],
    "pixel": [TopicTag(tag="google", kind=E)],
    "android": [TopicTag(tag="google", kind=E)],
    "chrome": [TopicTag(tag="google", kind=E)],
}
# Each issue's incoming order, the order with no vote.
ORDER = list(TAGS)
ENTITIES = {t: entity_tags(tags) for t, tags in TAGS.items()}


def _scores(votes: list[TopicVote], at: datetime) -> dict[str, float]:
    return score_topics(ORDER, ENTITIES, ENTITIES.values(), topic_preferences(votes, at, H))


def _ranked(votes: list[TopicVote], at: datetime) -> list[str]:
    scores = _scores(votes, at)
    return sorted(ORDER, key=lambda t: -scores[t])


def _vote(topic: str, direction: int, at: datetime = T1) -> TopicVote:
    return TopicVote(topic_id=topic, direction=direction, clicked_at=at)


def test_an_up_vote_lifts_its_topic_in_the_next_two_issues() -> None:
    votes = [_vote("local-election", 1)]
    for at in ISSUES[1:]:
        before = ORDER.index("local-election")
        assert _ranked(votes, at).index("local-election") < before


def test_a_down_vote_sinks_its_topic_in_the_next_two_issues() -> None:
    votes = [_vote("brazil-election", -1)]
    for at in ISSUES[1:]:
        before = ORDER.index("brazil-election")
        assert _ranked(votes, at).index("brazil-election") > before


def test_after_four_half_lives_a_vote_weighs_less_than_the_cutoff() -> None:
    vote = _vote("local-election", 1)

    assert vote_weight(vote, T1 + timedelta(days=3 * H), H) > RECENT_VOTE_CUTOFF
    assert vote_weight(vote, T1 + timedelta(days=4 * H), H) < RECENT_VOTE_CUTOFF
    assert kept_by_upvote(topic_preferences([vote], T1 + timedelta(days=3 * H), H)) == [
        "local-election"
    ]
    assert kept_by_upvote(topic_preferences([vote], T1 + timedelta(days=4 * H), H)) == []


def test_a_narrow_entity_tag_moves_an_unvoted_topic_more_than_a_broad_one() -> None:
    vote = _vote("genie", 1)
    weight = vote_weight(vote, ISSUES[1], H)
    scores = _scores([vote], ISSUES[1])

    # gemini-app carries the narrow `deepmind` (on 2 topics) and the broad `google`
    # (on 5); pixel carries only `google`. Each tag passes on the vote over its df.
    narrow = scores["gemini-app"] - scores["pixel"]
    assert scores["pixel"] == pytest.approx(weight / 5)
    assert narrow == pytest.approx(weight / 2)
    assert narrow > scores["pixel"] > 0


def test_with_no_votes_every_score_is_zero_and_the_order_is_unchanged() -> None:
    for at in ISSUES:
        assert set(_scores([], at).values()) == {0.0}
        assert _ranked([], at) == ORDER


def test_domain_and_region_tags_carry_no_preference() -> None:
    scores = _scores([_vote("local-election", 1)], ISSUES[1])

    # lottery shares only `taiwan` (region); us-midterms only `election` (domain).
    assert scores["lottery"] == 0.0
    assert scores["us-midterms"] == 0.0
    assert scores["kmt-chair"] > 0


def test_a_topics_own_vote_is_not_counted_again_through_its_tags() -> None:
    at = ISSUES[1]
    vote = _vote("local-election", 1)

    assert _scores([vote], at)["local-election"] == pytest.approx(vote_weight(vote, at, H))


def test_a_vote_clicked_after_the_issue_does_not_count_for_it() -> None:
    assert _scores([_vote("local-election", 1, ISSUES[2])], ISSUES[1])["local-election"] == 0.0


def test_an_up_and_a_down_on_one_topic_net_out() -> None:
    votes = [_vote("typhoon", 1), _vote("typhoon", -1, T1 + timedelta(seconds=1))]

    assert abs(_scores(votes, ISSUES[1])["typhoon"]) < 1e-4
