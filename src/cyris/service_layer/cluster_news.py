"""News clustering for related articles."""

import logging

from cyris.domain.models import Article, DigestItem, DigestSection, UsageStats
from cyris.service_layer.ports import LLMClient, complete_json
from cyris.service_layer.prompts import (
    DEFAULT_LANGUAGE,
    build_news_cluster_prompt,
    build_news_cluster_system_prompt,
)

logger = logging.getLogger(__name__)


def filter_news(articles: list[Article]) -> tuple[list[Article], list[Article]]:
    """Split articles into news and non-news based on source tags.

    Args:
        articles: Articles to filter.

    Returns:
        Tuple of (news_articles, non_news_articles).
        News articles have "news" in source_tags (strict lowercase match).
    """
    news_articles = []
    non_news_articles = []

    for article in articles:
        if "news" in article.source_tags:
            news_articles.append(article)
        else:
            non_news_articles.append(article)

    return news_articles, non_news_articles


async def cluster_news(
    articles: list[Article],
    llm: LLMClient | None,
    usage: UsageStats | None = None,
    article_scores: dict[str, float] | None = None,
    output_language: str = DEFAULT_LANGUAGE,
    style_prompt: str = "",
) -> tuple[list[DigestSection], list[Article]]:
    """Cluster related news articles using the LLM.

    Args:
        articles: News articles to cluster.
        llm: LLM client.
        usage: Optional UsageStats to accumulate API usage.

    Returns:
        Tuple of (news_clusters, unclustered_articles).
        - news_clusters: DigestSections with heading/summary/items
        - unclustered_articles: Articles that couldn't be clustered
    """
    if not articles:
        return [], []
    if llm is None:
        return [], articles  # no clustering without an LLM; leave all unclustered

    try:
        user_prompt = build_news_cluster_prompt(articles)

        result = await complete_json(
            llm,
            user_prompt,
            system=build_news_cluster_system_prompt(output_language, style_prompt),
            temperature=1.0,
            usage=usage,
        )

        clusters = result.get("clusters", [])
        unclustered_ids = set(result.get("unclustered_ids", []))

        # Build article lookup
        article_map = {a.id: a for a in articles}

        # Build DigestSections
        digest_sections = []
        for cluster in clusters:
            heading = cluster["heading"]
            summary = cluster["summary"]
            article_ids = cluster["article_ids"]

            # Build DigestItems from cluster articles
            items = []
            sources = []
            urls = []
            ref_urls = []
            scores = []

            for aid in article_ids:
                if aid in article_map:
                    a = article_map[aid]
                    sources.append(a.source_name)
                    urls.append(a.url)
                    if a.ref_urls:
                        if not ref_urls:
                            ref_urls = [*urls[:-1], *a.ref_urls]
                        else:
                            ref_urls.extend(a.ref_urls)
                    elif ref_urls:
                        ref_urls.append(a.url)
                    # Mark as clustered
                    unclustered_ids.discard(aid)
                    # Collect score if available
                    if article_scores and a.url in article_scores:
                        scores.append(article_scores[a.url])

            if sources:
                # Use max score from clustered articles
                max_score = max(scores) if scores else None
                # Single DigestItem per cluster with combined metadata
                items.append(
                    DigestItem(
                        title=heading,
                        summary=summary,
                        sources=sources,
                        urls=urls,
                        score=max_score,
                        ref_urls=ref_urls,
                    )
                )

                digest_sections.append(
                    DigestSection(
                        heading=heading,
                        items=items,
                    )
                )

        # Build unclustered articles list
        unclustered_articles = [article_map[aid] for aid in unclustered_ids if aid in article_map]

        logger.info(
            "News clustering: %d clusters created, %d articles unclustered",
            len(digest_sections),
            len(unclustered_articles),
        )

        return digest_sections, unclustered_articles

    except Exception:
        logger.warning(
            "News clustering failed, returning all articles as unclustered", exc_info=True
        )
        return [], articles
