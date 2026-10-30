"""Tests for DigestPipeline."""

import hashlib
import json
import re
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fakes import TEST_SETTINGS, FakeLLM, pipeline_settings

from cyris.adapters.output.html_digest import HtmlDigestWriter
from cyris.domain.models import Article, DigestItem, DigestSection, Lane
from cyris.domain.selection import digest_urls, split_read_lane_by_score
from cyris.service_layer.digest_pipeline import DigestPipeline

pytestmark = pytest.mark.integration


def _expected_story_id(date: str, period: str, urls: list[str]) -> str:
    """Computed independently of the implementation: the id formula is a contract."""
    return f"{date}-{period}-" + hashlib.sha1("\n".join(sorted(urls)).encode()).hexdigest()[:8]


class TestDigestPipeline:
    @pytest.fixture
    def pipeline(self):
        return DigestPipeline(FakeLLM(), **pipeline_settings())

    async def test_process_mixed_articles(
        self,
        pipeline,
        sample_filter_articles,
        sample_summarize_articles,
        sample_sources,
    ):
        mock_filter_result = [
            DigestItem(
                title="Apple Vision Pro 第二代",
                summary="價格降至 $2499",
                sources=["TechCrunch"],
                urls=["https://techcrunch.com/2026/03/16/apple-vision-pro-2"],
            )
        ]
        mock_summarize_result = [
            DigestSection(
                heading="AI 趨勢",
                items=[
                    DigestItem(
                        title="AI regulation",
                        summary="歐盟 AI 法案推進",
                        sources=["Stratechery"],
                        urls=["https://stratechery.com/2026/03/16/weekly-trends"],
                    )
                ],
            )
        ]

        all_articles = sample_filter_articles + sample_summarize_articles

        with (
            patch(
                "cyris.service_layer.digest_pipeline.filter_articles",
                new_callable=AsyncMock,
                return_value=mock_filter_result,
            ),
            patch(
                "cyris.service_layer.digest_pipeline.summarize_articles",
                new_callable=AsyncMock,
                return_value=mock_summarize_result,
            ),
        ):
            result = await pipeline.process(
                all_articles,
                sample_sources,
                period="morning",
                timezone=TEST_SETTINGS["general.timezone"],
            )

        assert result.content.period == "morning"
        assert result.content.articles_received == 3
        assert result.content.sources_processed == 3
        assert result.content.articles_included == 2  # 1 filtered + 1 summarized
        assert len(result.content.filtered_headlines) == 1
        assert result.content.thematic_summaries == []
        assert [len(s.items) for s in result.content.featured_articles] == [1]
        # Check URL classification
        assert "https://techcrunch.com/2026/03/16/apple-vision-pro-2" in result.accepted_urls
        assert "https://stratechery.com/2026/03/16/weekly-trends" in result.accepted_urls
        assert "https://reuters.com/2026/03/16/tsmc-arizona" in result.rejected_urls

    @pytest.mark.parametrize("fan_articles", [0, 1], ids=["no-articles", "one-article"])
    async def test_the_content_records_the_language_it_was_written_in(
        self, sample_sources, fan_articles
    ):
        pipeline = DigestPipeline(FakeLLM(), **pipeline_settings(output_language="ja"))
        articles = [
            Article(
                id=90,
                title="Weekly",
                url="https://group.example/12",
                content="Notes.",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                source_name="Fan source",
                source_lane=Lane.FOLLOW,
            )
        ][:fan_articles]

        result = await pipeline.process(
            articles, sample_sources, period="morning", timezone=TEST_SETTINGS["general.timezone"]
        )

        assert result.content.output_language == "ja"

    async def test_follow_lane_passthrough(self, pipeline, sample_sources):
        """Follow-lane articles bypass LLM, group by source, and are never discarded."""
        fan_articles = [
            Article(
                id=90,
                title="社團週報 #12",
                url="https://group.example/12",
                content="<p>本週活動整理與公告內容……</p>",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                source_name="某社團電子報",
                source_lane=Lane.FOLLOW,
            ),
            Article(
                id=91,
                title="社團週報 #13",
                url="https://group.example/13",
                content="下週活動預告",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                source_name="某社團電子報",
                source_lane=Lane.FOLLOW,
            ),
        ]

        # No filter/summarize mocks needed — follow lane never reaches the LLM path.
        result = await pipeline.process(
            fan_articles,
            sample_sources,
            period="morning",
            timezone=TEST_SETTINGS["general.timezone"],
        )

        assert len(result.content.fan_sections) == 1  # grouped by the single source
        section = result.content.fan_sections[0]
        assert section.heading == "某社團電子報"
        assert len(section.items) == 2
        assert section.items[0].summary == "本週活動整理與公告內容……"  # HTML-stripped excerpt
        # Both fan URLs accepted, none rejected
        assert set(result.accepted_urls) == {"https://group.example/12", "https://group.example/13"}
        assert result.rejected_urls == []
        assert result.content.articles_included == 2

    async def test_story_records_name_every_cluster_with_its_full_membership(self, sample_sources):
        """Story records carry every cluster's full URL list."""
        news = [
            Article(
                id=i,
                title=f"News {i}",
                url=f"https://news.example/{i}",
                content="News content",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                source_name="Wire",
                source_lane=Lane.SKIM,
                source_tags=["news"],
            )
            for i in (1, 2, 3)
        ]
        pipeline = DigestPipeline(
            FakeLLM(
                '{"clusters": ['
                '{"heading": "A", "summary": "S", "article_ids": [0, 1], "tags": []}, '
                '{"heading": "B", "summary": "S", "article_ids": [2], "tags": []}]}'
            ),
            **pipeline_settings(),
        )

        result = await pipeline.process(
            news, sample_sources, period="morning", timezone=TEST_SETTINGS["general.timezone"]
        )

        assert len(result.content.news_clusters) == 2
        # The records name both stories with their full memberships, under
        # content-derived ids a re-run of the same window would reproduce.
        date = result.content.date
        assert [r.id for r in result.story_records] == [
            _expected_story_id(
                date, "morning", ["https://news.example/1", "https://news.example/2"]
            ),
            _expected_story_id(date, "morning", ["https://news.example/3"]),
        ]
        assert result.story_records[0].urls == ["https://news.example/1", "https://news.example/2"]
        assert result.story_records[1].urls == ["https://news.example/3"]

    async def test_the_issue_counts_every_article_the_page_shows_a_topic_of_two_as_two(
        self, sample_sources, tmp_path
    ):
        news = [
            Article(
                id=i,
                title=f"News {i}",
                url=f"https://news.example/{i}",
                content="News content",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                source_name="Wire",
                source_lane=Lane.SKIM,
                source_tags=["news"],
            )
            for i in (1, 2, 3)
        ]
        pipeline = DigestPipeline(
            FakeLLM(
                '{"clusters": ['
                '{"heading": "A", "summary": "S", "article_ids": [0, 1], "tags": []}, '
                '{"heading": "B", "summary": "S", "article_ids": [2], "tags": []}]}'
            ),
            **pipeline_settings(),
        )

        result = await pipeline.process(
            news, sample_sources, period="morning", timezone=TEST_SETTINGS["general.timezone"]
        )

        html = HtmlDigestWriter(tmp_path).render(result.content)
        on_page = {
            url for group in re.findall(r"data-urls='([^']*)'", html) for url in json.loads(group)
        }
        assert len(result.content.news_clusters[0].items[0].urls) == 2
        assert result.content.articles_included == len(on_page) == 3
        assert on_page == digest_urls(result.content)

    async def test_rendered_story_id_matches_persisted_record(self, sample_sources, tmp_path):
        """Seam: the id the digest renders is the id the story store persists."""
        news = [
            Article(
                id=i,
                title=f"News {i}",
                url=f"https://news.example/{i}",
                content="News content",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                source_name="Wire",
                source_lane=Lane.SKIM,
                source_tags=["news"],
            )
            for i in (1, 2, 3)
        ]
        pipeline = DigestPipeline(
            FakeLLM(
                '{"clusters": ['
                '{"heading": "A", "summary": "S", "article_ids": [0, 1], "tags": []}, '
                '{"heading": "B", "summary": "S", "article_ids": [2], "tags": []}]}'
            ),
            **pipeline_settings(),
        )

        result = await pipeline.process(
            news, sample_sources, period="morning", timezone=TEST_SETTINGS["general.timezone"]
        )

        # (a) Sections and records carry the same ids, one-to-one, in order.
        assert [s.story_id for s in result.content.news_clusters] == [
            r.id for r in result.story_records
        ]

        # (b) The rendered page exposes each of those ids as data-story-id, verbatim.
        writer = HtmlDigestWriter(tmp_path)
        html = writer.render(result.content)
        for record in result.story_records:
            assert f'data-story-id="{record.id}"' in html

    async def test_process_no_articles(self, pipeline, sample_sources):
        result = await pipeline.process(
            [], sample_sources, period="evening", timezone=TEST_SETTINGS["general.timezone"]
        )

        assert result.content.articles_received == 0
        assert result.content.articles_included == 0
        assert result.content.filtered_headlines == []
        assert result.content.thematic_summaries == []
        assert result.accepted_urls == []
        assert result.rejected_urls == []


