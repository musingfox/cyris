"""Tests for filter-tier processor."""

import json
import logging
from datetime import UTC, datetime

from fakes import FakeLLM

from cyris.domain.models import Article, Tier
from cyris.service_layer.filtering import filter_articles


class TestFilterArticles:
    async def test_filter_returns_noteworthy_items(self, sample_filter_articles):
        llm = FakeLLM(
            json.dumps(
                {
                    "selected": [
                        {
                            "id": 101,
                            "title": "Apple Vision Pro 第二代發表",
                            "summary": "價格降至 $2499",
                            "source": "TechCrunch",
                        }
                    ]
                }
            )
        )

        items = await filter_articles(sample_filter_articles, llm)

        assert len(items) == 1
        assert items[0].title == "Apple Vision Pro 第二代發表"
        assert items[0].urls == ["https://techcrunch.com/2026/03/16/apple-vision-pro-2"]

    async def test_filter_skips_entry_missing_id_and_keeps_valid_entry(
        self, sample_filter_articles, caplog
    ):
        llm = FakeLLM(
            json.dumps(
                {
                    "selected": [
                        {"title": "only-title"},
                        {"id": 101, "title": "ok", "source": "S"},
                    ]
                }
            )
        )

        with caplog.at_level(logging.WARNING):
            items = await filter_articles(sample_filter_articles, llm)

        assert [item.title for item in items] == ["ok"]
        assert len(caplog.records) == 1

    async def test_filter_skips_entries_missing_title_or_source(
        self, sample_filter_articles, caplog
    ):
        llm = FakeLLM(
            json.dumps(
                {
                    "selected": [
                        {"id": 101, "source": "S"},
                        {"id": 101, "title": "missing-source"},
                    ]
                }
            )
        )

        with caplog.at_level(logging.WARNING):
            items = await filter_articles(sample_filter_articles, llm)

        assert items == []
        assert len(caplog.records) == 2

    async def test_filter_empty_input(self):
        items = await filter_articles([], FakeLLM())
        assert items == []

    async def test_filter_nothing_noteworthy(self, sample_filter_articles):
        llm = FakeLLM(json.dumps({"selected": []}))

        items = await filter_articles(sample_filter_articles, llm)

        assert items == []

    async def test_filter_articles_passes_temperature(self, sample_filter_articles):
        """filter_articles should pass temperature=1.0 to the LLM."""
        llm = FakeLLM(json.dumps({"selected": []}))

        await filter_articles(sample_filter_articles, llm)

        assert len(llm.calls) == 1
        assert llm.calls[0]["temperature"] == 1.0

    async def test_filter_item_keeps_ref_urls_without_changing_store_url(self):
        article = Article(
            id=101,
            title="Newsletter",
            url="newsletter:abc",
            content="Content",
            published_at=datetime(2026, 4, 10, tzinfo=UTC),
            source_name="Newsletter",
            source_tier=Tier.FILTER,
            ref_urls=["https://r1.com/a", "https://r2.com/b"],
        )
        llm = FakeLLM(
            json.dumps({"selected": [{"id": 101, "title": "Newsletter", "source": "Newsletter"}]})
        )

        item = (await filter_articles([article], llm))[0]

        assert item.ref_urls == ["https://r1.com/a", "https://r2.com/b"]
        assert item.urls == ["newsletter:abc"]

    async def test_filter_item_without_ref_urls_uses_empty_list(self):
        article = Article(
            id=102,
            title="RSS",
            url="https://ex.com/2",
            content="Content",
            published_at=datetime(2026, 4, 10, tzinfo=UTC),
            source_name="RSS",
            source_tier=Tier.FILTER,
        )
        llm = FakeLLM(json.dumps({"selected": [{"id": 102, "title": "RSS", "source": "RSS"}]}))

        item = (await filter_articles([article], llm))[0]

        assert item.ref_urls == []
        assert item.urls == ["https://ex.com/2"]
