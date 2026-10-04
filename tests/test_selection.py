"""Tests for digest article selection with priority fill."""

import pytest

from cyris.domain.models import DigestContent, DigestItem, DigestSection
from cyris.domain.selection import (
    _truncate_sections,
    count_dead_links,
    layer_by_score,
    select_digest_articles,
)

pytestmark = pytest.mark.unit


def _make_items(count: int, prefix: str = "item") -> list[DigestItem]:
    return [
        DigestItem(
            title=f"{prefix}-{i}",
            summary=f"Summary {i}",
            sources=[f"Source-{i}"],
            urls=[f"https://example.com/{prefix}-{i}"],
        )
        for i in range(count)
    ]


def _make_sections(item_counts: list[int], prefix: str = "section") -> list[DigestSection]:
    return [
        DigestSection(heading=f"{prefix}-{i}", items=_make_items(n, f"{prefix}-{i}"))
        for i, n in enumerate(item_counts)
    ]


def _base_content(**kwargs) -> DigestContent:
    defaults = {
        "date": "2026-04-01",
        "period": "morning",
        "sources_processed": 5,
        "articles_received": 50,
        "articles_included": 50,
    }
    defaults.update(kwargs)
    return DigestContent(**defaults)


class TestSelectDigestArticles:
    def test_empty_content(self):
        content = _base_content(articles_included=0)
        result = select_digest_articles(content, max_items=15)

        assert result.articles_included == 0
        assert result.news_clusters == []
        assert result.thematic_summaries == []
        assert result.filtered_headlines == []

    def test_under_cap_keeps_all(self):
        content = _base_content(
            news_clusters=_make_sections([2, 1], "news"),
            thematic_summaries=_make_sections([2, 2], "theme"),
            attention_sections=_make_sections([1], "attention"),
            filtered_headlines=_make_items(3, "headline"),
            articles_included=11,
        )
        result = select_digest_articles(content, max_items=15)

        assert len(result.news_clusters) == 2
        assert len(result.thematic_summaries) == 2
        assert len(result.attention_sections) == 1
        assert len(result.filtered_headlines) == 3
        assert result.articles_included == 11

    def test_priority_fill_summaries_first(self):
        content = _base_content(
            news_clusters=_make_sections([5, 5], "news"),
            thematic_summaries=_make_sections([3], "theme"),
            attention_sections=_make_sections([2], "attention"),
            filtered_headlines=_make_items(5, "headline"),
            articles_included=20,
        )
        result = select_digest_articles(content, max_items=15)

        cluster_count = sum(len(s.items) for s in result.news_clusters)
        summary_count = sum(len(s.items) for s in result.thematic_summaries)
        attention_count = sum(len(s.items) for s in result.attention_sections)
        assert summary_count == 3
        assert cluster_count == 10
        assert attention_count == 2
        assert len(result.filtered_headlines) == 0
        assert result.articles_included == 15

    def test_heavy_news_day_keeps_summaries(self):
        """Clusters alone exceed the cap: summaries survive, clusters get truncated."""
        content = _base_content(
            news_clusters=_make_sections([10, 10], "news"),
            thematic_summaries=_make_sections([5], "theme"),
            filtered_headlines=_make_items(5, "headline"),
            articles_included=30,
        )
        result = select_digest_articles(content, max_items=15)

        cluster_count = sum(len(s.items) for s in result.news_clusters)
        summary_count = sum(len(s.items) for s in result.thematic_summaries)
        assert summary_count == 5
        assert cluster_count == 10
        assert result.filtered_headlines == []
        assert result.articles_included == 15

    def test_clusters_exceed_remaining(self):
        content = _base_content(
            news_clusters=_make_sections([8, 8], "news"),
            thematic_summaries=_make_sections([3], "theme"),
            filtered_headlines=_make_items(5, "headline"),
            articles_included=24,
        )
        result = select_digest_articles(content, max_items=15)

        cluster_count = sum(len(s.items) for s in result.news_clusters)
        summary_count = sum(len(s.items) for s in result.thematic_summaries)
        assert summary_count == 3
        assert cluster_count == 12
        assert result.filtered_headlines == []
        assert result.articles_included == 15

    def test_summaries_alone_fill_cap(self):
        content = _base_content(
            news_clusters=_make_sections([2], "news"),
            thematic_summaries=_make_sections([5, 5, 5], "theme"),
            filtered_headlines=_make_items(10, "headline"),
            articles_included=27,
        )
        result = select_digest_articles(content, max_items=15)

        cluster_count = sum(len(s.items) for s in result.news_clusters)
        summary_count = sum(len(s.items) for s in result.thematic_summaries)
        assert summary_count == 15
        assert cluster_count == 0
        assert result.filtered_headlines == []
        assert result.articles_included == 15

    def test_section_truncation(self):
        """A section with more items than remaining slots gets truncated."""
        content = _base_content(
            news_clusters=[],
            thematic_summaries=_make_sections([10], "theme"),
            filtered_headlines=_make_items(5, "headline"),
            articles_included=15,
        )
        result = select_digest_articles(content, max_items=7)

        assert len(result.thematic_summaries) == 1
        assert len(result.thematic_summaries[0].items) == 7
        assert result.filtered_headlines == []

    def test_no_clusters_or_summaries(self):
        content = _base_content(
            news_clusters=[],
            thematic_summaries=[],
            filtered_headlines=_make_items(20, "headline"),
            articles_included=20,
        )
        result = select_digest_articles(content, max_items=15)

        assert len(result.filtered_headlines) == 15
        assert result.articles_included == 15

    def test_preserves_metadata(self):
        content = _base_content(
            news_clusters=_make_sections([2], "news"),
            filtered_headlines=_make_items(3, "headline"),
            articles_included=5,
        )
        result = select_digest_articles(content, max_items=15)

        assert result.date == "2026-04-01"
        assert result.period == "morning"
        assert result.sources_processed == 5
        assert result.articles_received == 50