def _article(article_id: int, lane: Lane, source_name: str) -> Article:
    return Article(
        id=article_id,
        title=f"Article {article_id}",
        url=f"https://example.com/{article_id}",
        content="Body.",
        published_at=datetime(2026, 4, 10, tzinfo=UTC),
        source_name=source_name,
        source_lane=lane,
    )


def _item(article: Article) -> DigestItem:
    return DigestItem(
        title=article.title,
        summary="Summary.",
        sources=[article.source_name],
        urls=[article.url],
    )


class TestVerdictsFollowTheDigest:
    """Accepted means shown in this issue; what the issue left out stays pending."""

    async def _process(self, articles, sample_sources, *, headlines, sections):
        pipeline = DigestPipeline(FakeLLM(), **pipeline_settings())
        with (
            patch(
                "cyris.service_layer.digest_pipeline.filter_articles",
                new_callable=AsyncMock,
                return_value=headlines,
            ),
            patch(
                "cyris.service_layer.digest_pipeline.summarize_articles",
                new_callable=AsyncMock,
                return_value=sections,
            ),
        ):
            return await pipeline.process(
                articles, sample_sources, timezone=TEST_SETTINGS["general.timezone"]
            )

    async def test_every_headline_the_filter_kept_is_accepted_and_its_verdict_stands(
        self, sample_sources
    ):
        kept, also_kept, discarded = (_article(i, Lane.SKIM, "TechCrunch") for i in (1, 2, 3))

        result = await self._process(
            [kept, also_kept, discarded],
            sample_sources,
            headlines=[_item(kept), _item(also_kept)],
            sections=[],
        )

        assert sorted(result.accepted_urls) == sorted([kept.url, also_kept.url])
        assert result.rejected_urls == [discarded.url]

    async def test_no_output_cap_leaves_a_summarized_or_filtered_article_out(
        self, sample_sources, tmp_path
    ):
        read = [_article(i, Lane.READ, "Stratechery") for i in range(1, 31)]
        skim = [_article(i, Lane.SKIM, "TechCrunch") for i in range(31, 71)]

        result = await self._process(
            [*read, *skim],
            sample_sources,
            headlines=[_item(a) for a in skim],
            sections=[DigestSection(heading="Theme", items=[_item(a) for a in read])],
        )

        assert sorted(result.accepted_urls) == sorted(a.url for a in [*read, *skim])
        assert result.content.articles_included == 70
        assert len(result.content.featured_articles) == 30
        assert result.rejected_urls == []
        # The page votes on every URL the issue kept, each once.
        html = HtmlDigestWriter(tmp_path).render(result.content)
        voted = [
            url for group in re.findall(r"data-urls='([^']*)'", html) for url in json.loads(group)
        ]
        assert sorted(voted) == sorted(digest_urls(result.content))
        assert len(voted) == 70

    async def test_a_summarize_article_no_section_names_is_neither_accepted_nor_rejected(
        self, sample_sources
    ):
        named, left_out = (_article(i, Lane.READ, "Stratechery") for i in (1, 2))

        result = await self._process(
            [named, left_out],
            sample_sources,
            headlines=[],
            sections=[DigestSection(heading="Theme", items=[_item(named)])],
        )

        assert result.accepted_urls == [named.url]
        assert result.rejected_urls == []


