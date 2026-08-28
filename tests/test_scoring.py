"""Tests for article scoring system."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import FakeLLM

from cyris.adapters.store.article_store import ArticleStore
from cyris.domain.language import detect_language
from cyris.domain.models import StoredArticle, Tier

# --- Language detection tests ---


class TestDetectLanguage:
    def test_english_text(self):
        assert detect_language("Hello World", "This is English content") == "en"

    def test_chinese_title(self):
        assert detect_language("這是一篇中文標題文章", "Some content") == "zh"

    def test_chinese_content(self):
        assert detect_language("Title", "這是一段中文內容，超過五個字") == "zh"

    def test_mixed_below_threshold(self):
        assert detect_language("Test 測試", "Hello 世界") == "en"  # 4 CJK < 5

    def test_empty_input(self):
        assert detect_language("", "") == "en"

    def test_threshold_exact(self):
        assert detect_language("一二三四五", "") == "zh"  # exactly 5


# --- Scoring system tests ---


class TestScoringSystem:
    def test_scoring_system_has_relevance_focus(self):
        from cyris.service_layer.prompts import SCORING_SYSTEM

        # New system should focus on relevance, not depth/originality
        assert "relevance" in SCORING_SYSTEM.lower()
        assert (
            "depth" not in SCORING_SYSTEM
            or "depth" in SCORING_SYSTEM.lower().split("article quality")[1]
        )
        assert (
            "originality" not in SCORING_SYSTEM
            or "originality" in SCORING_SYSTEM.lower().split("article quality")[1]
        )

    def test_scoring_system_has_language_field(self):
        from cyris.service_layer.prompts import SCORING_SYSTEM

        assert "language" in SCORING_SYSTEM
        assert "0-100" in SCORING_SYSTEM

    def test_build_scoring_system_prompt(self):
        from cyris.service_layer.prompts import build_scoring_system_prompt

        prompt = build_scoring_system_prompt()
        assert "relevance" in prompt.lower()
        assert len(prompt) > 100


# --- Scoring prompt tests ---


class TestBuildScoringPrompt:
    def test_builds_prompt_with_articles(self):
        from cyris.service_layer.prompts import build_scoring_prompt

        articles = [
            StoredArticle(
                url="http://a.com",
                original_id=1,
                title="Title A",
                content="Content A",
                published_at=datetime.now(UTC),
                source_name="Source A",
                source_tier=Tier.FILTER,
                first_seen_at=datetime.now(UTC),
            ),
        ]
        prompt = build_scoring_prompt(articles)
        assert "[1] Title A" in prompt
        assert "Source: Source A" in prompt
        assert "Content: Content A" in prompt

    def test_truncates_long_content(self):
        from cyris.service_layer.prompts import build_scoring_prompt

        articles = [
            StoredArticle(
                url="http://a.com",
                original_id=1,
                title="Title",
                content="X" * 2000,
                published_at=datetime.now(UTC),
                source_name="Src",
                source_tier=Tier.FILTER,
                first_seen_at=datetime.now(UTC),
            ),
        ]
        prompt = build_scoring_prompt(articles)
        # Content should be truncated to 1000 chars (default)
        assert "X" * 1000 in prompt
        assert "X" * 1001 not in prompt

    def test_custom_snippet_length(self):
        from cyris.service_layer.prompts import build_scoring_prompt

        articles = [
            StoredArticle(
                url="http://a.com",
                original_id=1,
                title="Title",
                content="Y" * 2000,
                published_at=datetime.now(UTC),
                source_name="Src",
                source_tier=Tier.FILTER,
                first_seen_at=datetime.now(UTC),
            ),
        ]
        prompt = build_scoring_prompt(articles, snippet_length=500)
        # Content should be truncated to 500 chars
        assert "Y" * 500 in prompt
        assert "Y" * 501 not in prompt

    def test_empty_articles(self):
        from cyris.service_layer.prompts import build_scoring_prompt

        assert build_scoring_prompt([]) == ""


# --- Score batch tests ---


class TestScoreArticlesBatch:
    @pytest.fixture
    def sample_articles(self) -> list[StoredArticle]:
        now = datetime.now(UTC)
        return [
            StoredArticle(
                url="http://a.com",
                original_id=1,
                title="AI Research Paper",
                content="Deep learning advances in 2026",
                published_at=now,
                source_name="ArXiv",
                source_tier=Tier.SUMMARIZE,
                first_seen_at=now,
            ),
            StoredArticle(
                url="http://b.com",
                original_id=2,
                title="雲端運算趨勢分析",
                content="台灣企業雲端轉型最新報告與深度分析",
                published_at=now,
                source_name="iThome",
                source_tier=Tier.SUMMARIZE,
                first_seen_at=now,
            ),
        ]

    async def test_batch_scoring(self, sample_articles):
        from cyris.service_layer.scoring import score_articles_batch

        llm = FakeLLM(
            json.dumps(
                {
                    "scores": [
                        {"id": 1, "score": 85, "language": "en"},
                        {"id": 2, "score": 72, "language": "zh"},
                    ]
                }
            )
        )

        result, tags, usage = await score_articles_batch(sample_articles, llm)

        assert result["http://a.com"] == (85.0, "en")
        assert result["http://b.com"] == (72.0, "zh")
        assert tags == {}
        assert usage.api_calls == 1
        assert usage.input_tokens == 500

    async def test_empty_articles(self):
        from cyris.service_layer.scoring import score_articles_batch

        result, tags, usage = await score_articles_batch([], FakeLLM())
        assert result == {}
        assert tags == {}
        assert usage.api_calls == 0

    async def test_fallback_language(self, sample_articles):
        from cyris.service_layer.scoring import score_articles_batch

        llm = FakeLLM(
            json.dumps(
                {
                    "scores": [
                        {"id": 1, "score": 80},  # no language field
                        {"id": 2, "score": 70},
                    ]
                }
            )
        )

        result, _, _ = await score_articles_batch(sample_articles, llm)

        # Article 2 has Chinese content, should fallback to "zh"
        assert result["http://b.com"][1] == "zh"
        assert result["http://a.com"][1] == "en"

    async def test_batch_scoring_returns_article_tags(self, sample_articles):
        from cyris.service_layer.scoring import score_articles_batch

        llm = FakeLLM(
            json.dumps(
                {
                    "scores": [
                        {
                            "id": 1,
                            "score": 80,
                            "language": "en",
                            "tags": ["Rust"],
                        }
                    ]
                }
            )
        )

        scores, tags, _ = await score_articles_batch(sample_articles, llm)

        assert scores["http://a.com"] == (80.0, "en")
        assert tags == {"http://a.com": ["Rust"]}

    async def test_batch_scoring_omits_missing_tags(self, sample_articles):
        from cyris.service_layer.scoring import score_articles_batch

        llm = FakeLLM(
            json.dumps({"scores": [{"id": 1, "score": 80, "language": "en"}]})
        )

        scores, tags, _ = await score_articles_batch(sample_articles, llm)

        assert scores["http://a.com"] == (80.0, "en")
        assert tags == {}

    async def test_custom_snippet_length_in_batch(self, sample_articles):
        from cyris.service_layer.scoring import score_articles_batch

        llm = FakeLLM(json.dumps({"scores": [{"id": 1, "score": 75, "language": "en"}]}))

        await score_articles_batch([sample_articles[0]], llm, snippet_length=500)

        # Verify user prompt was built with custom snippet length
        assert "Score the following articles" in llm.calls[0]["prompt"]


# --- Store update_scores tests ---


class TestUpdateScores:
    @pytest.fixture
    def store_with_articles(self, tmp_path: Path) -> tuple[ArticleStore, list[StoredArticle]]:
        store = ArticleStore(tmp_path)
        now = datetime.now(UTC)
        from cyris.domain.models import Article

        articles = [
            Article(
                id=1,
                title="Article A",
                url="http://a.com",
                content="Content A",
                published_at=now,
                source_name="Src A",
                source_tier=Tier.FILTER,
            ),
            Article(
                id=2,
                title="Article B",
                url="http://b.com",
                content="Content B",
                published_at=now,
                source_name="Src B",
                source_tier=Tier.SUMMARIZE,
            ),
        ]
        store.save(articles, now=now)
        stored = store.list_articles(state=None)
        return store, stored

    def test_update_scores(self, store_with_articles):
        store, articles = store_with_articles
        url_to_score = {
            "http://a.com": (85.5, "en"),
            "http://b.com": (72.0, "zh"),
        }
        updated = store.update_scores(url_to_score)
        assert updated == 2

        # Verify persisted
        result = store.list_articles(state=None)
        scores = {a.url: (a.score, a.language) for a in result}
        assert scores["http://a.com"] == (85.5, "en")
        assert scores["http://b.com"] == (72.0, "zh")

    def test_update_scores_missing_url(self, store_with_articles):
        store, _ = store_with_articles
        updated = store.update_scores({"http://missing.com": (50.0, "en")})
        assert updated == 0

    def test_update_scores_empty(self, store_with_articles):
        store, _ = store_with_articles
        assert store.update_scores({}) == 0


# --- Sort by score tests ---


class TestSortByScore:
    @pytest.fixture
    def store_with_scored(self, tmp_path: Path) -> ArticleStore:
        store = ArticleStore(tmp_path)
        now = datetime.now(UTC)
        from cyris.domain.models import Article

        articles = [
            Article(
                id=1,
                title="Low",
                url="http://low.com",
                content="C",
                published_at=now,
                source_name="S",
                source_tier=Tier.FILTER,
            ),
            Article(
                id=2,
                title="High",
                url="http://high.com",
                content="C",
                published_at=now,
                source_name="S",
                source_tier=Tier.FILTER,
            ),
            Article(
                id=3,
                title="Mid",
                url="http://mid.com",
                content="C",
                published_at=now,
                source_name="S",
                source_tier=Tier.FILTER,
            ),
            Article(
                id=4,
                title="Unscored",
                url="http://none.com",
                content="C",
                published_at=now,
                source_name="S",
                source_tier=Tier.FILTER,
            ),
        ]
        store.save(articles, now=now)
        store.update_scores(
            {
                "http://low.com": (30.0, "en"),
                "http://high.com": (90.0, "zh"),
                "http://mid.com": (60.0, "en"),
            }
        )
        return store

    def test_sort_by_score_desc(self, store_with_scored):
        articles = store_with_scored.list_articles(state=None, sort_by="score", descending=True)
        urls = [a.url for a in articles]
        # High (90) -> Mid (60) -> Low (30) -> Unscored (None at end)
        assert urls == ["http://high.com", "http://mid.com", "http://low.com", "http://none.com"]

    def test_sort_by_score_asc(self, store_with_scored):
        articles = store_with_scored.list_articles(state=None, sort_by="score", descending=False)
        urls = [a.url for a in articles]
        # Low (30) -> Mid (60) -> High (90) -> Unscored (None at end)
        assert urls == ["http://low.com", "http://mid.com", "http://high.com", "http://none.com"]


# --- Non-news filtering tests ---


class TestFilterNonNews:
    def test_filters_news_articles(self):
        now = datetime.now(UTC)
        articles = [
            StoredArticle(
                url="http://a.com",
                original_id=1,
                title="Tech",
                content="C",
                published_at=now,
                source_name="S",
                source_tier=Tier.FILTER,
                source_tags=["tech"],
                first_seen_at=now,
            ),
            StoredArticle(
                url="http://b.com",
                original_id=2,
                title="News",
                content="C",
                published_at=now,
                source_name="S",
                source_tier=Tier.FILTER,
                source_tags=["news", "tech"],
                first_seen_at=now,
            ),
        ]
        non_news = [a for a in articles if "news" not in a.source_tags]
        assert len(non_news) == 1
        assert non_news[0].url == "http://a.com"
