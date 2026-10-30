"""The stand-in topics of an issue: one per page item, until the filter assigns topics."""

import pytest

from cyris.domain.models import DigestContent, DigestItem, DigestSection, TopicKind
from cyris.domain.topics import stand_in_topics, topic_id

pytestmark = pytest.mark.unit


def _item(slug: str, **extra) -> DigestItem:
    fields = {"title": f"T {slug}", "summary": "", "sources": ["S"], "urls": [f"u-{slug}"]}
    return DigestItem(**(fields | extra))


def _cluster(slug: str, n: int) -> DigestSection:
    members = [_item(f"{slug}{i}") for i in range(n)]
    merged = DigestItem(
        title=f"H {slug}", summary="", sources=[], urls=[u for m in members for u in m.urls]
    )
    return DigestSection(
        heading=f"H {slug}",
        items=[merged],
        members=members,
        tags=["Taiwan"],
        story_id=f"story-{slug}",
    )


def _content(**sections) -> DigestContent:
    return DigestContent(
        date="2026-10-30",
        period="morning",
        sources_processed=1,
        articles_received=0,
        articles_included=0,
        **sections,
    )


def test_every_page_item_is_one_topic_and_a_cluster_keeps_its_story_id() -> None:
    pebble = _item("p", tags=["Anthropic", "AI"])
    content = _content(
        featured_articles=[DigestSection(heading="p", items=[pebble])],
        news_clusters=[_cluster("sand", 3), _cluster("water", 1)],
        filtered_headlines=[_item("h")],
    )

    topics = stand_in_topics(content)

    assert [(t.id, t.urls) for t in topics] == [
        (topic_id("2026-10-30", "morning", ["u-p"]), ["u-p"]),
        ("story-sand", ["u-sand0", "u-sand1", "u-sand2"]),
        ("story-water", ["u-water0"]),
        (topic_id("2026-10-30", "morning", ["u-h"]), ["u-h"]),
    ]


def test_stand_in_tags_are_normalized_and_carry_no_preference() -> None:
    pebble = _item("p", tags=["Anthropic", "anthropic ", "AI"])
    content = _content(featured_articles=[DigestSection(heading="p", items=[pebble])])

    (topic,) = stand_in_topics(content)

    assert [(t.tag, t.kind) for t in topic.tags] == [
        ("anthropic", TopicKind.DOMAIN),
        ("ai", TopicKind.DOMAIN),
    ]


def test_a_topic_id_is_fresh_each_issue_and_stable_within_one() -> None:
    assert topic_id("2026-10-30", "morning", ["b", "a"]) == topic_id(
        "2026-10-30", "morning", ["a", "b"]
    )
    assert topic_id("2026-10-30", "morning", ["a"]) != topic_id("2026-10-30", "evening", ["a"])
