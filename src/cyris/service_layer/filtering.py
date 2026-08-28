"""Filter-tier processor: batch articles through Claude to extract noteworthy headlines."""

import logging

from cyris.domain.models import Article, DigestItem, UsageStats
from cyris.service_layer.degrade import headlines_from_articles
from cyris.service_layer.ports import LLMClient, complete_json
from cyris.service_layer.prompts import (
    DEFAULT_LANGUAGE,
    build_filter_prompt,
    build_filter_system_prompt,
)

logger = logging.getLogger(__name__)


async def filter_articles(
    articles: list[Article],
    llm: LLMClient | None,
    usage: UsageStats | None = None,
    article_scores: dict[str, float] | None = None,
    filter_snippet_length: int = 500,
    output_language: str = DEFAULT_LANGUAGE,
    style_prompt: str = "",
) -> list[DigestItem]:
    """Send filter-tier articles to Claude for headline extraction.

    All articles are sent in a single batch for cross-comparison.
    Expected pass rate: < 10%.

    Args:
        articles: Filter-tier articles to process.
        llm: LLM client.
        usage: Optional UsageStats to accumulate token counts.

    Returns:
        Noteworthy headlines as DigestItems.
    """
    if not articles:
        return []

    articles_to_process = articles

    # Degraded mode: no LLM → keep the articles as plain excerpts
    if llm is None:
        logger.warning("No LLM configured; filter tier falls back to excerpt headlines")
        return headlines_from_articles(articles_to_process, article_scores)

    logger.info("Filtering %d articles through the LLM", len(articles_to_process))

    user_prompt = build_filter_prompt(articles_to_process, snippet_length=filter_snippet_length)
    system_prompt = build_filter_system_prompt(output_language, style_prompt)

    try:
        data = await complete_json(
            llm, user_prompt, system=system_prompt, temperature=1.0, usage=usage
        )
    except Exception:
        logger.warning("Filter LLM call failed; falling back to excerpt headlines", exc_info=True)
        return headlines_from_articles(articles_to_process, article_scores)

    # Build lookup for source URLs
    article_map = {a.id: a for a in articles_to_process}

    items = []
    for entry in data.get("selected", []):
        required_fields = {"id", "title", "source"}
        missing_fields = (
            required_fields - entry.keys() if isinstance(entry, dict) else required_fields
        )
        if missing_fields:
            logger.warning(
                "Skipping malformed filter entry missing required fields %s: %r",
                sorted(missing_fields),
                entry,
            )
            continue
        article_id = entry["id"]
        source_article = article_map.get(article_id)
        article_url = source_article.url if source_article else ""
        score = article_scores.get(article_url) if article_scores and article_url else None
        items.append(
            DigestItem(
                title=entry["title"],
                summary=entry.get("summary", ""),
                sources=[entry["source"]],
                urls=[article_url] if article_url else [],
                ref_urls=source_article.ref_urls if source_article else [],
                score=score,
            )
        )

    logger.info(
        "Filter passed %d / %d articles (%.1f%%)",
        len(items),
        len(articles_to_process),
        len(items) / len(articles_to_process) * 100 if articles_to_process else 0,
    )
    return items
