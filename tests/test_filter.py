"""Tests for skim-lane processor."""

import json
import logging
import re
from datetime import UTC, datetime

import pytest
from fakes import FakeLLM

from cyris.domain.models import Article, Lane, UsageStats
from cyris.service_layer import filtering
from cyris.service_layer.filtering import BATCH_SIZE, filter_articles

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

    @pytest.mark.parametrize(
        ("llm", "passed_through"),
        [
            (None, True),
            (FakeLLM(error=RuntimeError("quota")), True),
            (
                FakeLLM(
                    json.dumps(
                        {"selected": [{"id": 0, "title": "T", "summary": "S", "source": "X"}]}
                    )
                ),
                False,
            ),
        ],
        ids=["no-client", "call-failed", "answered"],
    )
    async def test_only_a_fallback_item_says_its_text_is_the_articles_own(
        self, sample_filter_articles, llm, passed_through
    ):
        items = await filter_articles(
            sample_filter_articles,
            llm,
            filter_snippet_length=500,
            output_language="zh-Hant",
            style_prompt="",
        )

        assert items
        assert {item.passed_through for item in items} == {passed_through}

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
            source_lane=Lane.SKIM,
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
            source_lane=Lane.SKIM,
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
            source_lane=Lane.SKIM,
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


def _articles(n: int) -> list[Article]:
    return [
        Article(
            id=f"a{i}",
            title=f"Article {i}",
            url=f"https://ex.com/{i}",
            content="Content",
            published_at=datetime(2026, 9, 17, 10, 0, tzinfo=UTC),
            source_name="S",
            source_lane=Lane.SKIM,
        )
        for i in range(n)
    ]


def _selecting(*ids, title="t"):
    return json.dumps({"selected": [{"id": i, "title": title, "source": "S"} for i in ids]})


class _FailingOnCalls(FakeLLM):
    """FakeLLM whose calls at the indexes in `fail_on` raise; the others answer in order."""

    def __init__(self, responses, fail_on=(), **kwargs):
        super().__init__(responses, **kwargs)
        self._fail_on = set(fail_on)

    async def complete(self, prompt, **kwargs):
        index = len(self.calls)
        if index in self._fail_on:
            self.calls.append({"prompt": prompt, **kwargs})
            raise RuntimeError(f"call {index} failed")
        return await super().complete(prompt, **kwargs)


async def _run(articles, llm, usage=None, article_scores=None):
    return await filter_articles(
        articles,
        llm,
        usage,
        article_scores,
        filter_snippet_length=500,
        output_language="zh-Hant",
        style_prompt="",
    )


def _filter_messages(caplog, prefix):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(prefix)]


class TestFilterBatchCountLog:
    async def test_log_names_article_and_batch_counts(self, caplog):
        with caplog.at_level(logging.INFO, logger=filtering.logger.name):
            await _run(_articles(2 * BATCH_SIZE + 1), FakeLLM(_selecting()))

        assert _filter_messages(caplog, "Filtering") == [
            f"Filtering {2 * BATCH_SIZE + 1} articles through the LLM (batches: 3)"
        ]

    async def test_single_batch_says_one(self, caplog):
        with caplog.at_level(logging.INFO, logger=filtering.logger.name):
            await _run(_articles(BATCH_SIZE), FakeLLM(_selecting()))

        assert _filter_messages(caplog, "Filtering")[0].endswith("(batches: 1)")

    async def test_no_llm_logs_no_filtering_line(self, caplog):
        with caplog.at_level(logging.INFO, logger=filtering.logger.name):
            await _run(_articles(3), None)

        assert _filter_messages(caplog, "Filtering") == []