def _article(title: str, score: float | None) -> DigestItem:
    """One summarized article, carrying its own summary as the summarize step now writes it."""
    return DigestItem(
        title=title,
        summary=f"{title} on its own",
        sources=[f"{title} source"],
        urls=[f"https://example.com/{title}"],
        score=score,
    )


def _topic(
    heading: str, *articles: DigestItem, summary: str | None = "Group summary"
) -> DigestSection:
    """A summarize-tier group; summary=None is the degraded shape, which has no group summary."""
    return DigestSection(heading=heading, summary=summary, items=list(articles))


def _layer(*sections: DigestSection, threshold: float = 70, max_featured: int = 5) -> DigestContent:
    content = _base_content(thematic_summaries=list(sections))
    return layer_by_score(content, featured_threshold=threshold, max_featured=max_featured)


def _titles(section: DigestSection) -> list[str]:
    return [item.title for item in section.items]


class TestLayerByScore:
    """The Top story is the only group; every Features card is one article."""

    def test_two_articles_on_one_topic_at_the_threshold_lead_as_one_group(self):
        result = _layer(_topic("Topic", _article("a", 80), _article("b", 70)))

        lead = result.featured_articles[0]
        assert lead.heading == "Topic"
        assert lead.summary == "Group summary"
        assert _titles(lead) == ["a", "b"]
        assert result.featured_articles[1:] == []
        assert result.thematic_summaries == []

    def test_one_article_above_the_threshold_is_no_group(self):
        result = _layer(_topic("Topic", _article("a", 80), _article("b", 69)))

        assert [_titles(s) for s in result.featured_articles] == [["a"], ["b"]]
        assert all(s.summary is None for s in result.featured_articles)

    def test_a_member_below_the_threshold_stays_in_the_group(self):
        result = _layer(_topic("Topic", _article("low", 50), _article("a", 90), _article("b", 75)))

        lead, *features = result.featured_articles
        assert _titles(lead) == ["low", "a", "b"]
        assert lead.summary == "Group summary"
        assert features == []

    def test_the_top_story_keeps_every_article_its_summary_covers(self):
        result = _layer(
            _topic("Topic", _article("a", 9), _article("b", 8), _article("c", 3)),
            _topic("Solo", _article("solo", 5)),
            threshold=7,
        )

        lead, *features = result.featured_articles
        assert lead.heading == "Topic"
        assert lead.summary == "Group summary"
        assert _titles(lead) == ["a", "b", "c"]
        assert [_titles(s) for s in features] == [["solo"]]

    def test_a_group_leads_even_when_a_single_article_scores_higher(self):
        result = _layer(
            _topic("Solo", _article("solo", 99)),
            _topic("Topic", _article("a", 80), _article("b", 75)),
        )

        assert _titles(result.featured_articles[0]) == ["a", "b"]
        assert [_titles(s) for s in result.featured_articles[1:]] == [["solo"]]

    def test_without_a_group_the_lead_is_the_highest_scoring_article(self):
        result = _layer(
            _topic("One", _article("mid", 75)),
            _topic("Two", _article("top", 95), _article("low", 40)),
        )

        assert [_titles(s) for s in result.featured_articles] == [["top"], ["mid"], ["low"]]

    def test_of_two_qualifying_groups_the_one_with_the_best_article_leads(self):
        result = _layer(
            _topic("Second", _article("s1", 85), _article("s2", 84)),
            _topic("First", _article("f1", 90), _article("f2", 71)),
        )

        lead, *features = result.featured_articles
        assert lead.heading == "First"
        assert [_titles(s) for s in features] == [["s1"], ["s2"]]

    def test_every_feature_is_one_article_with_its_own_summary(self):
        result = _layer(
            _topic("Topic", _article("a", 90), _article("b", 85)),
            _topic("Other", _article("c", 80), _article("d", 75)),
            threshold=95,
        )

        assert len(result.featured_articles) == 4
        for section in result.featured_articles:
            assert len(section.items) == 1
            assert section.summary is None
            assert section.items[0].summary == f"{section.items[0].title} on its own"

    def test_features_follow_score_and_unscored_articles_trail(self):
        result = _layer(
            _topic("Topic", _article("none", None), _article("b", 60), _article("a", 90)),
            threshold=95,
        )

        assert [_titles(s) for s in result.featured_articles] == [["a"], ["b"], ["none"]]

    def test_max_featured_caps_the_cards_and_leaves_the_rest_out(self):
        result = _layer(
            *(_topic(f"T{n}", _article(f"a{n}", 90 - n)) for n in range(5)),
            max_featured=2,
        )

        assert [_titles(s) for s in result.featured_articles] == [["a0"], ["a1"], ["a2"]]
        assert result.thematic_summaries == []

    def test_unscored_articles_never_count_toward_a_group(self):
        result = _layer(_topic("Topic", _article("a", 90), _article("b", None)))

        assert [_titles(s) for s in result.featured_articles] == [["a"], ["b"]]

    def test_a_degraded_section_never_groups(self):
        result = _layer(_topic("Topic", _article("a", 90), _article("b", 85), summary=None))

        assert [_titles(s) for s in result.featured_articles] == [["a"], ["b"]]

    @pytest.mark.parametrize("threshold", [70, 95], ids=["in-the-group", "as-a-feature"])
    def test_an_article_two_sections_name_is_drawn_once(self, threshold):
        result = _layer(
            _topic("Topic", _article("a", 90), _article("b", 85)),
            _topic("Again", _article("a", 90)),
            threshold=threshold,
        )

        titles = [t for s in result.featured_articles for t in _titles(s)]
        assert sorted(titles) == ["a", "b"]

    def test_an_issue_with_no_summarized_article_has_no_lead(self):
        result = _layer()

        assert result.featured_articles == []
        assert result.thematic_summaries == []


