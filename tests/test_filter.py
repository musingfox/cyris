"""Tests for filter-tier processor."""

import json

from fakes import FakeLLM

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
