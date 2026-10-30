"""Read-lane processor: group articles by tag, then summarize each group via Claude."""

import logging
from collections import defaultdict
from collections.abc import Mapping

from cyris.domain.models import Article, DigestItem, DigestSection, UsageStats
from cyris.service_layer.degrade import excerpt, excerpt_sections_from_articles
from cyris.service_layer.ports import LLMClient, complete_json
from cyris.service_layer.prompts import (
    build_summarize_prompt,
    build_summarize_system_prompt,
)

logger = logging.getLogger(__name__)


def _group_by_tags(articles: list[Article]) -> dict[str, list[Article]]:
    """Group articles by their first source tag. Articles with no tags go to 'general'."""
    groups: dict[str, list[Article]] = defaultdict(list)
    for article in articles:
        tag = article.source_tags[0] if article.source_tags else "general"
        groups[tag].append(article)
    return dict(groups)


async def summarize_articles(
    articles: list[Article],
    llm: LLMClient | None,
    usage: UsageStats | None = None,
    article_scores: dict[str, float] | None = None,
    *,
    snippet_length: int,
    output_language: str,
    style_prompt: str,
) -> list[DigestSection]:
    """Summarize articles grouped by source tags.

    Articles are pre-grouped by their first source tag, then each group
    is sent to Claude for thematic summarization.

    Args:
        articles: Read-lane articles to process.
        llm: LLM client.
        usage: Optional UsageStats object to accumulate token usage.
        snippet_length: Maximum length of content snippet to include in prompt.

    Returns:
        Thematic digest sections with summaries.
    """
    if not articles:
        return []

    # Degraded mode: no LLM → one plain excerpt per article, grouped by tag
    if llm is None:
        logger.warning("No LLM configured; read lane falls back to excerpts")
        if usage is not None:
            usage.fell_back_to_excerpts = True
        return excerpt_sections_from_articles(articles, article_scores)

    groups = _group_by_tags(articles)
    logger.info("Summarizing %d articles in %d tag groups", len(articles), len(groups))

    sections = []
    for tag, group_articles in groups.items():
        user_prompt = build_summarize_prompt(tag, group_articles, snippet_length=snippet_length)

        try:
            data = await complete_json(
                llm,
                user_prompt,
                system=build_summarize_system_prompt(output_language, style_prompt=style_prompt),
                temperature=1.0,
                usage=usage,
            )
        except Exception:
            logger.warning(
                "Summarize LLM call failed for tag '%s'; excerpt fallback", tag, exc_info=True
            )
            if usage is not None:
                usage.fell_back_to_excerpts = True
            sections.extend(excerpt_sections_from_articles(group_articles, article_scores))
            continue

        for section_data in data.get("sections", []):
            # Ids are group-positional indexes; everything else (title, source,
            # url, ref_urls) comes from the local article, never the LLM echo.
            raw_ids = section_data.get("article_ids") or [
                ref.get("id") for ref in section_data.get("articles", [])
            ]
            own_summaries = section_data.get("summaries")
            if not isinstance(own_summaries, dict):
                own_summaries = {}
            items = []
            for raw_id in raw_ids:
                try:
                    index = int(raw_id)
                except (TypeError, ValueError):
                    index = -1
                if not 0 <= index < len(group_articles):
                    logger.warning(
                        "Summarize returned unknown article id %r for tag '%s'", raw_id, tag
                    )
                    continue
                article = group_articles[index]
                own = own_summaries.get(str(raw_id))
                fell_back = not isinstance(own, str) or not own.strip()
                if fell_back:
                    logger.warning(
                        "Summarize wrote no summary for article id %r in tag '%s'; excerpt",
                        raw_id,
                        tag,
                    )
                    if usage is not None:
                        usage.fell_back_to_excerpts = True
                    own = excerpt(article.content)
                items.append(
                    DigestItem(
                        title=article.title,
                        summary=own,
                        sources=[article.source_name],
                        urls=[article.url],
                        score=article_scores.get(article.url) if article_scores else None,
                        ref_urls=article.ref_urls,
                        passed_through=fell_back,
                    )
                )

            sections.append(
                DigestSection(
                    heading=section_data["heading"],
                    items=items,
                    summary=section_data.get("summary") or None,
                )
            )

    logger.info("Generated %d thematic sections", len(sections))
    return sections


