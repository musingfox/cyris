"""Article selection for digest output: the read lane's layers and the order inside them."""

import logging
from collections.abc import Mapping
from itertools import zip_longest

from cyris.domain.models import Article, DigestContent, DigestItem, DigestSection

logger = logging.getLogger(__name__)


def split_read_lane_by_score(
    articles: list[Article],
    article_scores: dict[str, float] | None,
    threshold: int,
) -> tuple[list[Article], list[Article]]:
    """Split read-lane articles by score threshold.

    Articles with score >= threshold go to high_score (AI summarization).
    Articles with score < threshold go to low_score (attention sections).
    Articles without scores default to high_score.

    Args:
        articles: Read-lane articles to split.
        article_scores: Optional mapping of article URLs to scores.
        threshold: Score threshold for splitting.

    Returns:
        Tuple of (high_score_articles, low_score_articles).
    """
    if not articles:
        return ([], [])

    if article_scores is None:
        # No scores available, all go to high_score (default to AI processing)
        return (articles, [])

    high_score = []
    low_score = []

    for article in articles:
        score = article_scores.get(article.url)
        if score is None or score >= threshold:
            # No score or meets threshold → AI summarization
            high_score.append(article)
        else:
            # Below threshold → attention section
            low_score.append(article)

    return (high_score, low_score)


def _by_score(item: DigestItem) -> tuple[bool, float]:
    """Sort key, highest first under reverse=True; an unscored article trails every scored one."""
    return (item.score is not None, item.score or 0.0)


def _single(item: DigestItem) -> DigestSection:
    """A one-article section: drawn as that article, with its own summary."""
    return DigestSection(heading=item.title, items=[item])


def layer_by_score(content: DigestContent, *, featured_threshold: float) -> DigestContent:
    """Lay the read lane out as the Top story and Features.

    `featured_articles[0]` is the Top story. It is a group, with its group summary,
    when two or more articles of one summarized group score at least
    `featured_threshold`; of several such groups, the one holding the best article
    leads, and it keeps every article of its group, since the group summary covers
    them all. With no such group it is the highest-scoring article. Every other article
    becomes a one-article section, highest score first, and none is left out.
    `thematic_summaries` ends empty.

    A section with no group summary never groups: that is the degraded shape, whose
    items each carry their own excerpt.
    """
    groups = []
    for section in content.thematic_summaries:
        if section.summary is None:
            continue
        qualifying = [
            i for i in section.items if i.score is not None and i.score >= featured_threshold
        ]
        if len(qualifying) >= 2:
            groups.append(section)
    lead = max(groups, key=lambda g: max(_by_score(i) for i in g.items), default=None)

    # Keyed by URL, not identity: the model can name one article in two sections, and
    # one article is one card.
    seen = {tuple(i.urls) for i in lead.items} if lead else set()
    unique = []
    for item in (i for s in content.thematic_summaries for i in s.items):
        if tuple(item.urls) not in seen:
            seen.add(tuple(item.urls))
            unique.append(item)
    singles = sorted(unique, key=_by_score, reverse=True)
    if lead is None and singles:
        lead = _single(singles.pop(0))
    featured = [lead, *(_single(i) for i in singles)] if lead else []

    logger.info(
        "Layered a %d-article Top story and %d Features (threshold=%.1f)",
        len(lead.items) if lead else 0,
        max(0, len(featured) - 1),
        featured_threshold,
    )

    return content.model_copy(update={"featured_articles": featured, "thematic_summaries": []})


def _urls(entry: DigestItem | DigestSection) -> list[str]:
    if isinstance(entry, DigestSection):
        return [url for item in entry.items for url in item.urls]
    return entry.urls


def _preference_of(
    entry: DigestItem | DigestSection, preference: Mapping[str, float]
) -> float | None:
    """The best score among the entry's articles, or None when none of them was judged."""
    return max((preference[u] for u in _urls(entry) if u in preference), default=None)


def rank_by_preference[Ranked: (DigestItem, DigestSection)](
    items: list[Ranked], preference: Mapping[str, float]
) -> list[Ranked]:
    """Highest preference first, by a stable sort, so equal scores keep the incoming order.

    An entry none of whose articles has a score keeps its slot, and the scored
    entries are ordered among the remaining slots: it carries no signal to move it by.
    """
    scores = [_preference_of(item, preference) for item in items]
    ranked = iter(
        sorted(
            (item for item, score in zip(items, scores, strict=True) if score is not None),
            key=lambda item: -_preference_of(item, preference),
        )
    )
    return [
        item if score is None else next(ranked) for item, score in zip(items, scores, strict=True)
    ]


def preference_moves(baseline: DigestContent, ranked: DigestContent) -> dict[str, int]:
    """How many Features positions, and how many news positions, name other articles.

    The news is the clusters and the filter's headlines, each list counted on its own.
    """

    def moved(before: list, after: list) -> int:
        pairs = zip_longest((_urls(e) for e in before), (_urls(e) for e in after))
        return sum(a != b for a, b in pairs)

    return {
        "features": moved(baseline.featured_articles[1:], ranked.featured_articles[1:]),
        "news": moved(baseline.news_clusters, ranked.news_clusters)
        + moved(baseline.filtered_headlines, ranked.filtered_headlines),
    }


def order_by_preference(
    content: DigestContent, preference: Mapping[str, float] | None = None
) -> DigestContent:
    """A copy whose Features, news clusters and headlines follow the reader's vote preference.

    Nothing is left out: the issue has no output cap (ADR-0023), only the intake cap
    `max_articles_per_digest` bounds it. The Top story is never moved. The Features, by
    score, and the news clusters and headlines, in the model's order, are each ordered
    by their best article's preference; an entry with no judged article keeps its slot.
    With no preference the Features are by score and the news in the model's order.
    """
    preference = preference or {}
    featured = list(content.featured_articles)
    if featured:
        lead, *features = featured
        features.sort(key=lambda s: max(_by_score(i) for i in s.items), reverse=True)
        featured = [lead, *rank_by_preference(features, preference)]
    return content.model_copy(
        update={
            "featured_articles": featured,
            "news_clusters": rank_by_preference(list(content.news_clusters), preference),
            "filtered_headlines": rank_by_preference(list(content.filtered_headlines), preference),
        }
    )


def _iter_digest_items(content: DigestContent) -> list[DigestItem]:
    items: list[DigestItem] = []
    for sections in (
        content.featured_articles,
        content.news_clusters,
        content.thematic_summaries,
        content.attention_sections,
        content.fan_sections,
    ):
        for section in sections:
            items.extend(section.items)
    items.extend(content.filtered_headlines)
    items.extend(unit for paragraph in content.news_paragraphs for unit in paragraph.units)
    return items


def count_dead_links(content: DigestContent) -> int:
    """Count digest items with no clickable http(s) URL in urls or ref_urls."""
    # A sand fold links each member, so one live member must not hide a dead one behind
    # the merged item's link.
    folded = [s for s in content.news_clusters if len(s.members) >= 2]
    shown = [member for s in folded for member in s.members]
    hidden = {id(item) for s in folded for item in s.items}
    shown += [item for item in _iter_digest_items(content) if id(item) not in hidden]
    return sum(1 for item in shown if item.link is None)


def digest_urls(content: DigestContent) -> set[str]:
    """Every article URL the digest shows, in any section."""
    return {url for item in _iter_digest_items(content) for url in item.urls}
