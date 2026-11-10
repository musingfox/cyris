"""The filter: batch articles through the LLM to keep headlines and assign each a topic."""

import logging
from collections.abc import Sequence

from cyris.domain.models import (
    Article,
    DigestItem,
    DigestSection,
    Lane,
    TopicKind,
    TopicRecord,
    TopicTag,
    UsageStats,
)
from cyris.domain.tags import normalize_tag
from cyris.domain.topics import TopicBook, topic_name
from cyris.service_layer.degrade import excerpt, headlines_from_articles
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
    topics: TopicBook,
    filter_snippet_length: int,
    output_language: str,
    style_prompt: str,
) -> list[DigestItem]:
    """Send articles to the LLM, BATCH_SIZE at a time and in order, one call each.

    Each article it keeps goes under a topic in `topics`, and a batch is offered the topics
    every batch before it opened. Only the skim lane is judged and comes back as a headline:
    a `read` or `follow` article is here for its topic alone and is always kept. No pass rate
    is enforced: `FILTER_SYSTEM` asks for merit, not a percentage target. A batch whose call
    fails falls back to excerpt headlines while the other batches are still judged.

    Args:
        articles: The articles to process, of any lane.
        llm: LLM client.
        usage: Optional UsageStats to accumulate token counts.
        topics: The topics on offer; takes each placement.

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
        return headlines_from_articles(_judged(articles), article_scores)

    total_batches = (len(articles) + BATCH_SIZE - 1) // BATCH_SIZE
    logger.info("Filtering %d articles through the LLM (batches: %d)", len(articles), total_batches)

    system_prompt = build_filter_system_prompt(output_language, style_prompt=style_prompt)

    items: list[DigestItem] = []
    kept = 0
    judged = 0
    for start in range(0, len(articles), BATCH_SIZE):
        batch = articles[start : start + BATCH_SIZE]
        offered = topics.names()
        user_prompt = build_filter_prompt(
            batch, snippet_length=filter_snippet_length, topic_names=offered
        )
        try:
            data = await complete_json(
                llm, user_prompt, system=system_prompt, temperature=1.0, usage=usage
            )
            selected, placements = _selected_items(data, batch, topics, article_scores)
        except Exception:
            logger.warning(
                "Filter batch %d/%d failed; falling back to excerpt headlines",
                start // BATCH_SIZE + 1,
                total_batches,
                exc_info=True,
            )
            if usage is not None:
                usage.fell_back_to_excerpts = True
            for article in batch:
                topics.ensure(article.url, article.title)
            items.extend(headlines_from_articles(_judged(batch), article_scores))
            continue
        # Placed only once the whole reply is read, so a reply that fails places nothing.
        for entry, article, fallback_name in placements:
            _place(entry, article, fallback_name, topics, len(offered))
        items.extend(selected)
        kept += len(selected)
        judged += len(_judged(batch))

    if judged:
        logger.info("Filter passed %d / %d articles (%.1f%%)", kept, judged, kept / judged * 100)
    return items


def _judged(articles: list[Article]) -> list[Article]:
    return [a for a in articles if a.source_lane == Lane.SKIM]


def _selected_items(
    data: dict,
    batch: list[Article],
    topics: TopicBook,
    article_scores: dict[str, float] | None,
) -> tuple[list[DigestItem], list[tuple[dict, Article, str]]]:
    """The reply's headlines, and each article's placement with the name it falls back on."""
    items = []
    placements: list[tuple[dict, Article, str]] = []
    answered: set[str] = set()
    for entry in data.get("selected", []):
        position = echoed_position(entry.get("id"), len(batch)) if isinstance(entry, dict) else None
        article = batch[position] if position is not None else None
        if article is not None and article.source_lane != Lane.SKIM:
            if article.url not in answered:
                answered.add(article.url)
                placements.append((entry, article, article.title))
            continue
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
        if article is None:
            logger.warning("Filter returned unknown article id %r; item has no link", entry["id"])
        elif article.url in answered or topics.holds(article.url):
            logger.warning("Filter named article id %r twice; keeping the first", entry["id"])
            continue
        else:
            answered.add(article.url)
            placements.append((entry, article, entry["title"]))
        article_url = article.url if article else ""
        score = article_scores.get(article_url) if article_scores and article_url else None
        items.append(
            DigestItem(
                title=entry["title"],
                summary=entry.get("summary", ""),
                sources=[entry["source"]],
                urls=[article_url] if article_url else [],
                ref_urls=article.ref_urls if article else [],
                score=score,
            )
        )
    return items, placements


def _place(
    entry: dict, article: Article, fallback_name: str, topics: TopicBook, offered: int
) -> None:
    """Put the article where the entry says; a topic the reply does not name is its headline,
    and with a blank headline too the article is left to a topic of its own.

    A number can only name what the prompt offered, not a topic this same reply opened.
    """
    raw_name = entry.get("topic_name")
    name = topic_name(raw_name) if isinstance(raw_name, str) else ""
    topics.place(
        article.url,
        position=echoed_position(entry.get("topic_id"), offered),
        name=name or fallback_name,
        tags=_tags(entry.get("tags")),
    )


def _tags(raw: object) -> list[TopicTag]:
    """The reply's tags, normalized. Never trust its shape: a null, a bare string or a list with
    other values costs those values at worst. A tag whose kind is missing or unknown is a domain
    tag, which carries no preference (ADR-0023).
    """
    if isinstance(raw, str):
        raw = [raw]
    tags = []
    for entry in raw if isinstance(raw, list) else []:
        typed = entry if isinstance(entry, dict) else {"tag": entry}
        name = typed.get("tag")
        tag = normalize_tag(name) if isinstance(name, str) else None
        if tag is not None:
            tags.append(TopicTag(tag=tag, kind=_kind(typed.get("kind"))))
    return tags


def _kind(raw: object) -> TopicKind:
    try:
        return TopicKind(str(raw).strip().casefold())
    except ValueError:
        return TopicKind.DOMAIN


def _member(article: Article, article_scores: dict[str, float] | None) -> DigestItem:
    """One article of a sand block, as the page's fold and the sand line read it.

    Stand-in until the sand-what-happened ticket: the summary is the article's own
    80-character excerpt, which the page shows as the sand block's one line for want
    of a sentence written for the topic. A wire article opens with its dateline, which
    would spend the line on the wire's name, so the excerpt starts after it.
    """
    return DigestItem(
        title=article.title,
        summary=excerpt(article.content, 80, drop_dateline=True) if article.content else "",
        sources=[article.source_name],
        urls=[article.url],
        score=(article_scores or {}).get(article.url),
        ref_urls=article.ref_urls,
        passed_through=True,
    )


def _sand(
    topic: TopicRecord, articles: Sequence[Article], article_scores: dict[str, float] | None
) -> DigestSection:
    """A topic's two or more skim articles: one merged item carrying the vote, and each member."""
    scores = [article_scores[a.url] for a in articles if article_scores and a.url in article_scores]
    # Merge member reference links only when at least one member has any: members without
    # refs contribute their store URL unless it is a synthetic non-http one
    # (e.g. "newsletter:<id>"), which is a dead link.
    ref_urls = []
    if any(a.ref_urls for a in articles):
        ref_urls = list(
            dict.fromkeys(
                u
                for a in articles
                for u in (
                    a.ref_urls or ([a.url] if a.url.startswith(("http://", "https://")) else [])
                )
            )
        )
    merged = DigestItem(
        title=topic.name,
        summary="",
        sources=[a.source_name for a in articles],
        urls=[a.url for a in articles],
        score=max(scores) if scores else None,
        ref_urls=ref_urls,
    )
    return DigestSection(
        heading=topic.name,
        items=[merged],
        tags=[t.tag for t in topic.tags],
        members=[_member(a, article_scores) for a in articles],
    )


