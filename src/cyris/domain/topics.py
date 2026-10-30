"""The topics of one issue: every item the page shows is one topic (ADR-0023).

STAND-IN until vault card `filter-assigns-topics` lands: a topic is this issue's item and
nothing more. A news cluster (sand, or water when it holds one article) is one topic, and
every other item (a pebble, a kept headline) is a topic of its own article. The id is
derived from the issue and its member URLs, so it is fresh every period and never carries
a vote into the next issue; a cluster's id is its `story_id`, which the page already names.
"""

import hashlib

from cyris.domain.layers import layer_by_lane
from cyris.domain.models import DigestContent, TopicKind, TopicRecord, TopicTag
from cyris.domain.tags import normalize_tags

# STAND-IN: today's tags carry no kind, so every one is stored as a domain tag, which
# shows on the page and carries no preference. Calling them entities would let a broad
# tag (台灣, AI) carry a vote to unrelated stories, the leak ADR-0023 rejected.
STAND_IN_TAG_KIND = TopicKind.DOMAIN


def topic_id(date: str, period: str, urls: list[str]) -> str:
    """The same member set in the same issue yields the same id, like a story id."""
    membership = hashlib.sha1("\n".join(sorted(urls)).encode("utf-8")).hexdigest()[:8]
    return f"{date}-{period}-{membership}"


def stand_in_topics(content: DigestContent) -> list[TopicRecord]:
    """One topic per page item, in page order; an article lands in one topic only."""
    layers = layer_by_lane(content)
    entries: list[tuple[str | None, str, list[str], list[str]]] = [
        (None, p.item.title, p.item.urls, p.item.tags) for p in layers.pebbles
    ]
    entries += [(s.story_id, s.heading, s.vote.urls, list(s.tags)) for s in layers.sand]
    entries += [(w.story_id, w.item.title, w.item.urls, list(w.tags)) for w in layers.water]

    topics: list[TopicRecord] = []
    seen: set[str] = set()
    for story_id, name, urls, tags in entries:
        fresh = [u for u in dict.fromkeys(urls) if u not in seen]
        if not fresh:
            continue
        seen.update(fresh)
        topics.append(
            TopicRecord(
                id=story_id or topic_id(content.date, content.period, fresh),
                name=name,
                urls=fresh,
                tags=[TopicTag(tag=t, kind=STAND_IN_TAG_KIND) for t in normalize_tags(tags)],
            )
        )
    return topics