class TestFilterBatchFallback:
    async def test_failed_last_batch_falls_back_and_the_first_is_judged(self):
        articles = _articles(BATCH_SIZE + 3)
        llm = _FailingOnCalls([_selecting(0, title="first")], fail_on={1})
        usage = UsageStats(model="m")

        items = await _run(articles, llm, usage)

        assert [(i.urls, i.passed_through) for i in items] == [
            ([articles[0].url], False),
            *[([articles[BATCH_SIZE + k].url], True) for k in range(3)],
        ]
        assert usage.fell_back_to_excerpts is True
        assert len(llm.calls) == 2

    async def test_failed_second_of_three_keeps_first_and_third(self):
        articles = _articles(2 * BATCH_SIZE + 1)
        llm = _FailingOnCalls(
            [_selecting(0, title="first"), _selecting(0, title="third")], fail_on={1}
        )

        items = await _run(articles, llm)

        assert items[0].urls == [articles[0].url]
        assert items[0].passed_through is False
        assert [i.urls for i in items[1 : 1 + BATCH_SIZE]] == [
            [a.url] for a in articles[BATCH_SIZE : 2 * BATCH_SIZE]
        ]
        assert all(i.passed_through for i in items[1 : 1 + BATCH_SIZE])
        assert items[-1].urls == [articles[2 * BATCH_SIZE].url]
        assert items[-1].passed_through is False
        assert len(llm.calls) == 3

    async def test_failed_first_batch_keeps_second(self):
        articles = _articles(2 * BATCH_SIZE)
        llm = _FailingOnCalls([_selecting(0)], fail_on={0})

        items = await _run(articles, llm)

        assert [i.urls for i in items[:BATCH_SIZE]] == [[a.url] for a in articles[:BATCH_SIZE]]
        assert all(i.passed_through for i in items[:BATCH_SIZE])
        assert items[BATCH_SIZE].urls == [articles[BATCH_SIZE].url]
        assert items[BATCH_SIZE].passed_through is False

    async def test_every_call_failing_passes_every_article_through(self, caplog):
        articles = _articles(2 * BATCH_SIZE + 1)
        llm = FakeLLM(error=RuntimeError("quota"))
        usage = UsageStats(model="m")

        with caplog.at_level(logging.WARNING):
            items = await _run(articles, llm, usage)

        assert len(items) == 2 * BATCH_SIZE + 1
        assert all(i.passed_through for i in items)
        assert len(llm.calls) == 3
        assert [r.levelname for r in caplog.records] == ["WARNING"] * 3
        assert all(r.exc_info for r in caplog.records)

    async def test_unparseable_reply_falls_back_only_its_batch(self):
        articles = _articles(2 * BATCH_SIZE)
        llm = FakeLLM([_selecting(0, title="first"), "not json"])

        items = await _run(articles, llm)

        assert items[0].urls == [articles[0].url]
        assert items[0].passed_through is False
        assert [i.urls for i in items[1:]] == [[a.url] for a in articles[BATCH_SIZE:]]
        assert all(i.passed_through for i in items[1:])

    async def test_no_llm_passes_everything_through_without_calls(self, caplog):
        usage = UsageStats(model="m")
        with caplog.at_level(logging.WARNING):
            items = await _run(_articles(2 * BATCH_SIZE + 1), None, usage)

        assert len(items) == 2 * BATCH_SIZE + 1
        assert all(i.passed_through for i in items)
        assert usage.fell_back_to_excerpts is True
        assert len(caplog.records) == 1

    async def test_no_failed_batch_leaves_usage_unmarked_and_logs_no_warning(self, caplog):
        usage = UsageStats(model="m")
        with caplog.at_level(logging.WARNING):
            await _run(_articles(2 * BATCH_SIZE + 1), FakeLLM(_selecting()), usage)

        assert usage.fell_back_to_excerpts is False
        assert caplog.records == []


class TestFilterBatchIdResolution:
    async def test_position_is_resolved_within_its_own_batch(self):
        articles = _articles(2 * BATCH_SIZE + 1)
        llm = FakeLLM([_selecting(), _selecting(3), _selecting()])

        items = await _run(articles, llm)

        assert len(items) == 1
        assert items[0].urls == [articles[BATCH_SIZE + 3].url]
        assert items[0].urls != [articles[3].url]

    async def test_last_batch_position_zero_links_its_first_article(self):
        articles = _articles(2 * BATCH_SIZE + 1)
        llm = FakeLLM([_selecting(), _selecting(), _selecting(0)])

        items = await _run(articles, llm)

        assert [i.urls for i in items] == [[articles[2 * BATCH_SIZE].url]]

    async def test_id_outside_batch_links_nothing_and_warns_once(self, caplog):
        llm = FakeLLM([_selecting(BATCH_SIZE), _selecting(), _selecting()])

        with caplog.at_level(logging.WARNING):
            items = await _run(_articles(2 * BATCH_SIZE + 1), llm)

        assert items[0].urls == []
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1

    async def test_score_comes_from_the_article_at_its_batch_position(self):
        articles = _articles(2 * BATCH_SIZE + 1)
        scores = {articles[BATCH_SIZE + 3].url: 0.7}
        llm = FakeLLM([_selecting(), _selecting(3), _selecting()])

        items = await _run(articles, llm, article_scores=scores)

        assert items[0].score == 0.7


