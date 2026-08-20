"""Tests for digest article selection with priority fill."""

from cyris.domain.models import DigestContent, DigestItem, DigestSection
from cyris.domain.selection import count_dead_links, layer_by_score, select_digest_articles


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


class TestLayerByScore:
    def test_basic_layering(self):
        """Test basic score layering with sections above threshold."""
        sections = [
            DigestSection(
                heading="High Score Article",
                items=[
                    DigestItem(
                        title="Article 1",
                        summary="Summary",
                        sources=["Source A"],
                        urls=["http://example.com/1"],
                        score=90.0,
                    )
                ],
            ),
            DigestSection(
                heading="Medium Score Article",
                items=[
                    DigestItem(
                        title="Article 2",
                        summary="Summary",
                        sources=["Source B"],
                        urls=["http://example.com/2"],
                        score=75.0,
                    )
                ],
            ),
            DigestSection(
                heading="Low Score Article",
                items=[
                    DigestItem(
                        title="Article 3",
                        summary="Summary",
                        sources=["Source C"],
                        urls=["http://example.com/3"],
                        score=60.0,
                    )
                ],
            ),
        ]

        content = _base_content(thematic_summaries=sections, articles_included=3)

        result = layer_by_score(content, featured_threshold=70, max_featured=3)

        assert len(result.featured_articles) == 2
        assert len(result.thematic_summaries) == 1
        assert result.featured_articles[0].heading == "High Score Article"
        assert result.featured_articles[1].heading == "Medium Score Article"
        assert result.thematic_summaries[0].heading == "Low Score Article"

    def test_max_featured_cap(self):
        """Test that max_featured caps the number of featured sections."""
        sections = [
            DigestSection(
                heading=f"Article {i}",
                items=[
                    DigestItem(
                        title=f"Title {i}",
                        summary="Summary",
                        sources=["Source"],
                        urls=[f"http://example.com/{i}"],
                        score=90.0 - i,  # Descending scores: 90, 89, 88, ...
                    )
                ],
            )
            for i in range(10)
        ]

        content = _base_content(thematic_summaries=sections, articles_included=10)

        result = layer_by_score(content, featured_threshold=70, max_featured=3)

        assert len(result.featured_articles) == 3
        assert len(result.thematic_summaries) == 7
        # Check featured are top 3 by score
        assert result.featured_articles[0].heading == "Article 0"
        assert result.featured_articles[1].heading == "Article 1"
        assert result.featured_articles[2].heading == "Article 2"

    def test_no_qualifying_sections(self):
        """Test when all sections are below threshold."""
        sections = [
            DigestSection(
                heading="Low Score Article",
                items=[
                    DigestItem(
                        title="Article",
                        summary="Summary",
                        sources=["Source"],
                        urls=["http://example.com"],
                        score=60.0,
                    )
                ],
            )
        ]

        content = _base_content(thematic_summaries=sections, articles_included=1)

        result = layer_by_score(content, featured_threshold=70, max_featured=5)

        assert len(result.featured_articles) == 0
        assert len(result.thematic_summaries) == 1

    def test_multi_item_section_max_score(self):
        """Test score calculation for sections with multiple items."""
        sections = [
            DigestSection(
                heading="Multi-item Section",
                items=[
                    DigestItem(
                        title="Item 1",
                        summary="Summary",
                        sources=["A"],
                        urls=["http://a"],
                        score=60.0,
                    ),
                    DigestItem(
                        title="Item 2",
                        summary="Summary",
                        sources=["B"],
                        urls=["http://b"],
                        score=85.0,
                    ),
                    DigestItem(
                        title="Item 3",
                        summary="Summary",
                        sources=["C"],
                        urls=["http://c"],
                        score=70.0,
                    ),
                ],
            )
        ]

        content = _base_content(thematic_summaries=sections, articles_included=3)

        result = layer_by_score(content, featured_threshold=70, max_featured=5)

        # Max score is 85, which is >= 70, so it should be featured
        assert len(result.featured_articles) == 1
        assert len(result.thematic_summaries) == 0

    def test_mixed_scores_and_none(self):
        """Test sections with mixed scores and None values."""
        sections = [
            DigestSection(
                heading="Section with None",
                items=[
                    DigestItem(
                        title="No score",
                        summary="Summary",
                        sources=["A"],
                        urls=["http://a"],
                        score=None,
                    )
                ],
            ),
            DigestSection(
                heading="Section with high score",
                items=[
                    DigestItem(
                        title="High score",
                        summary="Summary",
                        sources=["B"],
                        urls=["http://b"],
                        score=80.0,
                    )
                ],
            ),
        ]

        content = _base_content(thematic_summaries=sections, articles_included=2)

        result = layer_by_score(content, featured_threshold=70, max_featured=5)

        assert len(result.featured_articles) == 1
        assert result.featured_articles[0].heading == "Section with high score"
        assert len(result.thematic_summaries) == 1
        assert result.thematic_summaries[0].heading == "Section with None"

    def test_sorting_by_max_score(self):
        """Test that featured sections are sorted by max score descending."""
        sections = [
            DigestSection(
                heading="Score 75",
                items=[
                    DigestItem(title="T", summary="S", sources=["A"], urls=["http://1"], score=75.0)
                ],
            ),
            DigestSection(
                heading="Score 90",
                items=[
                    DigestItem(title="T", summary="S", sources=["B"], urls=["http://2"], score=90.0)
                ],
            ),
            DigestSection(
                heading="Score 85",
                items=[
                    DigestItem(title="T", summary="S", sources=["C"], urls=["http://3"], score=85.0)
                ],
            ),
        ]

        content = _base_content(thematic_summaries=sections, articles_included=3)

        result = layer_by_score(content, featured_threshold=70, max_featured=3)

        assert len(result.featured_articles) == 3
        assert result.featured_articles[0].heading == "Score 90"
        assert result.featured_articles[1].heading == "Score 85"
        assert result.featured_articles[2].heading == "Score 75"

    def test_empty_thematic_summaries(self):
        """Test layering with no thematic summaries."""
        content = _base_content(thematic_summaries=[], articles_included=0)

        result = layer_by_score(content, featured_threshold=70, max_featured=5)

        assert len(result.featured_articles) == 0
        assert len(result.thematic_summaries) == 0


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