def _explanations_in(data: object, group: list[Article]) -> dict[str, str]:
    """The explanations a reply holds for `group`, by URL; malformed parts are skipped."""
    sections = data.get("sections") if isinstance(data, dict) else None
    if not isinstance(sections, list):
        raise ValueError(f"explain reply has no sections list: {data!r:.200}")
    found: dict[str, str] = {}
    for section_data in sections:
        own_summaries = section_data.get("summaries") if isinstance(section_data, dict) else None
        if not isinstance(own_summaries, dict):
            continue
        for raw_id, text in own_summaries.items():
            try:
                index = int(raw_id)
            except (TypeError, ValueError):
                continue
            if 0 <= index < len(group) and isinstance(text, str) and text.strip():
                found[group[index].url] = text
    return found


async def explain_follow_articles(
    articles: list[Article],
    llm: LLMClient,
    usage: UsageStats | None = None,
    *,
    snippet_length: int,
    output_language: str,
    style_prompt: str,
) -> dict[str, str]:
    """Each follow-lane article's explanation by URL, one call per source.

    The read lane's prompt writes it, so a followed article's card says what the article
    is the way a read article's does. An article the model left unexplained, or a source
    whose call failed, is absent, and keeps its excerpt.
    """
    groups: dict[str, list[Article]] = {}
    for article in articles:
        groups.setdefault(article.source_name, []).append(article)

    explanations: dict[str, str] = {}
    for source_name, group in groups.items():
        try:
            data = await complete_json(
                llm,
                build_summarize_prompt(source_name, group, snippet_length=snippet_length),
                system=build_summarize_system_prompt(output_language, style_prompt=style_prompt),
                temperature=1.0,
                usage=usage,
            )
            explanations.update(_explanations_in(data, group))
        except Exception:
            logger.warning(
                "Explain failed for source '%s'; excerpts kept", source_name, exc_info=True
            )
            if usage is not None:
                usage.fell_back_to_excerpts = True
    missing = sum(1 for a in articles if a.url not in explanations)
    if missing:
        logger.warning("Explain wrote nothing for %d follow articles; excerpts kept", missing)
        if usage is not None:
            usage.fell_back_to_excerpts = True
    return explanations


def build_follow_sections(
    articles: list[Article], explanations: Mapping[str, str] | None = None
) -> list[DigestSection]:
    """Build follow-lane sections grouped by source, never discarded.

    Each followed group/newsletter becomes one section, so updates from tracked groups
    keep their own channel and aren't washed out by the knowledge sections. An item
    carries its explanation from `explanations` when it has one, else a short excerpt
    of its own text.

    Args:
        articles: Follow-lane articles to group by source.
        explanations: Explanations by article URL, from `explain_follow_articles`.

    Returns:
        One DigestSection per source, items in feed order.
    """
    if not articles:
        return []

    explanations = explanations or {}
    groups: dict[str, list[Article]] = {}
    for article in articles:
        groups.setdefault(article.source_name, []).append(article)

    sections = []
    for source_name, group in groups.items():
        items = []
        for article in group:
            explanation = explanations.get(article.url)
            items.append(
                DigestItem(
                    title=article.title,
                    summary=explanation
                    or (excerpt(article.content, 80) if article.content else ""),
                    sources=[article.source_name],
                    urls=[article.url],
                    ref_urls=article.ref_urls,
                    passed_through=explanation is None,
                )
            )
        sections.append(DigestSection(heading=source_name, items=items))

    logger.info("Built %d fan sections from %d articles", len(sections), len(articles))
    return sections


def build_attention_sections(
    articles: list[Article],
    article_scores: dict[str, float] | None = None,
) -> list[DigestSection]:
    """Build attention sections from articles grouped by tag (no AI processing).

    Groups articles by their first tag and creates DigestItems with empty summaries.
    Scores are injected from article_scores if provided.

    Args:
        articles: Articles to group and convert to attention sections.
        article_scores: Optional mapping of article URLs to scores.

    Returns:
        List of DigestSections with DigestItems (empty summaries).
    """
    if not articles:
        return []

    groups = _group_by_tags(articles)
    sections = []

    for tag, group_articles in groups.items():
        items = []
        for article in group_articles:
            score = article_scores.get(article.url) if article_scores else None
            snippet = excerpt(article.content, 80) if article.content else ""
            items.append(
                DigestItem(
                    title=article.title,
                    summary=snippet,
                    sources=[article.source_name],
                    urls=[article.url],
                    score=score,
                    ref_urls=article.ref_urls,
                )
            )

        heading = group_articles[0].title if len(group_articles) == 1 else tag
        sections.append(
            DigestSection(
                heading=heading,
                items=items,
            )
        )

    logger.info("Built %d attention sections from %d articles", len(sections), len(articles))
    return sections
