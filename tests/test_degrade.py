"""Tests for degraded-mode fallbacks when the LLM is unavailable."""

from datetime import datetime

import pytest

from cyris.domain.models import Article, Tier
from cyris.service_layer.degrade import (
    excerpt,
    excerpt_sections_from_articles,
    headlines_from_articles,
)
from cyris.service_layer.filtering import filter_articles
from cyris.service_layer.summarize import summarize_articles


def _article(aid: int, tag: str = "tech") -> Article:
    return Article(
        id=aid,
        title=f"Title {aid}",
        url=f"https://example.com/{aid}",
        content="word " * 100,
        published_at=datetime(2026, 3, 19),
        source_name="Src",
        source_tier=Tier.FILTER,
        source_tags=[tag],
    )


def test_excerpt_truncates_long_and_keeps_short():
    assert excerpt("a" * 500, length=200).endswith("…")
    assert len(excerpt("a" * 500, length=200)) <= 201
    assert excerpt("short text") == "short text"
    assert excerpt("") == ""


def test_excerpt_strips_html_and_decodes_entities():
    html_content = '<figure> <img alt="pic"> </figure><p>Google&#39;s cloud &amp; AI</p>'
    assert excerpt(html_content) == "Google's cloud & AI"


def test_headlines_keep_every_article_with_excerpt():
    items = headlines_from_articles([_article(1), _article(2)])
    assert len(items) == 2  # nothing dropped without an LLM
    assert all(it.summary for it in items)  # excerpt stands in for a summary


def test_excerpt_sections_group_by_tag():
    sections = excerpt_sections_from_articles([_article(1, "tech"), _article(2, "biz")])
    assert len(sections) == 2  # one section per tag
    assert all(s.items for s in sections)


@pytest.mark.asyncio
async def test_filter_degrades_without_llm():
    items = await filter_articles([_article(1), _article(2)], None)
    assert len(items) == 2  # excerpt headlines, no crash


@pytest.mark.asyncio
async def test_summarize_degrades_without_llm():
    sections = await summarize_articles([_article(1)], None)
    assert sections and sections[0].items
