"""Tests for filter-tier processor."""

import json
import logging
import re
from datetime import UTC, datetime

import pytest
from fakes import FakeLLM

from cyris.domain.models import Article, Tier, UsageStats
from cyris.service_layer.filtering import filter_articles

pytestmark = pytest.mark.unit


class TestFilterArticles:
    @pytest.mark.parametrize(
        ("llm", "fell_back"),
        [
            (None, True),
            (FakeLLM(error=RuntimeError("quota")), True),
            (FakeLLM(json.dumps({"selected": []})), False),
        ],
        ids=["no-client", "call-failed", "answered"],
    )
    async def test_only_a_fallback_marks_the_usage(self, sample_filter_articles, llm, fell_back):
        usage = UsageStats(model="m")

        await filter_articles(
            sample_filter_articles,
            llm,
            usage=usage,
            filter_snippet_length=500,
            output_language="zh-Hant",
            style_prompt="",
        )

        assert usage.fell_back_to_excerpts is fell_back

    async def test_filter_returns_noteworthy_items(self, sample_filter_articles):
        llm = FakeLLM(
            json.dumps(
                {
                    "selected": [
                        {
                            "id": 0,
                            "title": "Apple Vision Pro 第二代發表",
                            "summary": "價格降至 $2499",
                            "source": "TechCrunch",
                        }
                    ]
                }
            )
        )

        items = await filter_articles(
            sample_filter_articles,
            llm,
            filter_snippet_length=500,
            output_language="zh-Hant",
            style_prompt="",
        )

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
                        {"id": 0, "title": "ok", "source": "S"},
                    ]
                }
            )
        )

        with caplog.at_level(logging.WARNING):
            items = await filter_articles(
                sample_filter_articles,
                llm,
                filter_snippet_length=500,
                output_language="zh-Hant",
                style_prompt="",
            )

        assert [item.title for item in items] == ["ok"]
        assert len(caplog.records) == 1

    async def test_filter_skips_entries_missing_title_or_source(
        self, sample_filter_articles, caplog
    ):
        llm = FakeLLM(
            json.dumps(
                {
                    "selected": [
                        {"id": 0, "source": "S"},
                        {"id": 0, "title": "missing-source"},
                    ]
                }
            )
        )

        with caplog.at_level(logging.WARNING):
            items = await filter_articles(
                sample_filter_articles,
                llm,
                filter_snippet_length=500,
                output_language="zh-Hant",
                style_prompt="",
            )

        assert items == []
        assert len(caplog.records) == 2

    async def test_filter_skips_non_dict_entry_and_keeps_valid_entry(
        self, sample_filter_articles, caplog
    ):
        llm = FakeLLM(
            json.dumps(
                {
                    "selected": [
                        "just-a-headline-string",
                        {"id": 0, "title": "ok", "source": "S"},
                    ]
                }
            )
        )

        with caplog.at_level(logging.WARNING):
            items = await filter_articles(
                sample_filter_articles,
                llm,
                filter_snippet_length=500,
                output_language="zh-Hant",
                style_prompt="",
            )

        assert [item.title for item in items] == ["ok"]
        assert len(caplog.records) == 1

    async def test_filter_skips_wrongly_typed_entry_and_keeps_valid_entry(
        self, sample_filter_articles, caplog
    ):
        llm = FakeLLM(
            json.dumps(
                {
                    "selected": [
                        {"id": [0], "title": "x", "source": "S"},
                        {"id": 0, "title": 7, "source": "S"},
                        {"id": 0, "title": "x", "source": None},
                        {"id": 0, "title": "ok", "source": "S"},
                    ]
                }
            )
        )

        with caplog.at_level(logging.WARNING):
            items = await filter_articles(
                sample_filter_articles,
                llm,
                filter_snippet_length=500,
                output_language="zh-Hant",
                style_prompt="",
            )

        assert [item.title for item in items] == ["ok"]
        assert len(caplog.records) == 3

    async def test_filter_empty_input(self):
        items = await filter_articles(
            [], FakeLLM(), filter_snippet_length=500, output_language="zh-Hant", style_prompt=""
        )
        assert items == []

    async def test_filter_nothing_noteworthy(self, sample_filter_articles):
        llm = FakeLLM(json.dumps({"selected": []}))

        items = await filter_articles(
            sample_filter_articles,
            llm,
            filter_snippet_length=500,
            output_language="zh-Hant",
            style_prompt="",
        )

        assert items == []

    async def test_filter_articles_passes_temperature(self, sample_filter_articles):
        """filter_articles should pass temperature=1.0 to the LLM."""
        llm = FakeLLM(json.dumps({"selected": []}))

        await filter_articles(
            sample_filter_articles,
            llm,
            filter_snippet_length=500,
            output_language="zh-Hant",
            style_prompt="",
        )

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
            json.dumps({"selected": [{"id": 0, "title": "Newsletter", "source": "Newsletter"}]})
        )

        item = (
            await filter_articles(
                [article],
                llm,
                filter_snippet_length=500,
                output_language="zh-Hant",
                style_prompt="",
            )
        )[0]

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
        llm = FakeLLM(json.dumps({"selected": [{"id": 0, "title": "RSS", "source": "RSS"}]}))

        item = (
            await filter_articles(
                [article],
                llm,
                filter_snippet_length=500,
                output_language="zh-Hant",
                style_prompt="",
            )
        )[0]

        assert item.ref_urls == []
        assert item.urls == ["https://ex.com/2"]


