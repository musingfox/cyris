"""Article selection with priority fill for digest output."""

import logging

from cyris.domain.models import Article, DigestContent, DigestItem, DigestSection

logger = logging.getLogger(__name__)


def split_summarize_tier_by_score(
    articles: list[Article],
    article_scores: dict[str, float] | None,
    threshold: int,
) -> tuple[list[Article], list[Article]]:
    """Split summarize-tier articles by score threshold.

    Articles with score >= threshold go to high_score (AI summarization).
    Articles with score < threshold go to low_score (attention sections).
    Articles without scores default to high_score.

    Args:
        articles: Summarize-tier articles to split.
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


def layer_by_score(
    content: DigestContent, *, featured_threshold: float, max_featured: int
) -> DigestContent:
    """Lay the summarize tier out as the Top story and Features.

    `featured_articles[0]` is the Top story. It is a group, with its group summary,
    when two or more articles of one summarized group score at least
    `featured_threshold`; of several such groups, the one holding the best article
    leads, and it keeps every article of its group, since the group summary covers
    them all. With no such group it is the highest-scoring article. Every other article
    becomes a one-article section, highest score first, and at most `max_featured` of
    them are kept: the rest leave the issue. `thematic_summaries` ends empty.

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
    featured = [lead, *(_single(i) for i in singles[:max_featured])] if lead else []

    logger.info(
        "Layered a %d-article Top story and %d Features (threshold=%.1f); %d past max_featured",
        len(lead.items) if lead else 0,
        max(0, len(featured) - 1),
        featured_threshold,
        max(0, len(singles) - max_featured),
    )

    return content.model_copy(update={"featured_articles": featured, "thematic_summaries": []})


def _cap_featured(featured: list[DigestSection], max_items: int) -> list[DigestSection]:
    """The Top story whole, or its best article in its slot, then Features by score."""
    if not featured or max_items <= 0:
        return []
    lead, *features = featured
    if len(lead.items) > max_items:
        lead = _single(max(lead.items, key=_by_score))
    features = sorted(features, key=lambda s: max(_by_score(i) for i in s.items), reverse=True)
    return [lead, *_truncate_sections(features, max_items - len(lead.items))]


def _count_section_items(sections: list[DigestSection]) -> int:
    return sum(len(s.items) for s in sections)


def _truncate_sections(sections: list[DigestSection], max_items: int) -> list[DigestSection]:
    """Truncate a list of sections to fit within max_items total."""
    result = []
    remaining = max_items
    for section in sections:
        if remaining <= 0:
            break
        if len(section.items) <= remaining:
            result.append(section)
            remaining -= len(section.items)
        else:
            truncated = section.model_copy(update={"items": section.items[:remaining]})
            result.append(truncated)
            remaining = 0
    return result


def select_digest_articles(content: DigestContent, *, max_items: int) -> DigestContent:
    """Apply priority fill to limit total digest articles.

    Priority order: featured_articles → thematic_summaries → news_clusters →
    attention_sections → filtered_headlines. Each category fills completely before
    the next gets remaining slots. Featured articles are laid out first
    (`layer_by_score`), so the cap never splits the Top story's group.
    Summaries lead: the knowledge channel (summarize tier, inherently small) must
    never be crowded out of the digest by a heavy news day.

    Args:
        content: Full processed digest content.
        max_items: Total article cap.

    Returns:
        New DigestContent with limited items.
    """
    selected_featured = _cap_featured(content.featured_articles, max_items)
    remaining = max_items - _count_section_items(selected_featured)

    # Priority 1: thematic summaries
    summary_count = _count_section_items(content.thematic_summaries)
    if summary_count <= remaining:
        selected_summaries = list(content.thematic_summaries)
        remaining -= summary_count
    else:
        selected_summaries = _truncate_sections(content.thematic_summaries, remaining)
        remaining = 0

    # Priority 2: news clusters
    news_count = _count_section_items(content.news_clusters)
    if remaining == 0:
        selected_clusters = []
    elif news_count <= remaining:
        selected_clusters = list(content.news_clusters)
        remaining -= news_count
    else:
        selected_clusters = _truncate_sections(content.news_clusters, remaining)
        remaining = 0

    # Priority 3: attention sections
    attention_count = _count_section_items(content.attention_sections)
    if remaining == 0:
        selected_attention = []
    elif attention_count <= remaining:
        selected_attention = list(content.attention_sections)
        remaining -= attention_count
    else:
        selected_attention = _truncate_sections(content.attention_sections, remaining)
        remaining = 0

    # Priority 4: filtered headlines
    if remaining == 0:
        selected_headlines = []
    else:
        selected_headlines = content.filtered_headlines[:remaining]
        remaining -= len(selected_headlines)

    total_selected = (
        _count_section_items(selected_featured)
        + _count_section_items(selected_clusters)
        + _count_section_items(selected_summaries)
        + _count_section_items(selected_attention)
        + len(selected_headlines)
        + _count_section_items(content.fan_sections)  # fan passthrough, never capped
    )

    logger.info(
        "Selected %d/%d articles: %d clusters, %d summaries, %d attention, %d headlines",
        total_selected,
        content.articles_included,
        _count_section_items(selected_clusters),
        _count_section_items(selected_summaries),
        _count_section_items(selected_attention),
        len(selected_headlines),
    )

    return content.model_copy(
        update={
            "featured_articles": selected_featured,
            "news_clusters": selected_clusters,
            "thematic_summaries": selected_summaries,
            "attention_sections": selected_attention,
            "filtered_headlines": selected_headlines,
            "articles_included": total_selected,
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
    return items


def count_dead_links(content: DigestContent) -> int:
    """Count digest items with no clickable http(s) URL in urls or ref_urls."""
    return sum(1 for item in _iter_digest_items(content) if item.link is None)


def digest_urls(content: DigestContent) -> set[str]:
    """Every article URL the digest shows, in any section."""
    return {url for item in _iter_digest_items(content) for url in item.urls}
