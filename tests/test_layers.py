"""The three layers of an issue: pebble, sand and water, decided by lane and member count."""

import pytest

from cyris.domain.layers import layer_by_lane
from cyris.domain.models import DigestContent, DigestItem, DigestSection, NewsParagraph

pytestmark = pytest.mark.unit


def _item(slug: str, *, score: float | None = None, **extra) -> DigestItem:
    fields = {
        "title": f"Title {slug}",
        "summary": f"Summary {slug}",
        "sources": [f"Source {slug}"],
        "urls": [f"https://example.test/{slug}"],
        "score": score,
    }
    return DigestItem(**(fields | extra))


def _cluster(slug: str, n: int, **extra) -> DigestSection:
    """A cluster as `cluster_news` writes it: one merged item and `n` members."""
    members = [
        _item(f"{slug}-{i}", summary=f"Excerpt {slug}-{i}", passed_through=True) for i in range(n)
    ]
    merged = DigestItem(
        title=f"Heading {slug}",
        summary=f"Cluster summary {slug}",
        sources=[s for m in members for s in m.sources],
        urls=[u for m in members for u in m.urls],
    )
    fields = {
        "heading": f"Heading {slug}",
        "items": [merged],
        "members": members,
        "tags": [f"tag-{slug}"],
        "story_id": f"story-{slug}",
    }
    return DigestSection(**(fields | extra))


def _content(**sections) -> DigestContent:
    return DigestContent(
        date="2026-10-29",
        period="morning",
        sources_processed=1,
        articles_received=0,
        articles_included=0,
        **sections,
    )


def _urls(entries) -> list[str]:
    return [u for e in entries for u in e.item.urls]


def test_every_read_and_follow_article_is_its_own_pebble_in_todays_order() -> None:
    group = DigestSection(
        heading="A group",
        summary="One summary for both.",
        items=[_item("lead-1", score=9.0), _item("lead-2", score=8.0)],
    )
    content = _content(
        featured_articles=[group, DigestSection(heading="f", items=[_item("feature")])],
        attention_sections=[
            DigestSection(heading="a", items=[_item("low", score=2.0)]),
            DigestSection(heading="b", items=[_item("lower", score=1.0), _item("high", score=3.0)]),
        ],
        fan_sections=[DigestSection(heading="Fan", items=[_item("fan-1"), _item("fan-2")])],
    )

    layers = layer_by_lane(content)

    # The Top story's group splits into one pebble per article; low scores stay pebbles,
    # highest first as their rows were; the follow lane closes the layer.
    assert _urls(layers.pebbles) == [
        f"https://example.test/{s}"
        for s in ("lead-1", "lead-2", "feature", "high", "low", "lower", "fan-1", "fan-2")
    ]
    assert layers.sand == () and layers.water == ()


def test_a_pebble_is_untranslated_when_its_text_is_the_articles_own() -> None:
    content = _content(
        featured_articles=[
            DigestSection(heading="f", items=[_item("model"), _item("x", passed_through=True)])
        ],
        attention_sections=[DigestSection(heading="a", items=[_item("radar")])],
        fan_sections=[DigestSection(heading="Fan", items=[_item("fan")])],
    )

    assert [p.untranslated for p in layer_by_lane(content).pebbles] == [False, True, True, True]


def test_a_legacy_issue_with_features_in_thematic_summaries_still_makes_pebbles() -> None:
    content = _content(thematic_summaries=[DigestSection(heading="t", items=[_item("old")])])

    assert _urls(layer_by_lane(content).pebbles) == ["https://example.test/old"]


def test_a_cluster_of_two_or_more_articles_is_sand_with_one_vote_over_every_member() -> None:
    content = _content(news_clusters=[_cluster("big", 3)])

    (sand,) = layer_by_lane(content).sand

    assert sand.heading == "Heading big"
    assert [m.title for m in sand.members] == ["Title big-0", "Title big-1", "Title big-2"]
    assert sand.vote.urls == [f"https://example.test/big-{i}" for i in range(3)]
    assert sand.story_id == "story-big"
    assert sand.tags == ("tag-big",)
    assert layer_by_lane(content).water == ()


def test_the_sand_line_stands_in_with_the_first_members_excerpt() -> None:
    (sand,) = layer_by_lane(_content(news_clusters=[_cluster("big", 2)])).sand

    assert sand.line == "Excerpt big-0"
    assert sand.line_untranslated


def test_a_cluster_of_one_article_is_a_water_row_keeping_its_story_and_tags() -> None:
    content = _content(news_clusters=[_cluster("one", 1)])

    layers = layer_by_lane(content)

    (water,) = layers.water
    assert layers.sand == ()
    assert water.item.urls == ["https://example.test/one-0"]
    assert water.story_id == "story-one"
    assert water.tags == ("tag-one",)


