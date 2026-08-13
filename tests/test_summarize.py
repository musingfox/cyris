"""Tests for summarize-tier processor."""

import json
from datetime import UTC

from fakes import FakeLLM

from cyris.domain.models import Tier
from cyris.service_layer.summarize import _group_by_tags, summarize_articles


class TestGroupByTags:
    def test_groups_by_first_tag(self, sample_filter_articles, sample_summarize_articles):
        all_articles = sample_filter_articles + sample_summarize_articles
        groups = _group_by_tags(all_articles)

        assert "tech" in groups
        assert "international" in groups
        assert len(groups["tech"]) == 2  # TechCrunch + Stratechery
        assert len(groups["international"]) == 1

    def test_no_tags_goes_to_general(self):
        from datetime import datetime

        from cyris.domain.models import Article

        article = Article(
            id=1,
            title="No tags",
            url="https://example.com",
            content="Content",
            published_at=datetime(2026, 3, 16, tzinfo=UTC),
            source_name="Unknown",
            source_tier=Tier.SUMMARIZE,
            source_tags=[],
        )
        groups = _group_by_tags([article])
        assert "general" in groups


class TestSummarizeArticles:
    async def test_summarize_returns_sections(self, sample_summarize_articles):
        llm = FakeLLM(
            json.dumps(
                {
                    "sections": [
                        {
                            "heading": "AI 監管趨勢",
                            "summary": "歐盟 AI 法案持續推進",
                            "articles": [
                                {
                                    "id": 103,
                                    "title": "Weekly tech trends",
                                    "source": "Stratechery",
                                }
                            ],
                        }
                    ]
                }
            )
        )

        sections = await summarize_articles(sample_summarize_articles, llm)

        assert len(sections) == 1
        assert sections[0].heading == "AI 監管趨勢"
        assert len(sections[0].items) == 1

    async def test_summarize_empty_input(self):
        sections = await summarize_articles([], FakeLLM())
        assert sections == []

    async def test_summarize_articles_passes_temperature(self, sample_summarize_articles):
        """summarize_articles should pass temperature=1.0 to the LLM."""
        llm = FakeLLM(json.dumps({"sections": []}))

        await summarize_articles(sample_summarize_articles, llm)

        assert len(llm.calls) == 1
        assert llm.calls[0]["temperature"] == 1.0


class TestBuildAttentionSections:
    def test_empty_input(self):
        from cyris.service_layer.summarize import build_attention_sections

        sections = build_attention_sections([])
        assert sections == []

    def test_empty_scores(self):
        from cyris.service_layer.summarize import build_attention_sections

        sections = build_attention_sections([], {})
        assert sections == []

    def test_single_article_with_tag_and_score(self, sample_summarize_articles):
        from cyris.service_layer.summarize import build_attention_sections

        article = sample_summarize_articles[0]  # Stratechery, tag "tech"
        article_scores = {article.url: 55.0}

        sections = build_attention_sections([article], article_scores)

        assert len(sections) == 1
        # Single-article groups use the article title as heading
        assert sections[0].heading == article.title
        assert len(sections[0].items) == 1
        assert sections[0].items[0].title == article.title
        assert sections[0].items[0].summary != ""  # content snippet
        assert sections[0].items[0].sources == [article.source_name]
        assert sections[0].items[0].urls == [article.url]
        assert sections[0].items[0].score == 55.0

    def test_multi_tag_grouping(self):
        from datetime import datetime

        from cyris.domain.models import Article, Tier
        from cyris.service_layer.summarize import build_attention_sections

        articles = [
            Article(
                id=1,
                title="Tech Article 1",
                url="https://example.com/tech1",
                content="Content 1",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                source_name="TechNews",
                source_tier=Tier.SUMMARIZE,
                source_tags=["tech"],
            ),
            Article(
                id=2,
                title="Tech Article 2",
                url="https://example.com/tech2",
                content="Content 2",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                source_name="TechBlog",
                source_tier=Tier.SUMMARIZE,
                source_tags=["tech"],
            ),
            Article(
                id=3,
                title="No Tag Article",
                url="https://example.com/notag",
                content="Content 3",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                source_name="General",
                source_tier=Tier.SUMMARIZE,
                source_tags=[],
            ),
        ]

        article_scores = {
            "https://example.com/tech1": 65.0,
            "https://example.com/tech2": 60.0,
        }

        sections = build_attention_sections(articles, article_scores)

        assert len(sections) == 2
        tech_section = next(s for s in sections if s.heading == "tech")
        # Single-article groups use the article title as heading
        general_section = next(s for s in sections if s.heading == "No Tag Article")

        assert len(tech_section.items) == 2
        assert len(general_section.items) == 1
        assert general_section.items[0].score is None

    def test_missing_score(self):
        from datetime import datetime

        from cyris.domain.models import Article, Tier
        from cyris.service_layer.summarize import build_attention_sections

        article = Article(
            id=1,
            title="Article",
            url="https://example.com/1",
            content="Content",
            published_at=datetime(2026, 4, 10, tzinfo=UTC),
            source_name="Source",
            source_tier=Tier.SUMMARIZE,
            source_tags=["tag"],
        )

        sections = build_attention_sections([article], None)

        assert len(sections) == 1
        assert sections[0].items[0].score is None


