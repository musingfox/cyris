"""Tests for prompt builders."""

from datetime import UTC, datetime

from cyris.domain.models import Article, Tier
from cyris.service_layer.prompts import (
    DEFAULT_LANGUAGE,
    FILTER_SYSTEM,
    SUMMARIZE_SYSTEM,
    build_filter_prompt,
    build_filter_system_prompt,
    build_news_cluster_prompt,
    build_summarize_prompt,
    language_wording,
)


class TestBuildNewsClusterPrompt:
    def test_build_news_cluster_prompt(self):
        article = Article(
            id=101,
            title="Breaking News",
            url="https://example.com/1",
            content="A" * 600,  # 600 chars, should be truncated at 500
            published_at=datetime(2026, 3, 31, 10, 0, tzinfo=UTC),
            source_name="Reuters",
            source_tier=Tier.FILTER,
            source_tags=["news"],
        )

        prompt = build_news_cluster_prompt([article])

        assert "[101] (Reuters) Breaking News" in prompt
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
    def test_summarize_system_depth_and_synthesis(self):
        """SUMMARIZE_SYSTEM should specify 3-5 sentence structure and synthesis."""
        assert "3-5" in SUMMARIZE_SYSTEM
        # Check for synthesis-related instructions
        assert (
            "synthesize" in SUMMARIZE_SYSTEM.lower()
            or "diverge" in SUMMARIZE_SYSTEM.lower()
            or "agree" in SUMMARIZE_SYSTEM.lower()
        )


class TestBuildFilterPrompt:
    def test_build_filter_prompt_default_snippet_length(self):
        """Default snippet length should be 500 chars."""
        article = Article(
            id=301,
            title="Filter Article",
            url="https://example.com/filter",
            content="X" * 1000,  # 1000 chars, should be truncated at 500 (default)
            published_at=datetime(2026, 4, 12, 10, 0, tzinfo=UTC),
            source_name="TechNews",
            source_tier=Tier.FILTER,
            source_tags=["tech"],
        )

        prompt = build_filter_prompt([article])

        assert "[301] (TechNews) Filter Article" in prompt
        # Should truncate at 500 chars (default)
        assert "X" * 500 in prompt
        assert "X" * 501 not in prompt

    def test_build_filter_prompt_custom_snippet_length_500(self):
        """Custom snippet_length=500 should truncate at 500 chars."""
        article = Article(
            id=302,
            title="Custom 500",
            url="https://example.com/custom500",
            content="Y" * 1000,
            published_at=datetime(2026, 4, 12, 10, 0, tzinfo=UTC),
            source_name="NewsSource",
            source_tier=Tier.FILTER,
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
            source_tier=Tier.FILTER,
            source_tags=["short"],
        )

        prompt = build_filter_prompt([article], snippet_length=100)

        assert "Z" * 100 in prompt
        assert "Z" * 101 not in prompt


class TestBuildSummarizePrompt:
    def test_build_summarize_prompt_default_length(self):
        article = Article(
            id=201,
            title="Tech Article",
            url="https://example.com/tech",
            content="B" * 2000,  # 2000 chars, should be truncated at 1000 (default)
            published_at=datetime(2026, 4, 9, 10, 0, tzinfo=UTC),
            source_name="TechCrunch",
            source_tier=Tier.SUMMARIZE,
            source_tags=["tech"],
        )

        prompt = build_summarize_prompt("tech", [article])

        assert "Topic group: tech" in prompt
        assert "[0] (TechCrunch) Tech Article" in prompt
        # Content should be truncated to 1000 chars (default)
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
            source_tier=Tier.SUMMARIZE,
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

    def test_the_default_tag_reaches_the_system_prompt_as_a_name(self):
        prompt = build_filter_system_prompt(DEFAULT_LANGUAGE)

        assert "繁體中文 (Traditional Chinese)" in prompt
        assert "zh-Hant" not in prompt
        assert "<output_language>" not in prompt
