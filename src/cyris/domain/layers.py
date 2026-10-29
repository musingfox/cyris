"""The reader's layout of an issue in three layers: pebble, sand and water (ADR-0023).

A layer is decided by lane and by the member count in this issue alone, never by a score:
every `read` or `follow` article is a pebble, a news topic of two or more articles is sand,
and a topic of one article, or a filtered headline, is water. A score or the vote preference
orders items inside a layer, and that order is the content's own; this view keeps it.
"""

from dataclasses import dataclass
from urllib.parse import urlsplit

from cyris.domain.models import DigestContent, DigestItem, DigestSection
from cyris.domain.selection import _by_score


@dataclass(frozen=True)
class Pebble:
    item: DigestItem
    # The card's text is the article's own, not the output language's.
    untranslated: bool


@dataclass(frozen=True)
class Sand:
    heading: str
    line: str
    line_untranslated: bool
    members: tuple[DigestItem, ...]
    # The one item whose URLs are every member's: the topic's single vote.
    vote: DigestItem
    tags: tuple[str, ...]
    story_id: str | None


@dataclass(frozen=True)
class Water:
    item: DigestItem
    tags: tuple[str, ...]
    story_id: str | None
    untranslated: bool


@dataclass(frozen=True)
class Layers:
    pebbles: tuple[Pebble, ...]
    sand: tuple[Sand, ...]
    water: tuple[Water, ...]
    tracking: tuple[DigestSection, ...]


def layer_by_lane(content: DigestContent) -> Layers:
    pebbles = [
        Pebble(item, item.passed_through)
        for section in (*content.featured_articles, *content.thematic_summaries)
        for item in section.items
    ]
    radar = [item for section in content.attention_sections for item in section.items]
    pebbles += [Pebble(item, True) for item in sorted(radar, key=_by_score, reverse=True)]
    pebbles += [Pebble(item, True) for section in content.fan_sections for item in section.items]

    sand: list[Sand] = []
    water: list[Water] = []
    for section in content.news_clusters:
        if not section.items:
            continue
        vote = section.items[0]
        tags = tuple(section.tags)
        if len(section.members or vote.urls) < 2:
            water.append(Water(vote, tags, section.story_id, vote.passed_through))
            continue
        # Stored before members: the cluster's own summary is the only line it has.
        first = section.members[0] if section.members else vote
        sand.append(
            Sand(
                heading=section.heading,
                line=first.summary,
                line_untranslated=first.passed_through,
                members=tuple(section.members or _members_of(vote)),
                vote=vote,
                tags=tags,
                story_id=section.story_id,
            )
        )
    water += [
        Water(item, tuple(item.tags), None, item.passed_through)
        for item in content.filtered_headlines
    ]

    # An issue stored with news paragraphs: each cited unit takes the layer its URL count
    # gives it, and the paragraph's text rides on its first unit, so none of it is lost.
    for paragraph in content.news_paragraphs:
        for n, unit in enumerate(paragraph.units):
            text = paragraph.text if n == 0 else unit.summary
            if len(unit.urls) >= 2:
                sand.append(
                    Sand(
                        heading=unit.title,
                        line=text,
                        line_untranslated=paragraph.passed_through,
                        members=tuple(_members_of(unit)),
                        vote=unit,
                        tags=tuple(unit.tags),
                        story_id=None,
                    )
                )
            else:
                item = unit.model_copy(update={"summary": text})
                water.append(Water(item, tuple(unit.tags), None, paragraph.passed_through))

    return Layers(
        pebbles=tuple(pebbles),
        sand=tuple(sand),
        water=tuple(water),
        tracking=tuple(content.tracked_topics),
    )


def _members_of(merged: DigestItem) -> list[DigestItem]:
    """A merged item's articles when no member was stored: one per link, titled by its label.

    Reference links take their hostname, as the stored page labelled them; otherwise each
    store URL takes the source beside it, its lockstep partner in the merged item.
    """
    if merged.ref_urls:
        return [_linked(urlsplit(url).hostname or url, url) for url in merged.ref_urls]
    return [
        _linked(merged.sources[i] if i < len(merged.sources) else url, url)
        for i, url in enumerate(merged.urls)
    ]


def _linked(label: str, url: str) -> DigestItem:
    return DigestItem(title=label, summary="", sources=[], urls=[url])
