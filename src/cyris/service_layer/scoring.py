"""Article scoring via Claude API."""

import logging
from collections.abc import Callable, Sequence
from typing import NamedTuple

from cyris.domain.areas import fold_name
from cyris.domain.language import detect_language
from cyris.domain.models import Lane, StoredArticle, UsageStats
from cyris.domain.tags import NEWS_TAG, normalize_tags
from cyris.service_layer.ports import LLMClient, complete_json
from cyris.service_layer.prompts import build_scoring_prompt, build_scoring_system_prompt

logger = logging.getLogger(__name__)

BATCH_SIZE = 20

_LOGGED_IDS = 5
_LOGGED_ID_WIDTH = 80


class AreaPick(NamedTuple):
    """One article's area: the active name it matched, and a new name it suggests instead."""

    area: str | None
    suggest: str | None


def _parse_pick(entry: dict, area_names: Sequence[str]) -> AreaPick:
    """A bad or out-of-set pick costs only this entry its area, never the batch."""
    raw_area = entry.get("area")
    area = None
    if isinstance(raw_area, str):
        wanted = fold_name(raw_area)
        area = next((name for name in area_names if fold_name(name) == wanted), None)
    raw_suggest = entry.get("suggest_area")
    suggest = raw_suggest.strip() if isinstance(raw_suggest, str) else ""
    return AreaPick(area, suggest or None)


def _id_key(article_id: object) -> str:
    """An id as the prompt showed it: bare text, so `7`, `"7"` and `" 7 "` are one id.

    The prompt prints `original_id` without its type, so a reply may echo either. A
    whole-number float, which JSON's `7.0` parses to, is the id `7`, as `7.0 == 7` was.
    """
    if isinstance(article_id, float) and article_id.is_integer():
        return str(int(article_id))
    return str(article_id).strip()


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
    area_names: Sequence[str],
) -> tuple[dict[str, tuple[float, str]], dict[str, list[str]], UsageStats, dict[str, AreaPick]]:
    """Score a batch of articles via the LLM.

    Args:
        articles: Articles to score (max ~20 for optimal batch size).
        llm: LLM client.
        snippet_length: Maximum length of content snippet to include in prompt.
        area_names: The active areas the model may pick from.

    Returns:
        Tuple of score mapping, tag mapping, usage stats, and area picks.
        Score mapping is {url: (score, language)}.
    """
    usage = UsageStats(model=llm.model)

    if not articles:
        return {}, {}, usage, {}

    id_to_url = {_id_key(a.original_id): a.url for a in articles}
    id_to_fallback_lang = {
        _id_key(a.original_id): detect_language(a.title, a.content) for a in articles
    }

    system_prompt = build_scoring_system_prompt(area_names)
    user_prompt = build_scoring_prompt(articles, snippet_length=snippet_length)

    data = await complete_json(llm, user_prompt, system=system_prompt, usage=usage)

    result: dict[str, tuple[float, str]] = {}
    url_to_tags: dict[str, list[str]] = {}
    picks: dict[str, AreaPick] = {}
    entries = data.get("scores") or []
    for entry in entries:
        article_key = _id_key(entry["id"])
        score = float(entry["score"])
        language = entry.get("language") or id_to_fallback_lang.get(article_key, "en")
        url = id_to_url.get(article_key)
        if url:
            result[url] = (score, language)
            # Never trust the reply's shape: a null, a bare string, or a list
            # with non-strings costs this entry its tags, never the batch.
            raw_tags = entry.get("tags")
            if isinstance(raw_tags, str):
                raw_tags = [raw_tags]
            tags = normalize_tags(raw_tags) if isinstance(raw_tags, list) else []
            if tags:
                url_to_tags[url] = tags
            picks[url] = _parse_pick(entry, area_names)

    logger.info("Scored %d / %d articles", len(result), len(articles))
    if not result:
        # The entry count tells a reply with no scores from one whose ids match none;
        # the ids sent and received say why they matched none.
        sent = [a.original_id for a in articles]
        received = [entry["id"] for entry in entries[:_LOGGED_IDS]]
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
    return result, url_to_tags, usage, picks


async def score_in_batches(
    articles: list[StoredArticle],
    llm: LLMClient,
    *,
    snippet_length: int,
    progress: Callable[[str], None] = lambda _msg: None,
    persist: Callable[[dict[str, tuple[float, str]]], None] | None = None,
    persist_tags: Callable[[dict[str, list[str]]], None] | None = None,
    area_names: Sequence[str],
) -> tuple[UsageStats, dict[str, AreaPick]]:
    """Score articles in BATCH_SIZE chunks, persisting each batch as it completes.

    Args:
        articles: Articles to score.
        llm: LLM client.
        snippet_length: Content snippet length for the prompt.
        progress: Progress message callback.
        persist: Called with each batch's url→(score, language) mapping.
        persist_tags: Called once, after the last batch, with the accumulated
            URL-to-tags mapping — one write instead of one per batch.
        area_names: The active areas the model may pick from.

    Returns:
        Accumulated usage stats across all batches, and every article's area pick.
    """
    total_usage = UsageStats(model=llm.model)
    total_batches = (len(articles) + BATCH_SIZE - 1) // BATCH_SIZE
    collected_tags: dict[str, list[str]] = {}
    collected_picks: dict[str, AreaPick] = {}

    for i in range(0, len(articles), BATCH_SIZE):
        batch = articles[i : i + BATCH_SIZE]
        progress(f"  Batch {i // BATCH_SIZE + 1}/{total_batches}: {len(batch)} articles...")

        try:
            url_to_score_lang, url_to_tags, usage, picks = await score_articles_batch(
                batch,
                llm,
                snippet_length=snippet_length,
                area_names=area_names,
            )
            total_usage.merge(usage)

            if persist is not None:
                persist(url_to_score_lang)
            collected_tags.update(url_to_tags)
            collected_picks.update(picks)
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

    return total_usage, collected_picks