class TestTheCapKeepsTheLeadWhole:
    def _capped(self, *sections: DigestSection, max_items: int, **kwargs) -> DigestContent:
        content = _layer(*sections, **kwargs).model_copy(
            update={"news_clusters": _make_sections([2], "news")}
        )
        return select_digest_articles(content, max_items=max_items)

    def test_a_group_that_fits_goes_in_whole(self):
        result = self._capped(
            _topic("Topic", _article("a", 90), _article("b", 80)),
            _topic("Solo", _article("solo", 99)),
            max_items=3,
        )

        assert [_titles(s) for s in result.featured_articles] == [["a", "b"], ["solo"]]
        assert result.news_clusters == []
        assert result.articles_included == 3

    def test_a_group_that_does_not_fit_is_replaced_by_its_best_article(self):
        result = self._capped(
            _topic("Topic", _article("a", 90), _article("b", 85), _article("c", 80)),
            _topic("Solo", _article("solo", 70)),
            max_items=2,
        )

        lead, *features = result.featured_articles
        assert _titles(lead) == ["a"]
        assert lead.summary is None
        assert lead.items[0].summary == "a on its own"
        assert [_titles(s) for s in features] == [["solo"]]
        assert result.articles_included == 2

    def test_the_cap_takes_features_by_score(self):
        content = _base_content(
            featured_articles=[
                DigestSection(heading=t, items=[_article(t, s)])
                for t, s in (("lead", 99), ("low", 60), ("high", 90), ("mid", 75))
            ]
        )

        result = select_digest_articles(content, max_items=3)

        assert [_titles(s) for s in result.featured_articles] == [["lead"], ["high"], ["mid"]]

    def test_the_lead_and_features_come_before_news(self):
        result = self._capped(_topic("Topic", _article("a", 90), _article("b", 80)), max_items=3)

        assert [_titles(s) for s in result.featured_articles] == [["a", "b"]]
        assert sum(len(s.items) for s in result.news_clusters) == 1


