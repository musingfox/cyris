"""Tests for prompt builders."""

from datetime import UTC, datetime

import pytest

from cyris.domain.models import Article, Lane
from cyris.service_layer.prompts import (
    CLEF_PREFERENCE,
    FILTER_CRITERIA,
    FILTER_SYSTEM,
    NO_BODY_MARKER,
    SUMMARIZE_SYSTEM,
    build_filter_prompt,
    build_filter_system_prompt,
    build_news_cluster_prompt,
    build_summarize_prompt,
    language_wording,
)

pytestmark = pytest.mark.unit


class TestBuildNewsClusterPrompt:
    def test_build_news_cluster_prompt(self):
        article = Article(
            id=101,
            title="Breaking News",
            url="https://example.com/1",
            content="A" * 600,  # 600 chars, should be truncated at 500
            published_at=datetime(2026, 3, 31, 10, 0, tzinfo=UTC),
            source_name="Reuters",
            source_lane=Lane.SKIM,
            source_tags=["news"],
        )

        prompt = build_news_cluster_prompt([article], snippet_length=500)

        assert "[0] (Reuters) Breaking News" in prompt
        # Check truncation at 500 chars
        lines = prompt.split("\n")
        content_line = [line for line in lines if line.strip().startswith("A")][0]
        assert len(content_line.strip()) == 500


class TestFilterSystemPrompt:
    def test_filter_system_no_percentage_target(self):
        """FILTER_SYSTEM should not contain percentage targets."""
        assert "TARGET:" not in FILTER_SYSTEM
        assert "5-10%" not in FILTER_SYSTEM
        assert "1-3 from a batch" not in FILTER_SYSTEM


class TestSummarizeSystemPrompt:
    def test_it_explains_and_does_not_analyse(self):
        """The prompt casts the model as the reader's editor, never as an analyst."""
        text = SUMMARIZE_SYSTEM.lower()
        assert "analyst" not in text
        assert "explain" in text
        assert "never analyse" in text

    @pytest.mark.parametrize(
        "counter_example",
        ["This reflects", "likely to", "The key argument is", "This means", "Worth reading if"],
    )
    def test_it_names_each_way_of_crossing_the_line(self, counter_example):
        """Analysis, inference, ranking points, consequences and advice are each named."""
        assert counter_example in SUMMARIZE_SYSTEM

    def test_it_says_a_missing_body_plainly(self):
        """An article with no fetched text gets that said, not invented content."""
        assert NO_BODY_MARKER in SUMMARIZE_SYSTEM
        assert "not fetched" in SUMMARIZE_SYSTEM

    def test_it_keeps_the_reply_shape_summarize_parses(self):
        for key in ('"heading"', '"summary"', '"summaries"', '"article_ids"'):
            assert key in SUMMARIZE_SYSTEM


class TestBuildFilterPrompt:
    def test_build_filter_prompt_custom_snippet_length_500(self):
        """Custom snippet_length=500 should truncate at 500 chars."""
        article = Article(
            id=302,
            title="Custom 500",
            url="https://example.com/custom500",
            content="Y" * 1000,
            published_at=datetime(2026, 4, 12, 10, 0, tzinfo=UTC),
            source_name="NewsSource",
            source_lane=Lane.SKIM,
            source_tags=["news"],
        )

        prompt = build_filter_prompt([article], snippet_length=500)

        assert "Y" * 500 in prompt
        assert "Y" * 501 not in prompt

    def test_build_filter_prompt_custom_snippet_length_100(self):
        """Custom snippet_length=100 should truncate at 100 chars."""
        article = Article(
            id=303,
            title="Custom 100",
            url="https://example.com/custom100",
            content="Z" * 1000,
            published_at=datetime(2026, 4, 12, 10, 0, tzinfo=UTC),
            source_name="ShortNews",
            source_lane=Lane.SKIM,
            source_tags=["short"],
        )

        prompt = build_filter_prompt([article], snippet_length=100)

        assert "Z" * 100 in prompt
        assert "Z" * 101 not in prompt


