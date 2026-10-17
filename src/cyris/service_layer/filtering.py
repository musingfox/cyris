"""Skim-lane processor: batch articles through Claude to extract noteworthy headlines."""

import logging

from cyris.domain.models import Article, DigestItem, UsageStats
from cyris.service_layer.degrade import headlines_from_articles
from cyris.service_layer.parse import echoed_position
from cyris.service_layer.ports import LLMClient, complete_json
from cyris.service_layer.prompts import (
    build_filter_prompt,
    build_filter_system_prompt,
)

logger = logging.getLogger(__name__)

# Bounding each prompt keeps the pipeline independent of any model's long-prompt pricing
# and context tier; every article is still judged, batch by batch.
BATCH_SIZE = 20


async def filter_articles(
    articles: list[Article],
    llm: LLMClient | None,
    usage: UsageStats | None = None,
    article_scores: dict[str, float] | None = None,
    *,
    filter_snippet_length: int,
    output_language: str,
    style_prompt: str,
) -> list[DigestItem]:
    """Send skim-lane articles to Claude for headline extraction, BATCH_SIZE at a time.

    Each batch is judged on its own, one call each. No pass rate is enforced:
    `FILTER_SYSTEM` asks for merit, not a percentage target. A batch whose call fails
    falls back to excerpt headlines while the other batches are still judged.

    Args:
        articles: Skim-lane articles to process.
        llm: LLM client.
        usage: Optional UsageStats to accumulate token counts.

    Returns:
        Noteworthy headlines as DigestItems.
    """
    if not articles:
        return []

    # Degraded mode: no LLM → keep the articles as plain excerpts
    if llm is None:
        logger.warning("No LLM configured; skim lane falls back to excerpt headlines")
        if usage is not None:
            usage.fell_back_to_excerpts = True
        return headlines_from_articles(articles, article_scores)

    total_batches = (len(articles) + BATCH_SIZE - 1) // BATCH_SIZE
    logger.info("Filtering %d articles through the LLM (batches: %d)", len(articles), total_batches)

    system_prompt = build_filter_system_prompt(output_language, style_prompt=style_prompt)

    items: list[DigestItem] = []
    kept = 0
    judged = 0
    for start in range(0, len(articles), BATCH_SIZE):
        batch = articles[start : start + BATCH_SIZE]
        user_prompt = build_filter_prompt(batch, snippet_length=filter_snippet_length)
        try:
            data = await complete_json(
                llm, user_prompt, system=system_prompt, temperature=1.0, usage=usage
            )
        except Exception:
            logger.warning(
                "Filter batch %d/%d failed; falling back to excerpt headlines",
                start // BATCH_SIZE + 1,
                total_batches,
                exc_info=True,
            )
            if usage is not None:
                usage.fell_back_to_excerpts = True
            items.extend(headlines_from_articles(batch, article_scores))
            continue
        selected = _selected_items(data, batch, article_scores)
        items.extend(selected)
        kept += len(selected)
        judged += len(batch)

    if judged:
        logger.info("Filter passed %d / %d articles (%.1f%%)", kept, judged, kept / judged * 100)
    return items


def _selected_items(
    data: dict, batch: list[Article], article_scores: dict[str, float] | None
) -> list[DigestItem]:
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
        if (
            not isinstance(entry["id"], int | str)
            or not isinstance(entry["title"], str)
            or not isinstance(entry["source"], str)
        ):
            logger.warning(
                "Skipping malformed filter entry with wrongly typed required fields: %r",
                entry,
            )
            continue
        position = echoed_position(entry["id"], len(batch))
        source_article = batch[position] if position is not None else None
        if source_article is None:
            logger.warning("Filter returned unknown article id %r; item has no link", entry["id"])
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
    return items