class TestFilterBatchPartition:
    async def test_pool_goes_out_as_bounded_batches_with_one_prompt_shape(self):
        llm = FakeLLM(_selecting())

        await _run(_articles(2 * BATCH_SIZE + 1), llm)

        ids = [re.findall(r"^\[(\d+)\]", c["prompt"], flags=re.MULTILINE) for c in llm.calls]
        assert ids == [
            [str(i) for i in range(BATCH_SIZE)],
            [str(i) for i in range(BATCH_SIZE)],
            ["0"],
        ]
        assert {c["temperature"] for c in llm.calls} == {1.0}
        assert len({c["system"] for c in llm.calls}) == 1

    async def test_titles_arrive_once_each_in_input_order(self):
        articles = _articles(2 * BATCH_SIZE + 1)
        llm = FakeLLM(_selecting())

        await _run(articles, llm)

        titles = [
            t
            for c in llm.calls
            for t in re.findall(r"^\[\d+\] \(.*?\) (.*)$", c["prompt"], flags=re.MULTILINE)
        ]
        assert titles == [a.title for a in articles]

    @pytest.mark.parametrize(("count", "calls"), [(BATCH_SIZE, 1), (BATCH_SIZE + 1, 2)])
    async def test_call_count_follows_the_batch_boundary(self, count, calls):
        llm = FakeLLM(_selecting())

        await _run(_articles(count), llm)

        assert len(llm.calls) == calls

    async def test_batch_size_is_read_at_call_time(self, monkeypatch):
        monkeypatch.setattr(filtering, "BATCH_SIZE", 2)
        llm = FakeLLM(_selecting())

        await _run(_articles(5), llm)

        sizes = [len(re.findall(r"^\[\d+\]", c["prompt"], flags=re.MULTILINE)) for c in llm.calls]
        assert sizes == [2, 2, 1]

    async def test_usage_counts_every_batch_call(self):
        usage = UsageStats(model="m")

        await _run(_articles(2 * BATCH_SIZE + 1), FakeLLM(_selecting()), usage)

        assert usage.api_calls == 3
        assert usage.input_tokens == 1500
        assert usage.fell_back_to_excerpts is False

    async def test_empty_pool_makes_no_call(self):
        llm = FakeLLM(_selecting())

        assert await _run([], llm) == []
        assert len(llm.calls) == 0


class TestFilterPassRateLog:
    async def test_rate_counts_only_judged_articles_kept(self, caplog):
        llm = FakeLLM([_selecting(0), _selecting(), _selecting(0)])

        with caplog.at_level(logging.INFO, logger=filtering.logger.name):
            await _run(_articles(2 * BATCH_SIZE + 1), llm)

        assert _filter_messages(caplog, "Filter passed") == [
            f"Filter passed 2 / {2 * BATCH_SIZE + 1} articles "
            f"({2 / (2 * BATCH_SIZE + 1) * 100:.1f}%)"
        ]

    async def test_failed_batch_is_excluded_from_the_rate(self, caplog):
        llm = _FailingOnCalls([_selecting(0)], fail_on={1})

        with caplog.at_level(logging.INFO, logger=filtering.logger.name):
            await _run(_articles(2 * BATCH_SIZE), llm)

        assert _filter_messages(caplog, "Filter passed") == [
            f"Filter passed 1 / {BATCH_SIZE} articles ({100 / BATCH_SIZE:.1f}%)"
        ]

    async def test_no_rate_when_every_batch_failed(self, caplog):
        with caplog.at_level(logging.INFO, logger=filtering.logger.name):
            await _run(_articles(2 * BATCH_SIZE), FakeLLM(error=RuntimeError("quota")))

        assert _filter_messages(caplog, "Filter passed") == []

    async def test_single_batch_pool_rate(self, sample_filter_articles, caplog):
        llm = FakeLLM(_selecting(0))

        with caplog.at_level(logging.INFO, logger=filtering.logger.name):
            await _run(sample_filter_articles, llm)

        assert _filter_messages(caplog, "Filter passed") == ["Filter passed 1 / 2 articles (50.0%)"]