class TestBuildSummarizePrompt:
    def test_an_article_with_no_text_carries_the_marker(self):
        article = Article(
            id=203,
            title="EP200: A newsletter issue",
            url="https://example.com/empty",
            content="",
            published_at=datetime(2026, 10, 28, 10, 0, tzinfo=UTC),
            source_name="ByteByteGo",
            source_lane=Lane.READ,
            source_tags=["tech"],
        )

        prompt = build_summarize_prompt("tech", [article], snippet_length=1000)

        assert f"[0] (ByteByteGo) EP200: A newsletter issue\n    {NO_BODY_MARKER}" in prompt

    def test_an_article_with_text_carries_no_marker(self):
        article = Article(
            id=204,
            title="Has text",
            url="https://example.com/text",
            content="Some body.",
            published_at=datetime(2026, 10, 28, 10, 0, tzinfo=UTC),
            source_name="Blog",
            source_lane=Lane.READ,
            source_tags=["tech"],
        )

        assert NO_BODY_MARKER not in build_summarize_prompt("tech", [article], snippet_length=100)

    def test_build_summarize_prompt_truncates_at_snippet_length(self):
        article = Article(
            id=201,
            title="Tech Article",
            url="https://example.com/tech",
            content="B" * 2000,  # 2000 chars, should be truncated at 1000
            published_at=datetime(2026, 4, 9, 10, 0, tzinfo=UTC),
            source_name="TechCrunch",
            source_lane=Lane.READ,
            source_tags=["tech"],
        )

        prompt = build_summarize_prompt("tech", [article], snippet_length=1000)

        assert "Topic group: tech" in prompt
        assert "[0] (TechCrunch) Tech Article" in prompt
        assert "B" * 1000 in prompt
        assert "B" * 1001 not in prompt

    def test_build_summarize_prompt_custom_length(self):
        article = Article(
            id=202,
            title="AI Article",
            url="https://example.com/ai",
            content="C" * 2000,  # 2000 chars, should be truncated at 1500
            published_at=datetime(2026, 4, 9, 10, 0, tzinfo=UTC),
            source_name="ArXiv",
            source_lane=Lane.READ,
            source_tags=["ai"],
        )

        prompt = build_summarize_prompt("ai", [article], snippet_length=1500)

        assert "Topic group: ai" in prompt
        assert "[0] (ArXiv) AI Article" in prompt
        # Content should be truncated to 1500 chars
        assert "C" * 1500 in prompt
        assert "C" * 1501 not in prompt


class TestOutputLanguage:
    """The setting is a BCP 47 tag; the prompt gets a name a model can act on."""

    def test_a_listed_tag_becomes_its_name(self):
        assert language_wording("zh-Hant") == "繁體中文 (Traditional Chinese)"
        assert language_wording("en") == "English"

    def test_an_unlisted_tag_is_passed_through(self):
        # Keeps an unlisted language usable, and keeps a config still holding a
        # plain language name behaving exactly as it did before the tags landed.
        assert language_wording("pt-BR") == "pt-BR"
        assert language_wording("繁體中文") == "繁體中文"

    def test_a_listed_tag_reaches_the_system_prompt_as_a_name(self):
        prompt = build_filter_system_prompt("zh-Hant", style_prompt="")

        assert "繁體中文 (Traditional Chinese)" in prompt
        assert "zh-Hant" not in prompt
        assert "<output_language>" not in prompt


class TestClefPreference:
    WANT = (
        "reader_upvoted and reader_downvoted are this reader's own past votes on the articles "
        "most similar to this one. Judging by those votes, would this reader upvote this article?"
    )
    SEPARATOR = (
        " Where those votes say little about this article, fall back on these general criteria:\n\n"
    )

    def test_criteria_is_the_filter_prompt_before_its_format_block(self):
        assert FILTER_SYSTEM.split("Respond in JSON")[0].strip() == FILTER_CRITERIA

    def test_preference_is_framing_then_criteria(self):
        assert CLEF_PREFERENCE == self.WANT + self.SEPARATOR + FILTER_CRITERIA

    def test_preference_carries_selection_rules_but_not_the_format_block(self):
        assert "INCLUDE articles with at least one of these qualities:" in CLEF_PREFERENCE
        assert "EXCLUDE:" in CLEF_PREFERENCE
        assert "Respond in JSON" not in CLEF_PREFERENCE