class TestSplitReadLaneByScore:
    def test_all_above_threshold(self):
        articles = [
            Article(
                id=1,
                title="High Score 1",
                url="https://example.com/1",
                content="Content",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                source_name="Source",
                source_lane=Lane.READ,
            ),
            Article(
                id=2,
                title="High Score 2",
                url="https://example.com/2",
                content="Content",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                source_name="Source",
                source_lane=Lane.READ,
            ),
        ]
        article_scores = {
            "https://example.com/1": 80.0,
            "https://example.com/2": 75.0,
        }

        high, low = split_read_lane_by_score(articles, article_scores, threshold=70)

        assert len(high) == 2
        assert len(low) == 0

    def test_mixed_scores(self):
        articles = [
            Article(
                id=1,
                title="High",
                url="https://example.com/1",
                content="Content",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                source_name="Source",
                source_lane=Lane.READ,
            ),
            Article(
                id=2,
                title="Low 1",
                url="https://example.com/2",
                content="Content",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                source_name="Source",
                source_lane=Lane.READ,
            ),
            Article(
                id=3,
                title="Low 2",
                url="https://example.com/3",
                content="Content",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                source_name="Source",
                source_lane=Lane.READ,
            ),
        ]
        article_scores = {
            "https://example.com/1": 80.0,
            "https://example.com/2": 55.0,
            "https://example.com/3": 65.0,
        }

        high, low = split_read_lane_by_score(articles, article_scores, threshold=70)

        assert len(high) == 1
        assert high[0].title == "High"
        assert len(low) == 2
        assert low[0].title == "Low 1"
        assert low[1].title == "Low 2"

    def test_missing_scores_default_to_high(self):
        articles = [
            Article(
                id=1,
                title="Has Score",
                url="https://example.com/1",
                content="Content",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                source_name="Source",
                source_lane=Lane.READ,
            ),
            Article(
                id=2,
                title="No Score",
                url="https://example.com/2",
                content="Content",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                source_name="Source",
                source_lane=Lane.READ,
            ),
        ]
        article_scores = {
            "https://example.com/1": 55.0,
        }

        high, low = split_read_lane_by_score(articles, article_scores, threshold=70)

        assert len(high) == 1
        assert high[0].title == "No Score"
        assert len(low) == 1
        assert low[0].title == "Has Score"

    def test_none_article_scores_all_go_to_high(self):
        articles = [
            Article(
                id=1,
                title="Article 1",
                url="https://example.com/1",
                content="Content",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                source_name="Source",
                source_lane=Lane.READ,
            ),
            Article(
                id=2,
                title="Article 2",
                url="https://example.com/2",
                content="Content",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                source_name="Source",
                source_lane=Lane.READ,
            ),
        ]

        high, low = split_read_lane_by_score(articles, None, threshold=70)

        assert len(high) == 2
        assert len(low) == 0

    def test_empty_articles(self):
        high, low = split_read_lane_by_score([], {}, threshold=70)

        assert high == []
        assert low == []

    def test_score_exactly_at_threshold(self):
        articles = [
            Article(
                id=1,
                title="Exact",
                url="https://example.com/1",
                content="Content",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                source_name="Source",
                source_lane=Lane.READ,
            ),
        ]
        article_scores = {"https://example.com/1": 70.0}

        high, low = split_read_lane_by_score(articles, article_scores, threshold=70)

        assert len(high) == 1
        assert len(low) == 0


