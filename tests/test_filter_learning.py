"""Tests for filter integration with learning features."""

import json
from datetime import datetime

import pytest
from fakes import FakeLLM

from cyris.domain.models import Article, PreferenceProfile, Tier, UsageStats
from cyris.service_layer.filtering import filter_articles
from cyris.service_layer.prompts import build_filter_system_prompt


class TestBuildFilterSystemPrompt:
    def test_without_profile_substitutes_default_language(self):
        """Without profile, returns the base prompt with default language substituted."""
        from cyris.service_layer.prompts import FILTER_SYSTEM

        result = build_filter_system_prompt()
        assert result == FILTER_SYSTEM.replace("<output_language>", "繁體中文")
        assert "<output_language>" not in result

    def test_language_is_substituted(self):
        """A custom output language replaces the placeholder."""
        result = build_filter_system_prompt(language="English")
        assert "English" in result
        assert "<output_language>" not in result

    def test_style_prompt_is_appended(self):
        """A style prompt is injected into the system prompt."""
        result = build_filter_system_prompt(style_prompt="Be concise and skeptical.")
        assert "Be concise and skeptical." in result

    def test_with_profile_appends_injection(self):
        """Append prompt injection when profile provided."""
        from cyris.service_layer.prompts import FILTER_SYSTEM

        profile = PreferenceProfile(
            generated_at="2026-03-19T10:00:00Z",
            sample_size=10,
            themes=["AI"],
            signals=["Funding"],
            anti_signals=["Listicles"],
            prompt_injection="User prefers enterprise AI and major funding rounds.",
        )

        result = build_filter_system_prompt(preference_profile=profile)

        base = FILTER_SYSTEM.replace("<output_language>", "繁體中文")
        assert result.startswith(base)
        assert "User prefers enterprise AI" in result


class TestFilterArticlesWithoutLearning:
    @pytest.mark.asyncio
    async def test_filter_articles_baseline(self):
        """Filter articles without learning data (baseline behavior)."""
        articles = [
            Article(
                id=1,
                title="Major AI Startup Raises $100M",
                url="https://example.com/1",
                content="AI startup secures major funding...",
                published_at=datetime(2026, 3, 19),
                source_name="TechCrunch",
                source_tier=Tier.FILTER,
            ),
            Article(
                id=2,
                title="10 Tips for Better Productivity",
                url="https://example.com/2",
                content="Listicle content...",
                published_at=datetime(2026, 3, 19),
                source_name="Blog",
                source_tier=Tier.FILTER,
            ),
        ]

        mock_response = {
            "selected": [
                {
                    "id": 1,
                    "title": "重大 AI 新創募資 1 億美元",
                    "summary": "AI 新創獲得重大資金",
                    "source": "TechCrunch",
                }
            ],
            "rejected_count": 1,
        }

        llm = FakeLLM(json.dumps(mock_response), input_tokens=200, output_tokens=100)

        usage = UsageStats(model="claude-sonnet-4-6")
        results = await filter_articles(articles, llm, usage=usage)

        assert len(results) == 1
        assert results[0].title == "重大 AI 新創募資 1 億美元"
        assert usage.input_tokens == 200
        assert usage.output_tokens == 100