def test_a_filtered_headline_is_a_water_row_after_the_single_clusters() -> None:
    headline = _item("wire", passed_through=True, tags=["t"])
    content = _content(news_clusters=[_cluster("one", 1)], filtered_headlines=[headline])

    water = layer_by_lane(content).water

    assert _urls(water) == ["https://example.test/one-0", "https://example.test/wire"]
    assert (water[1].story_id, water[1].tags, water[1].untranslated) == (None, ("t",), True)
    assert not water[0].untranslated


def test_sand_and_water_keep_the_order_the_content_holds() -> None:
    content = _content(
        news_clusters=[_cluster("b", 2), _cluster("x", 1), _cluster("a", 2)],
        filtered_headlines=[_item("w2"), _item("w1")],
    )

    layers = layer_by_lane(content)

    assert [s.heading for s in layers.sand] == ["Heading b", "Heading a"]
    assert _urls(layers.water) == [
        "https://example.test/x-0",
        "https://example.test/w2",
        "https://example.test/w1",
    ]


def test_a_legacy_cluster_with_no_members_takes_them_from_its_urls() -> None:
    legacy = _cluster("old", 2, members=[])
    single = _cluster("lone", 1, members=[])

    layers = layer_by_lane(_content(news_clusters=[legacy, single]))

    (sand,) = layers.sand
    # Its stored summary is the only line it has; each member is labelled by its source.
    assert (sand.line, sand.line_untranslated) == ("Cluster summary old", False)
    assert [(m.title, m.urls) for m in sand.members] == [
        ("Source old-0", ["https://example.test/old-0"]),
        ("Source old-1", ["https://example.test/old-1"]),
    ]
    assert sand.vote.urls == legacy.items[0].urls
    assert _urls(layers.water) == ["https://example.test/lone-0"]


def test_a_legacy_cluster_with_reference_links_labels_its_members_by_host() -> None:
    merged = DigestItem(
        title="Heading ref",
        summary="s",
        sources=["Letter", "Wire"],
        urls=["newsletter:1", "https://wire.example.test/a"],
        ref_urls=["https://ref.example.test/1", "https://wire.example.test/a"],
    )
    content = _content(news_clusters=[DigestSection(heading="Heading ref", items=[merged])])

    (sand,) = layer_by_lane(content).sand

    assert [(m.title, m.link) for m in sand.members] == [
        ("ref.example.test", "https://ref.example.test/1"),
        ("wire.example.test", "https://wire.example.test/a"),
    ]


def test_a_legacy_cluster_counts_its_articles_not_its_reference_links() -> None:
    merged = DigestItem(
        title="One letter",
        summary="s",
        sources=["Letter"],
        urls=["newsletter:1"],
        ref_urls=["https://ref.example.test/1", "https://ref.example.test/2"],
    )
    content = _content(news_clusters=[DigestSection(heading="One letter", items=[merged])])

    layers = layer_by_lane(content)

    assert layers.sand == ()
    assert [w.item for w in layers.water] == [merged]


def test_a_legacy_paragraph_unit_of_two_or_more_urls_is_sand_and_one_url_is_water() -> None:
    big = DigestItem(
        title="Busy unit",
        summary="",
        sources=["A", "B"],
        urls=["https://example.test/a", "https://example.test/b"],
    )
    small = DigestItem(title="Quiet unit", summary="", sources=["C"], urls=["https://c.test/"])
    late = DigestItem(title="Late unit", summary="", sources=["D"], urls=["https://d.test/"])
    content = _content(
        news_paragraphs=[
            NewsParagraph(text="Two events.", units=[big, small]),
            NewsParagraph(text="Its own text.", units=[late], passed_through=True),
        ]
    )

    layers = layer_by_lane(content)

    (sand,) = layers.sand
    assert (sand.heading, sand.vote.urls) == ("Busy unit", big.urls)
    assert [m.title for m in sand.members] == ["A", "B"]
    # A paragraph's text rides on its first unit, so none of it is lost.
    assert (sand.line, sand.line_untranslated) == ("Two events.", False)
    assert [(w.item.title, w.item.summary, w.untranslated) for w in layers.water] == [
        ("Quiet unit", "", False),
        ("Late unit", "Its own text.", True),
    ]


def test_tracking_passes_through_untouched() -> None:
    topic = DigestSection(heading="Tracked", items=[_item("hit")])

    assert layer_by_lane(_content(tracked_topics=[topic])).tracking == (topic,)