class _NumericTailLLM(FakeLLM):
    """Selects every article it was shown, but writes each id as the integer tail
    of the one in the prompt, the way @cf/openai/gpt-oss-120b rewrote
    'CNA/2026-09-17/202609170255' as 255 when clustering on 2026-09-17."""

    async def complete(self, prompt, **kwargs):
        ids = re.findall(r"^\[(.+?)\]", prompt, flags=re.MULTILINE)
        tails = [int(re.search(r"(\d{1,4})$", i).group(1)) for i in ids]
        self._responses = [
            json.dumps({"selected": [{"id": t, "title": f"t{t}", "source": "S"} for t in tails]})
        ]
        return await super().complete(prompt, **kwargs)


def _cna_articles() -> list[Article]:
    return [
        Article(
            id=f"CNA/2026-09-17/2026091702{n}",
            title=f"Article {n}",
            url=f"https://www.cna.com.tw/news/{n}",
            content="Content",
            published_at=datetime(2026, 9, 17, 10, 0, tzinfo=UTC),
            source_name="中央社即時新聞 財經新聞",
            source_tier=Tier.FILTER,
        )
        for n in ("55", "47")
    ]


async def test_filter_survives_a_model_that_rewrites_string_ids():
    articles = _cna_articles()

    items = await filter_articles(
        articles,
        _NumericTailLLM(),
        filter_snippet_length=500,
        output_language="zh-Hant",
        style_prompt="",
    )

    assert [item.urls for item in items] == [[a.url] for a in articles]


@pytest.mark.parametrize("raw_id", ["1", pytest.param("0" * 4301 + "1", id="'0'*4301+'1'")])
async def test_filter_resolves_a_position_echoed_as_a_string(raw_id):
    articles = _cna_articles()
    llm = FakeLLM(json.dumps({"selected": [{"id": raw_id, "title": "t", "source": "S"}]}))

    items = await filter_articles(
        articles, llm, filter_snippet_length=500, output_language="zh-Hant", style_prompt=""
    )

    assert items[0].urls == [articles[1].url]


@pytest.mark.parametrize(
    "raw_id", [True, "²", "1.0", 2, -1, pytest.param("9" * 4301, id="'9'*4301")], ids=repr
)
async def test_an_id_naming_no_position_links_nothing(raw_id, caplog):
    llm = FakeLLM(json.dumps({"selected": [{"id": raw_id, "title": "t", "source": "S"}]}))

    with caplog.at_level(logging.WARNING):
        items = await filter_articles(
            _cna_articles(),
            llm,
            filter_snippet_length=500,
            output_language="zh-Hant",
            style_prompt="",
        )

    assert items[0].urls == []
    assert len(caplog.records) == 1  # a model that rewrites ids is visible in the log
