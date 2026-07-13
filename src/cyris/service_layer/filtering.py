"""Filter-tier processor: batch articles through Claude to extract noteworthy headlines."""

import logging

import httpx

from cyris.domain.models import Article, DigestItem, EmbeddingStore, PreferenceProfile, UsageStats
from cyris.service_layer.ports import LLMClient, complete_json
from cyris.service_layer.prompts import (
    DEFAULT_LANGUAGE,
    build_filter_prompt,
    build_filter_system_prompt,
)

logger = logging.getLogger(__name__)


async def filter_articles(
    articles: list[Article],
    llm: LLMClient,
    usage: UsageStats | None = None,
    preference_profile: PreferenceProfile | None = None,
    embedding_store: EmbeddingStore | None = None,
    ollama_url: str = "http://localhost:11434",
    similarity_threshold: float = 0.6,
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
        preference_profile: Optional user preference profile for prompt injection.
        embedding_store: Optional embedding centroid for similarity pre-filtering.
        ollama_url: Ollama API base URL for embedding generation.
        similarity_threshold: Minimum cosine similarity to pass pre-filter.

    Returns:
        Noteworthy headlines as DigestItems.
    """
    if not articles:
        return []

    # Apply embedding pre-filter if available
    articles_to_process = articles
    if embedding_store is not None:
        try:
            from cyris.learn.embeddings import cosine_similarity, generate_embeddings_batch

            logger.info("Generating embeddings for %d articles", len(articles))
            article_texts = [f"{a.title}\n{a.content[:500]}" for a in articles]
            article_embeddings = await generate_embeddings_batch(article_texts, ollama_url)

            # Filter by similarity
            passed = []
            rejected_count = 0
            for article, embedding in zip(articles, article_embeddings, strict=False):
                similarity = cosine_similarity(embedding, embedding_store.centroid)
                if similarity >= similarity_threshold:
                    passed.append(article)
                else:
                    rejected_count += 1

            logger.info(
                "Embedding pre-filter: %d passed, %d rejected (threshold=%.2f)",
                len(passed),
                rejected_count,
                similarity_threshold,
            )
            articles_to_process = passed

        except (httpx.ConnectError, httpx.TimeoutException) as e:
            logger.warning("Ollama API unavailable, skipping embedding pre-filter: %s", e)
        except Exception as e:
            logger.warning("Embedding pre-filter failed, using all articles: %s", e)

    if not articles_to_process:
        logger.info("No articles passed embedding pre-filter")
        return []

    logger.info("Filtering %d articles through Claude", len(articles_to_process))

    user_prompt = build_filter_prompt(articles_to_process, snippet_length=filter_snippet_length)
    system_prompt = build_filter_system_prompt(output_language, style_prompt, preference_profile)

    data = await complete_json(llm, user_prompt, system=system_prompt, temperature=1.0, usage=usage)

    # Build lookup for source URLs
    article_map = {a.id: a for a in articles_to_process}

    items = []
    for entry in data.get("selected", []):
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
