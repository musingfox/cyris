"""Tests for filter integration with learning features."""

import json
from datetime import datetime

import pytest
from fakes import FakeLLM
from httpx import ConnectError, Response

from cyris.domain.models import Article, EmbeddingStore, PreferenceProfile, Tier, UsageStats
from cyris.learn.embeddings import filter_by_similarity
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
    async def test_filter_articles_baseline(self, respx_mock):
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


class TestFilterArticlesWithEmbeddings:
    @pytest.mark.asyncio
    async def test_filter_with_embedding_prefilter(self, respx_mock):
        """Apply embedding similarity pre-filter before Claude."""
        articles = [
            Article(
                id=1,
                title="AI Enterprise Adoption",
                url="https://example.com/1",
                content="Enterprise AI adoption is growing...",
                published_at=datetime(2026, 3, 19),
                source_name="TechCrunch",
                source_tier=Tier.FILTER,
            ),
            Article(
                id=2,
                title="Random Celebrity News",
                url="https://example.com/2",
                content="Celebrity does something...",
                published_at=datetime(2026, 3, 19),
                source_name="TMZ",
                source_tier=Tier.FILTER,
            ),
            Article(
                id=3,
                title="Cloud Infrastructure Security",
                url="https://example.com/3",
                content="Cloud security updates...",
                published_at=datetime(2026, 3, 19),
                source_name="Wired",
                source_tier=Tier.FILTER,
            ),
        ]

        # Mock centroid favoring tech content
        embedding_store = EmbeddingStore(
            centroid=[0.5] * 768,
            sample_count=10,
            last_updated="2026-03-19T00:00:00Z",
        )

        # Mock Ollama embeddings
        # Articles 1 and 3 similar to centroid, article 2 dissimilar
        respx_mock.post("http://localhost:11434/api/embeddings").mock(
            side_effect=[
                Response(200, json={"embedding": [0.52] * 768}),  # Similar
                Response(200, json={"embedding": [0.1] * 768}),  # Dissimilar
                Response(200, json={"embedding": [0.48] * 768}),  # Similar
            ]
        )

        # Mock Claude (should only receive 2 articles after pre-filter)
        mock_response = {
            "selected": [
                {
                    "id": 1,
                    "title": "企業 AI 採用率成長",
                    "summary": "企業 AI 採用成長",
                    "source": "TechCrunch",
                }
            ],
            "rejected_count": 1,
        }

        llm = FakeLLM(json.dumps(mock_response))

        results = await filter_articles(
            articles,
            llm,
            embedding_store=embedding_store,
            similarity_threshold=0.4,
        )

        assert len(results) == 1
        assert results[0].title == "企業 AI 採用率成長"

    @pytest.mark.asyncio
    async def test_filter_with_ollama_offline(self, respx_mock):
        """Gracefully skip embedding pre-filter if Ollama offline."""
        articles = [
            Article(
                id=1,
                title="Test Article",
                url="https://example.com/1",
                content="Content...",
                published_at=datetime(2026, 3, 19),
                source_name="Source",
                source_tier=Tier.FILTER,
            ),
        ]

        embedding_store = EmbeddingStore(
            centroid=[0.5] * 768,
            sample_count=10,
            last_updated="2026-03-19T00:00:00Z",
        )

        # Mock Ollama offline
        respx_mock.post("http://localhost:11434/api/embeddings").mock(
            side_effect=ConnectError("Connection refused")
        )

        # Mock Claude (should receive all articles)
        llm = FakeLLM(json.dumps({"selected": [], "rejected_count": 1}))

        results = await filter_articles(
            articles,
            llm,
            embedding_store=embedding_store,
        )

        # Should still work, just without pre-filter
        assert len(results) == 0


class TestFilterBySimilarity:
    def test_filter_by_similarity_threshold(self):
        """Filter articles by cosine similarity threshold."""
        from cyris.domain.models import Article, Tier

        articles = [
            Article(
                id=1,
                title="Article 1",
                url="https://example.com/1",
                content="Content 1",
                published_at=datetime(2026, 3, 19),
                source_name="Source",
                source_tier=Tier.FILTER,
            ),
            Article(
                id=2,
                title="Article 2",
                url="https://example.com/2",
                content="Content 2",
                published_at=datetime(2026, 3, 19),
                source_name="Source",
                source_tier=Tier.FILTER,
            ),
            Article(
                id=3,
                title="Article 3",
                url="https://example.com/3",
                content="Content 3",
                published_at=datetime(2026, 3, 19),
                source_name="Source",
                source_tier=Tier.FILTER,
            ),
        ]

        # Create embeddings with different directions
        # For centroid [1, 0, 0, ...], similar vectors point in same direction
        article_embeddings = [
            [1.0] + [0.0] * 767,  # High similarity (same direction)
            [0.0] + [1.0] + [0.0] * 766,  # Low similarity (perpendicular)
            [0.9] + [0.1] + [0.0] * 766,  # High similarity (close direction)
        ]

        centroid = [1.0] + [0.0] * 767

        passed, rejected = filter_by_similarity(
            articles, article_embeddings, centroid, threshold=0.6
        )

        assert len(passed) == 2
        assert len(rejected) == 1
        assert passed[0].id == 1
        assert passed[1].id == 3
        assert rejected[0].id == 2

    def test_filter_length_mismatch_raises_error(self):
        """Raise ValueError if articles and embeddings length mismatch."""
        from cyris.domain.models import Article, Tier

        articles = [
            Article(
                id=1,
                title="Article",
                url="https://example.com/1",
                content="Content",
                published_at=datetime(2026, 3, 19),
                source_name="Source",
                source_tier=Tier.FILTER,
            ),
        ]

        embeddings = [[0.5] * 768, [0.6] * 768]  # 2 embeddings for 1 article

        centroid = [0.5] * 768

        with pytest.raises(ValueError):
            filter_by_similarity(articles, embeddings, centroid)
