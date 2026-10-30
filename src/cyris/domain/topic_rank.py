"""Option I: how the reader's topic votes order an issue (ADR-0023).

A topic's score is its own votes, each halving every `half_life_days`, plus the
preference of its entity tags. A tag's preference is the decayed votes of the other
topics carrying it, divided by df, the number of topics carrying it among those an
issue's population holds; the population is fixed at the issue's own time, so the
discount does not drift with the reader's clock. Domain and region tags carry nothing.
Embeddings take no part. Ported from the preference POC, vault
`pm/cyris/docs/digest-pebble-sand-water/preference-poc-2026-10-29/review/model_revised.py`,
without its similarity term and its 0.5 tag weight, which ADR-0023 does not adopt.
"""

from collections.abc import Iterable, Mapping
from datetime import datetime

from cyris.domain.models import TopicKind, TopicTag, TopicVote

# The owner ruled on 2026-10-30 that a topic the reader voted up stays on the
# recent-topic list after `L` days, while its net vote is still worth more than this:
# 0.1 is about four half-lives (0.5 ** 4 ≈ 0.06 is below it, 0.5 ** 3 ≈ 0.13 above).
RECENT_VOTE_CUTOFF = 0.1

_SECONDS_PER_DAY = 86_400


def vote_weight(vote: TopicVote, at: datetime, half_life_days: float) -> float:
    """The vote's signed weight at `at`; a vote clicked later counts nothing yet."""
    if vote.clicked_at > at:
        return 0.0
    age_days = (at - vote.clicked_at).total_seconds() / _SECONDS_PER_DAY
    return vote.direction * 0.5 ** (age_days / half_life_days)


def topic_preferences(
    votes: Iterable[TopicVote], at: datetime, half_life_days: float
) -> dict[str, float]:
    """Each voted topic's decayed net vote at `at`."""
    prefs: dict[str, float] = {}
    for vote in votes:
        if vote.clicked_at <= at:
            prefs[vote.topic_id] = prefs.get(vote.topic_id, 0.0) + vote_weight(
                vote, at, half_life_days
            )
    return prefs


def entity_tags(tags: Iterable[TopicTag]) -> frozenset[str]:
    return frozenset(t.tag for t in tags if t.kind == TopicKind.ENTITY)


def score_topics(
    topic_ids: Iterable[str],
    entities: Mapping[str, frozenset[str]],
    population: Iterable[frozenset[str]],
    prefs: Mapping[str, float],
) -> dict[str, float]:
    """Each topic's score: its own preference plus its entity tags' preference.

    `entities` names the entity tags of every topic asked about and every voted one;
    `population` is the entity tag set of each topic the discount counts over.
    """
    df: dict[str, int] = {}
    for tags in population:
        for tag in tags:
            df[tag] = df.get(tag, 0) + 1
    scores = {}
    for topic in topic_ids:
        tag_term = 0.0
        for tag in entities.get(topic, frozenset()):
            carried = sum(
                p for v, p in prefs.items() if v != topic and tag in entities.get(v, frozenset())
            )
            tag_term += carried / max(df.get(tag, 0), 1)
        scores[topic] = prefs.get(topic, 0.0) + tag_term
    return scores


def kept_by_upvote(prefs: Mapping[str, float]) -> list[str]:
    """Voted topics still worth more than `RECENT_VOTE_CUTOFF` upward."""
    return [topic for topic, p in prefs.items() if p > RECENT_VOTE_CUTOFF]
