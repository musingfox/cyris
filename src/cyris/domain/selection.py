"""Article selection with priority fill for digest output."""

import logging

from cyris.domain.models import Article, DigestContent, DigestSection

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


def layer_by_score(
    content: DigestContent, featured_threshold: float = 70, max_featured: int = 5
) -> DigestContent:
    """Extract high-scoring sections from thematic_summaries to featured_articles.

    Scans thematic_summaries sections and moves sections with at least one item
    scoring >= featured_threshold to the featured_articles list. Featured sections
    are sorted by max item score (descending) and capped at max_featured.

    Args:
        content: Digest content to layer.
        featured_threshold: Minimum score to qualify for featured.
        max_featured: Maximum number of featured sections.

    Returns:
        Modified DigestContent with featured sections extracted.
    """
    featured = []
    remaining_summaries = []

    # Scan each section and compute max score
    for section in content.thematic_summaries:
        max_score = 0.0
        has_score = False

        for item in section.items:
            if item.score is not None:
                has_score = True
                if item.score > max_score:
                    max_score = item.score

        # Check if section qualifies as featured
        if has_score and max_score >= featured_threshold:
            featured.append((section, max_score))
        else:
            remaining_summaries.append(section)

    # Sort featured by max score descending and cap
    featured.sort(key=lambda x: x[1], reverse=True)
    featured_sections = [s for s, _ in featured[:max_featured]]

    # If we capped, put the overflow back in remaining summaries
    if len(featured) > max_featured:
        overflow = [s for s, _ in featured[max_featured:]]
        remaining_summaries = overflow + remaining_summaries

    logger.info(
        "Layered %d featured sections (threshold=%.1f), %d remaining in thematic",
        len(featured_sections),
        featured_threshold,
        len(remaining_summaries),
    )

    return content.model_copy(
        update={
            "featured_articles": featured_sections,
            "thematic_summaries": remaining_summaries,
        }
    )


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


def select_digest_articles(content: DigestContent, max_items: int = 15) -> DigestContent:
    """Apply priority fill to limit total digest articles.

    Priority order: news_clusters → thematic_summaries → attention_sections → filtered_headlines.
    Each category fills completely before the next gets remaining slots.

    Args:
        content: Full processed digest content.
        max_items: Total article cap.

    Returns:
        New DigestContent with limited items.
    """
    remaining = max_items

    # Priority 1: news clusters
    news_count = _count_section_items(content.news_clusters)
    if news_count <= remaining:
        selected_clusters = list(content.news_clusters)
        remaining -= news_count
    else:
        selected_clusters = _truncate_sections(content.news_clusters, remaining)
        remaining = 0

    # Priority 2: thematic summaries
    summary_count = _count_section_items(content.thematic_summaries)
    if remaining == 0:
        selected_summaries = []
    elif summary_count <= remaining:
        selected_summaries = list(content.thematic_summaries)
        remaining -= summary_count
    else:
        selected_summaries = _truncate_sections(content.thematic_summaries, remaining)
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
        _count_section_items(selected_clusters)
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
            "news_clusters": selected_clusters,
            "thematic_summaries": selected_summaries,
            "attention_sections": selected_attention,
            "filtered_headlines": selected_headlines,
            "articles_included": total_selected,
        }
    )