def _scored_topic() -> tuple[list[Article], dict[str, float], FakeLLM]:
    """Three read-lane articles on one tag, and a model that writes both summary kinds."""
    articles = [
        Article(
            id=n,
            title=f"Essay {n}",
            url=f"https://essays.example/{n}",
            content=f"Essay {n} body.",
            published_at=datetime(2026, 4, 10, tzinfo=UTC),
            source_name="Stratechery",
            source_lane=Lane.READ,
            source_tags=["tech"],
        )
        for n in range(3)
    ]
    scores = {"https://essays.example/0": 90, "https://essays.example/1": 80}
    scores["https://essays.example/2"] = 60
    llm = FakeLLM(
        json.dumps(
            {
                "sections": [
                    {
                        "heading": "Topic heading",
                        "summary": "The group summary.",
                        "summaries": {str(n): f"Essay {n} on its own." for n in range(3)},
                        "article_ids": [0, 1, 2],
                    }
                ]
            }
        )
    )
    return articles, scores, llm


async def _page(sample_sources, tmp_path, **settings) -> str:
    articles, scores, llm = _scored_topic()
    pipeline = DigestPipeline(llm, **pipeline_settings(summarize_score_threshold=0, **settings))
    result = await pipeline.process(
        articles,
        sample_sources,
        timezone=TEST_SETTINGS["general.timezone"],
        article_scores=scores,
    )
    html = HtmlDigestWriter(tmp_path).render(result.content)
    return html[html.index("<main>") : html.index("</main>")]


