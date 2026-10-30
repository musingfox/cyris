"""Use case: read the reader's topic votes and score an issue's topics by option I (ADR-0023)."""

from datetime import datetime, timedelta

from cyris.domain.models import TopicRecord, TopicSummary
from cyris.domain.topic_rank import entity_tags, kept_by_upvote, score_topics, topic_preferences
from cyris.service_layer.ports import TopicRepository


def score_issue_topics(
    topics: TopicRepository,
    issue: list[TopicRecord],
    at: datetime,
    *,
    half_life_days: int,
    lifetime_days: int,
) -> dict[str, float]:
    """Each article URL of the issue, scored as its topic, at the issue's time `at`.

    The discount's population is the topics last seen in the `lifetime_days` before
    `at` and this issue's own, which are saved only after the order is set.
    """
    prefs = topic_preferences(topics.topic_votes(), at, half_life_days)
    population = {
        t.id: entity_tags(t.tags) for t in topics.recent_topics(at - timedelta(lifetime_days), at)
    }
    population |= {t.id: entity_tags(t.tags) for t in issue}
    voted = [i for i in prefs if i not in population]
    entities = population | {t.id: entity_tags(t.tags) for t in topics.topics_by_ids(voted)}
    scores = score_topics([t.id for t in issue], entities, population.values(), prefs)
    return {url: scores[t.id] for t in issue for url in t.urls}


def recent_topic_list(
    topics: TopicRepository, at: datetime, *, half_life_days: int, lifetime_days: int
) -> list[TopicSummary]:
    """The topics an article may join: last seen within `lifetime_days`, plus each
    one the reader voted up whose vote is still above `RECENT_VOTE_CUTOFF`.

    For vault card `filter-assigns-topics`, which hands this list to the model.
    """
    recent = topics.recent_topics(at - timedelta(lifetime_days), at)
    seen = {t.id for t in recent}
    upvoted = kept_by_upvote(topic_preferences(topics.topic_votes(), at, half_life_days))
    return recent + topics.topics_by_ids([i for i in upvoted if i not in seen])