def sections_by_topic(
    kept: list[DigestItem],
    topics: TopicBook,
    articles: Sequence[Article],
    article_scores: dict[str, float] | None = None,
) -> tuple[list[DigestSection], list[DigestItem]]:
    """The skim articles the filter kept, as the issue holds them: (sand, headlines).

    A topic's kept articles are decided by their count in this issue alone: two or more are
    one sand section, one is a headline, a continuing topic included. An item with no link
    has no article, so no topic, and stays a headline. A topic of its own is named by the
    headline, or by the article's title when the headline is blank.
    """
    by_url = {a.url: a for a in articles}
    held: dict[str, list[str]] = {}
    for item in kept:
        if item.urls:
            own = topic_name(item.title) or by_url[item.urls[0]].title
            held.setdefault(topics.ensure(item.urls[0], own).id, []).append(item.urls[0])

    sand: list[DigestSection] = []
    headlines: list[DigestItem] = []
    for item in kept:
        if not item.urls:
            headlines.append(item)
            continue
        topic = topics.ensure(item.urls[0], item.title)
        urls = held[topic.id]
        if len(urls) == 1:
            headlines.append(item.model_copy(update={"tags": [t.tag for t in topic.tags]}))
        elif urls[0] == item.urls[0]:
            sand.append(_sand(topic, [by_url[u] for u in urls], article_scores))
    return sand, headlines