class TestTheLayeringSettingsReachThePage:
    @pytest.mark.parametrize("featured_threshold", [70, 85], ids=["grouped", "single"])
    async def test_every_essay_is_its_own_card_whether_or_not_the_top_story_groups(
        self, sample_sources, tmp_path, featured_threshold
    ):
        page = await _page(sample_sources, tmp_path, featured_threshold=featured_threshold)

        assert "The group summary." not in page
        assert "Topic heading" not in page
        assert page.count('class="featured-item"') == 3
        for n in range(3):
            assert f"Essay {n} on its own." in page


def _scored_item(article: Article, score: float, title: str | None = None) -> DigestItem:
    return _item(article).model_copy(update={"title": title or article.title, "score": score})


class TestKeptItemsReachThePreference:
    """The vote order scores what this issue kept, in the articles' fetched text."""

    @staticmethod
    def _kept_issue():
        h1, h2, h3 = (_article(i, Lane.SKIM, "TechCrunch") for i in (1, 2, 3))
        s1, s2 = (_article(i, Lane.READ, "Stratechery") for i in (4, 5))
        h1.title, h2.title = "Original one", "Original two"
        translated = [_item(h1).model_copy(update={"title": "譯一"})]
        translated.append(_item(h2).model_copy(update={"title": "譯二"}))
        sections = [
            DigestSection(heading="Theme", items=[_scored_item(s1, 90), _scored_item(s2, 80)])
        ]
        return [h1, h2, h3, s1, s2], translated, sections

    async def _process(self, articles, sample_sources, *, headlines, sections, preference, news=()):
        pipeline = DigestPipeline(
            FakeLLM(),
            **pipeline_settings(summarize_score_threshold=0, featured_threshold=100),
        )
        with (
            patch(
                "cyris.service_layer.digest_pipeline.filter_articles",
                new_callable=AsyncMock,
                return_value=headlines,
            ),
            patch(
                "cyris.service_layer.digest_pipeline.summarize_articles",
                new_callable=AsyncMock,
                return_value=sections,
            ),
            patch(
                "cyris.service_layer.digest_pipeline.cluster_news",
                new_callable=AsyncMock,
                return_value=(list(news), []),
            ),
        ):
            return await pipeline.process(
                articles,
                sample_sources,
                timezone=TEST_SETTINGS["general.timezone"],
                article_scores={a.url: 1.0 for a in articles},
                preference=preference,
            )

    async def test_the_preference_sees_the_kept_headlines_and_features_in_original_text(
        self, sample_sources
    ):
        articles, headlines, sections = self._kept_issue()
        asked: list[list[Article]] = []

        async def preference(kept):
            asked.append(kept)
            return None

        await self._process(
            articles, sample_sources, headlines=headlines, sections=sections, preference=preference
        )

        [kept] = asked
        assert [a.url for a in kept] == [articles[0].url, articles[1].url, articles[4].url]
        assert [a.title for a in kept] == ["Original one", "Original two", articles[4].title]

    async def test_every_cluster_member_reaches_the_preference(self, sample_sources):
        articles, headlines, sections = self._kept_issue()
        wires = [
            Article(
                id=9 + n,
                title=f"Wire story {n}",
                url=f"https://news.example/{9 + n}",
                content="News content",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                source_name="Wire",
                source_lane=Lane.SKIM,
                source_tags=["news"],
            )
            for n in range(2)
        ]
        cluster = DigestSection(heading="News", items=[_item(w) for w in wires])
        asked: list[list[Article]] = []

        async def preference(kept):
            asked.append(kept)
            return None

        await self._process(
            [*articles, *wires],
            sample_sources,
            headlines=headlines,
            sections=sections,
            preference=preference,
            news=[cluster],
        )

        [kept] = asked
        assert {w.url for w in wires} <= {a.url for a in kept}
        assert len(kept) == 5

    async def test_without_a_preference_nothing_is_asked_and_nothing_moves(self, sample_sources):
        articles, headlines, sections = self._kept_issue()

        result = await self._process(
            articles, sample_sources, headlines=headlines, sections=sections, preference=None
        )

        assert result.preference_moves is None


