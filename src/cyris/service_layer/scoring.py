"""Article scoring via Claude API."""

import logging
from collections.abc import Callable

from cyris.domain.language import detect_language
from cyris.domain.models import StoredArticle, Tier, UsageStats
from cyris.domain.tags import NEWS_TAG, normalize_tags
from cyris.service_layer.ports import LLMClient, complete_json
from cyris.service_layer.prompts import build_scoring_prompt, build_scoring_system_prompt

logger = logging.getLogger(__name__)

BATCH_SIZE = 20


def select_scorable(
    articles: list[StoredArticle],
    *,
    force: bool = False,
) -> list[StoredArticle]:
    """The articles a scoring pass should send to the LLM.

    One rule, one implementation: `run_digest` and `cyris articles score` both
    call this, so neither can drift into scoring what the other skips.
    """
    scorable = []
    for a in articles:
        if a.source_tier == Tier.FAN:  # fan tier is never scored
            continue
        if NEWS_TAG in a.source_tags:
            continue
        if not force and a.score is not None:
            continue
        scorable.append(a)
    return scorable


async def score_articles_batch(
    articles: list[StoredArticle],
    llm: LLMClient,
    snippet_length: int = 1000,
) -> tuple[dict[str, tuple[float, str]], dict[str, list[str]], UsageStats]:
    """Score a batch of articles via the LLM.

    Args:
        articles: Articles to score (max ~20 for optimal batch size).
        llm: LLM client.
        snippet_length: Maximum length of content snippet to include in prompt.

    Returns:
        Tuple of score mapping, tag mapping, and usage stats.
        Score mapping is {url: (score, language)}.
    """
    usage = UsageStats(model=llm.model)

    if not articles:
        return {}, {}, usage

    id_to_url = {a.original_id: a.url for a in articles}
    id_to_fallback_lang = {a.original_id: detect_language(a.title, a.content) for a in articles}

    system_prompt = build_scoring_system_prompt()
    user_prompt = build_scoring_prompt(articles, snippet_length=snippet_length)

    data = await complete_json(llm, user_prompt, system=system_prompt, usage=usage)

    result: dict[str, tuple[float, str]] = {}
    url_to_tags: dict[str, list[str]] = {}
    for entry in data.get("scores", []):
        article_id = entry["id"]
        score = float(entry["score"])
        language = entry.get("language") or id_to_fallback_lang.get(article_id, "en")
        url = id_to_url.get(article_id)
        if url:
            result[url] = (score, language)
            # Same distrust as cluster_news: a null, a bare string, or a list
            # with non-strings costs this entry its tags, never the batch.
            raw_tags = entry.get("tags")
            if isinstance(raw_tags, str):
                raw_tags = [raw_tags]
            tags = normalize_tags(raw_tags) if isinstance(raw_tags, list) else []
            if tags:
                url_to_tags[url] = tags

    logger.info("Scored %d / %d articles", len(result), len(articles))
    return result, url_to_tags, usage


async def score_in_batches(
    articles: list[StoredArticle],
    llm: LLMClient,
    snippet_length: int = 1000,
    progress: Callable[[str], None] = lambda _msg: None,
    persist: Callable[[dict[str, tuple[float, str]]], None] | None = None,
    persist_tags: Callable[[dict[str, list[str]]], None] | None = None,
) -> UsageStats:
    """Score articles in BATCH_SIZE chunks, persisting each batch as it completes.

    Args:
        articles: Articles to score.
        llm: LLM client.
        snippet_length: Content snippet length for the prompt.
        progress: Progress message callback.
        persist: Called with each batch's url→(score, language) mapping.
        persist_tags: Called once, after the last batch, with the accumulated
            URL-to-tags mapping — one write instead of one per batch.

    Returns:
        Accumulated usage stats across all batches.
    """
    total_usage = UsageStats(model=llm.model)
    total_batches = (len(articles) + BATCH_SIZE - 1) // BATCH_SIZE
    collected_tags: dict[str, list[str]] = {}

    for i in range(0, len(articles), BATCH_SIZE):
        batch = articles[i : i + BATCH_SIZE]
        progress(f"  Batch {i // BATCH_SIZE + 1}/{total_batches}: {len(batch)} articles...")

        try:
            url_to_score_lang, url_to_tags, usage = await score_articles_batch(
                batch,
                llm,
                snippet_length=snippet_length,
            )
            total_usage.merge(usage)

            if persist is not None:
                persist(url_to_score_lang)
            collected_tags.update(url_to_tags)
        except Exception:
            # One bad batch loses its own scores, not the whole run's.
            logger.exception(
                "Scoring batch %d/%d failed; continuing", i // BATCH_SIZE + 1, total_batches
            )

    if persist_tags is not None and collected_tags:
        try:
            persist_tags(collected_tags)
        except Exception:
            logger.exception("Persisting tags failed; the scores are already written")

    return total_usage
