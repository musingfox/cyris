"""The reader's layout of an issue: one read-only view over the stored content, by area.

`layout_by_area` places each card, group, cluster, paragraph and row under its area. The mail,
its text and Discord lay an issue out by it; the page lays it out in three layers instead
(`domain.layers`, ADR-0023).
"""

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal

from cyris.domain.areas import fold_name
from cyris.domain.models import DigestContent, DigestItem, DigestSection, NewsParagraph
from cyris.domain.selection import _by_score


@dataclass(frozen=True)
class LowerRow:
    item: DigestItem
    kind: Literal["radar", "wire"]

    @property
    def untranslated(self) -> bool:
        return self.kind == "radar" or self.item.passed_through


@dataclass(frozen=True)
class AreaSection:
    # "named": `label` is the reader's area. "unplaced": entries no active area claims
    # (no area, or one renamed or dismissed since the issue was stamped); `label` is None
    # and the surfaces title it. "unnamed": the issue has no area order at all.
    label: str | None
    role: Literal["named", "unplaced", "unnamed"]
    features: tuple[DigestSection, ...] = ()
    following: tuple[DigestSection, ...] = ()
    clusters: tuple[DigestSection, ...] = ()
    paragraphs: tuple[NewsParagraph, ...] = ()
    rows: tuple[LowerRow, ...] = ()


@dataclass(frozen=True)
class AreaLayout:
    headline: DigestSection | None
    tracking: tuple[DigestSection, ...]
    sections: tuple[AreaSection, ...]


@dataclass
class _Bucket:
    features: list[DigestSection] = field(default_factory=list)
    following: list[DigestSection] = field(default_factory=list)
    clusters: list[DigestSection] = field(default_factory=list)
    paragraphs: list[NewsParagraph] = field(default_factory=list)
    radar: list[DigestItem] = field(default_factory=list)
    headlines: list[DigestItem] = field(default_factory=list)


def layout_by_area(content: DigestContent) -> AreaLayout:
    index = {fold_name(name): i for i, name in enumerate(content.area_order)}
    unplaced = len(content.area_order)

    def place(area: str | None) -> int:
        if not content.area_order:
            return 0
        if area is None or fold_name(area) not in index:
            return unplaced
        return index[fold_name(area)]

    def label(key: int) -> str | None:
        if not content.area_order or key == unplaced:
            return None
        return content.area_order[key]

    def role(key: int) -> Literal["named", "unplaced", "unnamed"]:
        if not content.area_order:
            return "unnamed"
        return "named" if key < unplaced else "unplaced"

    buckets: dict[int, _Bucket] = defaultdict(_Bucket)

    def place_section(section: DigestSection, into: str) -> None:
        if not section.items:
            return
        getattr(buckets[place(section.items[0].area)], into).append(section)

    pool = [*content.featured_articles, *content.thematic_summaries]
    headline = pool[0] if pool else None
    for section in pool[1:]:
        place_section(section, "features")
    for section in content.fan_sections:
        place_section(section, "following")
    for section in content.news_clusters:
        place_section(section, "clusters")
    for paragraph in content.news_paragraphs:
        if paragraph.units:
            buckets[place(paragraph.units[0].area)].paragraphs.append(paragraph)
    for section in content.attention_sections:
        for item in section.items:
            buckets[place(item.area)].radar.append(item)
    for item in content.filtered_headlines:
        buckets[place(item.area)].headlines.append(item)

    sections = []
    for key in sorted(buckets):
        bucket = buckets[key]
        sections.append(
            AreaSection(
                label=label(key),
                role=role(key),
                features=tuple(bucket.features),
                following=tuple(bucket.following),
                clusters=tuple(bucket.clusters),
                paragraphs=tuple(bucket.paragraphs),
                rows=(
                    *(
                        LowerRow(item, "radar")
                        for item in sorted(bucket.radar, key=_by_score, reverse=True)
                    ),
                    *(LowerRow(item, "wire") for item in bucket.headlines),
                ),
            )
        )
    return AreaLayout(
        headline=headline,
        tracking=tuple(content.tracked_topics),
        sections=tuple(sections),
    )