class TestThePreferenceOrdersTheNews:
    """The news clusters and headlines follow the vote preference, as the Features do."""

    async def _headlines(self, sample_sources, scores):
        h1, h2 = (_article(i, Lane.SKIM, "TechCrunch") for i in (1, 2))

        async def preference(_kept):
            return scores

        pipeline = DigestPipeline(FakeLLM(), **pipeline_settings())
        with patch(
            "cyris.service_layer.digest_pipeline.filter_articles",
            new_callable=AsyncMock,
            return_value=[_item(h1), _item(h2)],
        ):
            result = await pipeline.process(
                [h1, h2],
                sample_sources,
                timezone=TEST_SETTINGS["general.timezone"],
                preference=preference,
            )
        return h1, h2, result

    async def test_the_headlines_follow_the_scores_and_the_moves_count_them(self, sample_sources):
        h1, h2 = (f"https://example.com/{i}" for i in (1, 2))
        _, _, scored = await self._headlines(sample_sources, {h1: 0.1, h2: 0.9})

        assert [i.title for i in scored.content.filtered_headlines] == ["Article 2", "Article 1"]
        assert scored.preference_moves == {"features": 0, "news": 2}

    async def test_no_scores_move_nothing_and_report_none(self, sample_sources):
        _, _, result = await self._headlines(sample_sources, None)

        assert [i.title for i in result.content.filtered_headlines] == ["Article 1", "Article 2"]
        assert result.preference_moves is None

    async def test_an_empty_mapping_still_reports(self, sample_sources):
        _, _, result = await self._headlines(sample_sources, {})

        assert result.preference_moves == {"features": 0, "news": 0}

    async def test_a_cluster_follows_its_best_members_score(self, sample_sources):
        wires = [
            _article(i, Lane.SKIM, "Wire").model_copy(update={"source_tags": ["news"]})
            for i in (1, 2, 3, 4)
        ]
        clusters = [
            DigestSection(heading="quiet", items=[_item(w) for w in wires[:2]]),
            DigestSection(heading="voted", items=[_item(w) for w in wires[2:]]),
        ]

        async def preference(_kept):
            return {"https://example.com/1": -0.2, "https://example.com/4": 0.6}

        pipeline = DigestPipeline(FakeLLM(), **pipeline_settings())
        with (
            patch(
                "cyris.service_layer.digest_pipeline.filter_articles",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "cyris.service_layer.digest_pipeline.cluster_news",
                new_callable=AsyncMock,
                return_value=(clusters, []),
            ),
        ):
            result = await pipeline.process(
                wires,
                sample_sources,
                timezone=TEST_SETTINGS["general.timezone"],
                preference=preference,
            )

        assert [s.heading for s in result.content.news_clusters] == ["voted", "quiet"]


