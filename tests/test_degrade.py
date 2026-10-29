"""Tests for degraded-mode fallbacks when the LLM is unavailable."""

from datetime import datetime

import pytest

from cyris.domain.models import Article, Lane
from cyris.service_layer.degrade import (
    excerpt,
    excerpt_sections_from_articles,
    headlines_from_articles,
    strip_dateline,
)
from cyris.service_layer.filtering import filter_articles
from cyris.service_layer.summarize import summarize_articles

pytestmark = pytest.mark.unit


def _article(aid: int, tag: str = "tech") -> Article:
    return Article(
        id=aid,
        title=f"Title {aid}",
        url=f"https://example.com/{aid}",
        content="word " * 100,
        published_at=datetime(2026, 3, 19),
        source_name="Src",
        source_lane=Lane.SKIM,
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


LONG_ASIDE = "（" + "這段說明很長" * 8 + "，談的是停電）其後正文。"


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        # A wire dateline in full-width brackets, ending in 報導 or 電.
        ("（中央社首爾29日綜合外電報導）南韓軍方表示。", "南韓軍方表示。"),
        ("（中央社記者王小明台北29日電）氣象署今天表示。", "氣象署今天表示。"),
        # The same in ASCII brackets, with a space after it.
        ("(中央社香港29日綜合外電報導) 聯準會維持利率不變。", "聯準會維持利率不變。"),
        # No dateline: the text is unchanged.
        ("南韓軍方表示，北韓今天發射飛彈。", "南韓軍方表示，北韓今天發射飛彈。"),
        # A leading parenthesis that is not a dateline stays: a caption mark, and an
        # aside that ends in 電 but runs past a dateline's length.
        ("（圖）南韓軍方表示。", "（圖）南韓軍方表示。"),
        # Short brackets that end in a closing word but carry no date: 電 is also
        # electricity, and a feature is a 報導 too.
        ("（台電）今天宣布調漲電價。", "（台電）今天宣布調漲電價。"),
        ("（停電）供電尚未恢復。", "（停電）供電尚未恢復。"),
        ("（因停電）醫院暫停急診。", "（因停電）醫院暫停急診。"),
        ("（圖／台電）供電尚未恢復。", "（圖／台電）供電尚未恢復。"),
        ("（專題報導）偏鄉的醫療。", "（專題報導）偏鄉的醫療。"),
        (LONG_ASIDE, LONG_ASIDE),
        # A dateline anywhere but the very start is the text's own.
        ("正文開頭（中央社台北29日電）其後。", "正文開頭（中央社台北29日電）其後。"),
    ],
)
def test_strip_dateline_drops_only_a_leading_wire_dateline(text, kept):
    assert strip_dateline(text) == kept


def test_headlines_keep_every_article_with_excerpt():
    items = headlines_from_articles([_article(1), _article(2)])
    assert len(items) == 2  # nothing dropped without an LLM
    assert all(it.summary for it in items)  # excerpt stands in for a summary


def test_every_fallback_item_says_its_text_is_the_articles_own():
    headlines = headlines_from_articles([_article(1), _article(2)])
    sections = excerpt_sections_from_articles([_article(3, "tech"), _article(4, "biz")])

    items = [*headlines, *(item for section in sections for item in section.items)]
    assert [item.passed_through for item in items] == [True] * 4


def test_excerpt_sections_group_by_tag():
    sections = excerpt_sections_from_articles([_article(1, "tech"), _article(2, "biz")])
    assert len(sections) == 2  # one section per tag
    assert all(s.items for s in sections)


@pytest.mark.asyncio
async def test_filter_degrades_without_llm():
    items = await filter_articles(
        [_article(1), _article(2)],
        None,
        filter_snippet_length=500,
        output_language="zh-Hant",
        style_prompt="",
    )
    assert len(items) == 2  # excerpt headlines, no crash


@pytest.mark.asyncio
async def test_summarize_degrades_without_llm():
    sections = await summarize_articles(
        [_article(1)], None, snippet_length=1000, output_language="zh-Hant", style_prompt=""
    )
    assert sections and sections[0].items
