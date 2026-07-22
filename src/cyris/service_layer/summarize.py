"""Summarize-tier processor: group articles by tag, then summarize each group via Claude."""

import logging
from collections import defaultdict

from cyris.domain.models import Article, DigestItem, DigestSection, UsageStats
from cyris.service_layer.degrade import excerpt_sections_from_articles
from cyris.service_layer.ports import LLMClient, complete_json
from cyris.service_layer.prompts import (
    DEFAULT_LANGUAGE,
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
    snippet_length: int = 1000,
    article_scores: dict[str, float] | None = None,
    output_language: str = DEFAULT_LANGUAGE,
    style_prompt: str = "",
) -> list[DigestSection]:
    """Summarize articles grouped by source tags.

    Articles are pre-grouped by their first source tag, then each group
    is sent to Claude for thematic summarization.

    Args:
        articles: Summarize-tier articles to process.
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
        logger.warning("No LLM configured; summarize tier falls back to excerpts")
        return excerpt_sections_from_articles(articles, article_scores)

    groups = _group_by_tags(articles)
    logger.info("Summarizing %d articles in %d tag groups", len(articles), len(groups))

    article_map = {a.id: a for a in articles}

    sections = []
    for tag, group_articles in groups.items():
        user_prompt = build_summarize_prompt(tag, group_articles, snippet_length=snippet_length)

        try:
            data = await complete_json(
                llm,
                user_prompt,
                system=build_summarize_system_prompt(output_language, style_prompt),
                temperature=1.0,
                usage=usage,
            )
        except Exception:
            logger.warning(
                "Summarize LLM call failed for tag '%s'; excerpt fallback", tag, exc_info=True
            )
            sections.extend(excerpt_sections_from_articles(group_articles, article_scores))
            continue

        for section_data in data.get("sections", []):
            items = []
            for art_ref in section_data.get("articles", []):
                source_article = article_map.get(art_ref.get("id"))
                article_url = source_article.url if source_article else ""
                score = article_scores.get(article_url) if article_scores and article_url else None
                items.append(
                    DigestItem(
                        title=art_ref.get("title", ""),
                        summary=section_data.get("summary", ""),
                        sources=[art_ref.get("source", "")],
                        urls=[article_url] if article_url else [],
                        score=score,
                    )
                )

            sections.append(
                DigestSection(
                    heading=section_data["heading"],
                    items=items,
                )
            )

    logger.info("Generated %d thematic sections", len(sections))
    return sections


def _strip_html_snippet(html: str, max_len: int = 80) -> str:
    """Strip HTML tags and return a plain text snippet."""
    import re

    text = re.sub(r"<[^>]+>", "", html)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return text


def build_fan_sections(articles: list[Article]) -> list[DigestSection]:
    """Build fan-tier sections grouped by source (no AI, never discarded).

    Each followed group/newsletter becomes one section; items carry a short
    non-LLM excerpt so updates from tracked groups keep their own channel and
    aren't washed out by the knowledge sections.

    Args:
        articles: Fan-tier articles to group by source.

    Returns:
        One DigestSection per source, items in feed order.
    """
    if not articles:
        return []

    groups: dict[str, list[Article]] = {}
    for article in articles:
        groups.setdefault(article.source_name, []).append(article)

    sections = []
    for source_name, group in groups.items():
        items = [
            DigestItem(
                title=article.title,
                summary=_strip_html_snippet(article.content) if article.content else "",
                sources=[article.source_name],
                urls=[article.url],
            )
            for article in group
        ]
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
            snippet = _strip_html_snippet(article.content) if article.content else ""
            items.append(
                DigestItem(
                    title=article.title,
                    summary=snippet,
                    sources=[article.source_name],
                    urls=[article.url],
                    score=score,
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