class TestSelectDigestArticlesWithAttention:
    def test_attention_fills_after_thematic(self):
        """Test that attention sections fill after thematic summaries."""
        content = _base_content(
            news_clusters=_make_sections([2], "news"),
            thematic_summaries=_make_sections([3], "theme"),
            attention_sections=_make_sections([5], "attention"),
            filtered_headlines=_make_items(10, "headline"),
            articles_included=20,
        )
        result = select_digest_articles(content, max_items=15)

        cluster_count = sum(len(s.items) for s in result.news_clusters)
        summary_count = sum(len(s.items) for s in result.thematic_summaries)
        attention_count = sum(len(s.items) for s in result.attention_sections)
        assert cluster_count == 2
        assert summary_count == 3
        assert attention_count == 5
        assert len(result.filtered_headlines) == 5
        assert result.articles_included == 15

    def test_attention_exceeds_limit(self):
        """Test that attention sections get truncated if they exceed remaining slots."""
        content = _base_content(
            news_clusters=_make_sections([5], "news"),
            thematic_summaries=_make_sections([5], "theme"),
            attention_sections=_make_sections([8], "attention"),
            filtered_headlines=_make_items(5, "headline"),
            articles_included=23,
        )
        result = select_digest_articles(content, max_items=15)

        cluster_count = sum(len(s.items) for s in result.news_clusters)
        summary_count = sum(len(s.items) for s in result.thematic_summaries)
        attention_count = sum(len(s.items) for s in result.attention_sections)
        assert cluster_count == 5
        assert summary_count == 5
        assert attention_count == 5
        assert len(result.filtered_headlines) == 0
        assert result.articles_included == 15

    def test_no_attention_sections(self):
        """Test behavior when attention_sections is empty."""
        content = _base_content(
            news_clusters=_make_sections([3], "news"),
            thematic_summaries=_make_sections([5], "theme"),
            attention_sections=[],
            filtered_headlines=_make_items(10, "headline"),
            articles_included=18,
        )
        result = select_digest_articles(content, max_items=15)

        assert sum(len(s.items) for s in result.news_clusters) == 3
        assert sum(len(s.items) for s in result.thematic_summaries) == 5
        assert sum(len(s.items) for s in result.attention_sections) == 0
        assert len(result.filtered_headlines) == 7
        assert result.articles_included == 15

    def test_only_attention_sections(self):
        """Test when only attention sections are present."""
        content = _base_content(
            news_clusters=[],
            thematic_summaries=[],
            attention_sections=_make_sections([5, 3], "attention"),
            filtered_headlines=_make_items(10, "headline"),
            articles_included=18,
        )
        result = select_digest_articles(content, max_items=15)

        assert sum(len(s.items) for s in result.attention_sections) == 8
        assert len(result.filtered_headlines) == 7
        assert result.articles_included == 15


def _dead_item(title: str = "dead") -> DigestItem:
    return DigestItem(
        title=title,
        summary="",
        sources=["newsletter"],
        urls=["newsletter:x"],
        ref_urls=[],
    )


def test_count_dead_links_thematic_and_headline():
    content = _base_content(
        thematic_summaries=[
            DigestSection(heading="theme", items=[_dead_item()]),
        ],
        filtered_headlines=[
            DigestItem(
                title="live",
                summary="",
                sources=["a"],
                urls=["https://a.com/1"],
            )
        ],
    )
    assert count_dead_links(content) == 1


def test_count_dead_links_ref_urls_are_clickable():
    content = _base_content(
        thematic_summaries=[
            DigestSection(
                heading="theme",
                items=[
                    DigestItem(
                        title="item",
                        summary="",
                        sources=["n"],
                        urls=["newsletter:x"],
                        ref_urls=["https://a.com/ref"],
                    )
                ],
            )
        ],
    )
    assert count_dead_links(content) == 0


def test_count_dead_links_empty_content():
    content = _base_content(articles_included=0)
    assert count_dead_links(content) == 0


def test_truncation_preserves_story_id():
    """T3: a truncated section keeps its story_id."""
    section = DigestSection(heading="Tech", items=_make_items(3), story_id="X")

    result = _truncate_sections([section], max_items=1)

    assert len(result) == 1
    assert result[0].story_id == "X"
    assert len(result[0].items) == 1