class TestSummarizeFollow:
    """With the setting on, follow articles get the read lane's explanation; off, an excerpt."""

    @staticmethod
    def _follow():
        a, b = (_article(i, Lane.FOLLOW, "A group") for i in (21, 22))
        c = _article(23, Lane.FOLLOW, "A newsletter")
        return [a, b, c]

    @staticmethod
    def _llm():
        return FakeLLM(
            json.dumps(
                {
                    "sections": [
                        {
                            "heading": "Ignored heading",
                            "summary": "Ignored group line.",
                            "summaries": {"0": "Explains the first.", "1": "Explains the second."},
                            "article_ids": [0, 1],
                        }
                    ]
                }
            )
        )

    async def test_off_sends_no_follow_article_to_the_model(self, sample_sources):
        llm = self._llm()
        pipeline = DigestPipeline(llm, **pipeline_settings(summarize_follow=False))

        result = await pipeline.process(
            self._follow(), sample_sources, timezone=TEST_SETTINGS["general.timezone"]
        )

        assert llm.calls == []
        items = [i for s in result.content.fan_sections for i in s.items]
        assert [i.summary for i in items] == ["Body."] * 3
        assert all(i.passed_through for i in items)

    async def test_on_explains_each_source_and_keeps_the_sections_by_source(self, sample_sources):
        llm = self._llm()
        pipeline = DigestPipeline(llm, **pipeline_settings(summarize_follow=True))

        result = await pipeline.process(
            self._follow(), sample_sources, timezone=TEST_SETTINGS["general.timezone"]
        )

        assert [c["prompt"].splitlines()[0] for c in llm.calls] == [
            "Topic group: A group",
            "Topic group: A newsletter",
        ]
        assert all("never analyse" in c["system"] for c in llm.calls)
        sections = result.content.fan_sections
        assert [s.heading for s in sections] == ["A group", "A newsletter"]
        group, newsletter = sections
        assert [(i.summary, i.passed_through) for i in group.items] == [
            ("Explains the first.", False),
            ("Explains the second.", False),
        ]
        # The newsletter's call explained ids 0 and 1, of which it has only 0.
        assert [(i.summary, i.passed_through) for i in newsletter.items] == [
            ("Explains the first.", False)
        ]

    async def test_on_keeps_the_excerpt_where_the_model_wrote_nothing(self, sample_sources):
        llm = FakeLLM(error=RuntimeError("down"))
        pipeline = DigestPipeline(llm, **pipeline_settings(summarize_follow=True))

        result = await pipeline.process(
            self._follow(), sample_sources, timezone=TEST_SETTINGS["general.timezone"]
        )

        items = [i for s in result.content.fan_sections for i in s.items]
        assert [(i.summary, i.passed_through) for i in items] == [("Body.", True)] * 3
        assert result.content.usage.fell_back_to_excerpts
