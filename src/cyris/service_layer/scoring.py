"""Article scoring via Claude API."""

import logging
from collections.abc import Callable, Sequence

from cyris.domain.language import detect_language
from cyris.domain.models import Lane, StoredArticle, UsageStats
from cyris.domain.tags import NEWS_TAG, normalize_tags
from cyris.service_layer.parse import echoed_position
from cyris.service_layer.ports import LLMClient, complete_json
from cyris.service_layer.prompts import build_scoring_prompt, build_scoring_system_prompt

logger = logging.getLogger(__name__)

BATCH_SIZE = 20

_LOGGED_IDS = 5
_LOGGED_ID_WIDTH = 80


def _first_ids(ids: Sequence[object]) -> str:
    """The first few ids with their types visible, each clipped; ids only, never article text."""
    shown = [repr(i)[:_LOGGED_ID_WIDTH] for i in ids[:_LOGGED_IDS]]
    return "[" + ", ".join(shown) + "]"


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
        if a.source_lane == Lane.FOLLOW:  # follow lane is never scored
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
    *,
    snippet_length: int,
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

    system_prompt = build_scoring_system_prompt()
    user_prompt = build_scoring_prompt(articles, snippet_length=snippet_length)

    data = await complete_json(llm, user_prompt, system=system_prompt, usage=usage)

    result: dict[str, tuple[float, str]] = {}
    url_to_tags: dict[str, list[str]] = {}
    entries = data.get("scores") or []
    matched = 0
    for entry in entries:
        position = echoed_position(entry.get("id"), len(articles))
        if position is not None:
            article = articles[position]
            url = article.url
            score = float(entry["score"])
            language = entry.get("language") or detect_language(article.title, article.content)
            matched += 1
            result[url] = (score, language)
            # Never trust the reply's shape: a null, a bare string, or a list
            # with non-strings costs this entry its tags, never the batch.
            raw_tags = entry.get("tags")
            if isinstance(raw_tags, str):
                raw_tags = [raw_tags]
            tags = normalize_tags(raw_tags) if isinstance(raw_tags, list) else []
            if tags:
                url_to_tags[url] = tags

    logger.info("Scored %d / %d articles", len(result), len(articles))
    if not result:
        # The entry count tells a reply with no scores from one whose ids match none;
        # the ids sent and received say why they matched none.
        sent = [a.original_id for a in articles]
        received = [entry.get("id") for entry in entries[:_LOGGED_IDS]]
        logger.warning(
            "Scored none of %d articles: the reply had %d entries; "
            "ids sent: %d of type %s, first five %s; ids received, first five: %s",
            len(articles),
            len(entries),
            len(sent),
            "/".join(sorted({type(i).__name__ for i in sent})),
            _first_ids(sent),
            _first_ids(received),
        )
    elif len(result) < len(articles):
        # A reply short of entries and a reply whose ids miss read alike in the count
        # above; the entry and match counts, and the ids on each side, tell them apart.
        unscored = [a.original_id for a in articles if a.url not in result]
        unmatched = [
            entry.get("id")
            for entry in entries
            if echoed_position(entry.get("id"), len(articles)) is None
        ]
        logger.warning(
            "Scored %d of %d articles: the reply had %d entries, %d matched a sent id; "
            "ids sent but not scored, first five %s; ids received that matched none, "
            "first five: %s",
            len(result),
            len(articles),
            len(entries),
            matched,
            _first_ids(unscored),
            _first_ids(unmatched),
        )
    return result, url_to_tags, usage


async def score_in_batches(
    articles: list[StoredArticle],
    llm: LLMClient,
    *,
    snippet_length: int,
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
            total_usage.fell_back_to_excerpts = True

    if persist_tags is not None and collected_tags:
        try:
            persist_tags(collected_tags)
        except Exception:
            logger.exception("Persisting tags failed; the scores are already written")

    return total_usage