class TestDigestItemsCarryRefUrls:
    def test_fan_item_keeps_ref_urls_without_changing_store_url(self):
        from datetime import datetime

        from cyris.domain.models import Article
        from cyris.service_layer.summarize import build_fan_sections

        article = Article(
            id=1,
            title="Newsletter",
            url="newsletter:abc",
            content="Content",
            published_at=datetime(2026, 4, 10, tzinfo=UTC),
            source_name="Newsletter",
            source_tier=Tier.FAN,
            ref_urls=["https://r1.com/a"],
        )

        item = build_fan_sections([article])[0].items[0]

        assert item.ref_urls == ["https://r1.com/a"]
        assert item.urls == ["newsletter:abc"]

    def test_attention_item_keeps_ref_urls_without_changing_store_url(self):
        from datetime import datetime

        from cyris.domain.models import Article
        from cyris.service_layer.summarize import build_attention_sections

        article = Article(
            id=1,
            title="Article",
            url="newsletter:abc",
            content="Content",
            published_at=datetime(2026, 4, 10, tzinfo=UTC),
            source_name="Newsletter",
            source_tier=Tier.SUMMARIZE,
            ref_urls=["https://r1.com/a"],
        )

        item = build_attention_sections([article])[0].items[0]

        assert item.ref_urls == ["https://r1.com/a"]
        assert item.urls == ["newsletter:abc"]

    async def test_summarized_item_keeps_ref_urls_without_changing_store_url(self):
        from datetime import datetime

        from cyris.domain.models import Article

        article = Article(
            id=1,
            title="Article",
            url="newsletter:abc",
            content="Content",
            published_at=datetime(2026, 4, 10, tzinfo=UTC),
            source_name="Newsletter",
            source_tier=Tier.SUMMARIZE,
            ref_urls=["https://r1.com/a"],
        )
        llm = FakeLLM(
            json.dumps(
                {
                    "sections": [
                        {
                            "heading": "Heading",
                            "summary": "Summary",
                            "articles": [{"id": 1, "title": "Article", "source": "Newsletter"}],
                        }
                    ]
                }
            )
        )

        item = (await summarize_articles([article], llm))[0].items[0]

        assert item.ref_urls == ["https://r1.com/a"]
        assert item.urls == ["newsletter:abc"]

    def test_degraded_item_keeps_ref_urls_without_changing_store_url(self):
        from datetime import datetime

        from cyris.domain.models import Article
        from cyris.service_layer.degrade import excerpt_sections_from_articles

        article = Article(
            id=1,
            title="Article",
            url="newsletter:abc",
            content="Content",
            published_at=datetime(2026, 4, 10, tzinfo=UTC),
            source_name="Newsletter",
            source_tier=Tier.SUMMARIZE,
            ref_urls=["https://r1.com/a"],
        )

        item = excerpt_sections_from_articles([article])[0].items[0]

        assert item.ref_urls == ["https://r1.com/a"]
        assert item.urls == ["newsletter:abc"]

    async def test_rss_items_default_to_empty_ref_urls(self):
        from datetime import datetime

        from cyris.domain.models import Article
        from cyris.service_layer.degrade import excerpt_sections_from_articles
        from cyris.service_layer.summarize import build_attention_sections, build_fan_sections

        fan_article = Article(
            id=1,
            title="Fan",
            url="https://example.com/fan",
            content="Content",
            published_at=datetime(2026, 4, 10, tzinfo=UTC),
            source_name="Source",
            source_tier=Tier.FAN,
        )
        summarize_article = fan_article.model_copy(update={"source_tier": Tier.SUMMARIZE})
        llm = FakeLLM(
            json.dumps(
                {
                    "sections": [
                        {
                            "heading": "Heading",
                            "summary": "Summary",
                            "articles": [{"id": 1, "title": "Fan", "source": "Source"}],
                        }
                    ]
                }
            )
        )

        fan_item = build_fan_sections([fan_article])[0].items[0]
        attention_item = build_attention_sections([summarize_article])[0].items[0]
        summarized_item = (await summarize_articles([summarize_article], llm))[0].items[0]
        degraded_item = excerpt_sections_from_articles([summarize_article])[0].items[0]

        assert [
            item.ref_urls for item in (fan_item, attention_item, summarized_item, degraded_item)
        ] == [
            [],
            [],
            [],
            [],
        ]
