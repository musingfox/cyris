"""Tests for digest article selection: the read lane's layers and the order inside them."""

import pytest

from cyris.domain.models import DigestContent, DigestItem, DigestSection
from cyris.domain.selection import (
    count_dead_links,
    layer_by_score,
    order_by_preference,
    preference_moves,
    rank_by_preference,
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
    """A read-lane group; summary=None is the degraded shape, which has no group summary."""
    return DigestSection(heading=heading, summary=summary, items=list(articles))


def _layer(*sections: DigestSection, threshold: float = 70) -> DigestContent:
    content = _base_content(thematic_summaries=list(sections))
    return layer_by_score(content, featured_threshold=threshold)


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

    def test_every_article_past_the_lead_is_a_feature_and_none_is_left_out(self):
        result = _layer(*(_topic(f"T{n}", _article(f"a{n}", 90 - n)) for n in range(12)))

        assert [_titles(s) for s in result.featured_articles] == [[f"a{n}"] for n in range(12)]
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


def _headline(name: str, *urls: str) -> DigestItem:
    return DigestItem(
        title=name,
        summary="",
        sources=["Wire"],
        urls=list(urls) or [f"https://example.com/{name}"],
    )


def _names(items) -> list[str]:
    return [getattr(i, "title", None) or i.heading for i in items]


class TestOrderByPreference:
    """The reader's votes order each layer and cut nothing; the LLM's order breaks every tie."""

    def test_an_issue_of_any_size_keeps_every_entry_and_its_count(self):
        content = _base_content(
            featured_articles=_make_sections([3, *[1] * 20], "feature"),
            news_clusters=_make_sections([10, 10, 1], "news"),
            attention_sections=_make_sections([9], "attention"),
            fan_sections=_make_sections([6], "fan"),
            filtered_headlines=_make_items(40, "headline"),
            articles_included=99,
        )

        assert order_by_preference(content) == content
        assert order_by_preference(content, {"https://example.com/headline-39": 1.0}).model_dump(
            exclude={"filtered_headlines"}
        ) == content.model_dump(exclude={"filtered_headlines"})

    def test_an_empty_issue_stays_empty(self):
        content = _base_content(articles_included=0)

        assert order_by_preference(content) == content

    def test_the_headlines_and_clusters_follow_the_votes(self):
        content = _base_content(
            news_clusters=_make_sections([1, 2], "news"),
            filtered_headlines=[_headline("disliked"), _headline("unjudged"), _headline("liked")],
        )
        preference = {
            "https://example.com/disliked": -0.2,
            "https://example.com/liked": 0.3,
            "https://example.com/news-0-0": 0.1,
            "https://example.com/news-1-1": 0.9,
        }

        result = order_by_preference(content, preference)

        assert _names(result.filtered_headlines) == ["liked", "unjudged", "disliked"]
        assert _names(result.news_clusters) == ["news-1", "news-0"]

    def test_without_votes_the_news_keeps_the_models_order(self):
        content = _base_content(
            news_clusters=_make_sections([1, 1], "news"),
            filtered_headlines=[_headline("b"), _headline("a")],
        )

        result = order_by_preference(content, None)

        assert _names(result.filtered_headlines) == ["b", "a"]
        assert _names(result.news_clusters) == ["news-0", "news-1"]

    def test_equal_scores_keep_the_llms_order(self):
        items = [_headline(n) for n in "abc"]
        preference = {f"https://example.com/{n}": 0.1 for n in "abc"}

        assert _names(rank_by_preference(items, preference)) == ["a", "b", "c"]

    def test_an_item_without_a_score_keeps_its_slot(self):
        items = [_headline("unjudged"), _headline("low"), _headline("high")]
        preference = {"https://example.com/low": -0.4, "https://example.com/high": 0.4}

        assert _names(rank_by_preference(items, preference)) == ["unjudged", "high", "low"]

    def test_an_item_of_several_articles_takes_its_best_articles_score(self):
        items = [
            _headline("single", "https://example.com/single"),
            _headline("merged", "https://example.com/m1", "https://example.com/m2"),
        ]
        preference = {
            "https://example.com/single": 0.2,
            "https://example.com/m1": -0.5,
            "https://example.com/m2": 0.3,
        }

        assert _names(rank_by_preference(items, preference)) == ["merged", "single"]

    def test_features_follow_the_preference_and_the_top_story_stays(self):
        content = _base_content(
            featured_articles=[
                DigestSection(heading=t, items=[_article(t, s)])
                for t, s in (("lead", 99), ("high", 90), ("mid", 75), ("low", 60))
            ]
        )
        preference = {
            "https://example.com/lead": -0.9,
            "https://example.com/high": -0.1,
            "https://example.com/mid": 0.0,
            "https://example.com/low": 0.2,
        }

        result = order_by_preference(content, preference)

        assert _names(result.featured_articles) == ["lead", "low", "mid", "high"]

    def test_features_of_equal_preference_keep_their_score_order(self):
        content = _base_content(
            featured_articles=[
                DigestSection(heading=t, items=[_article(t, s)])
                for t, s in (("lead", 99), ("low", 60), ("high", 90))
            ]
        )
        preference = {"https://example.com/low": 0.1, "https://example.com/high": 0.1}

        result = order_by_preference(content, preference)

        assert _names(result.featured_articles) == ["lead", "high", "low"]

    def test_the_moves_count_the_feature_positions_that_changed(self):
        content = _base_content(
            featured_articles=[
                DigestSection(heading=t, items=[_article(t, s)])
                for t, s in (("lead", 99), ("high", 90), ("mid", 75), ("low", 60))
            ],
        )
        preference = {f"https://example.com/{n}": 0.0 for n in ("high", "mid")}
        preference |= {"https://example.com/low": 0.5}

        ranked = order_by_preference(content, preference)

        assert _names(ranked.featured_articles) == ["lead", "low", "high", "mid"]
        assert preference_moves(content, ranked) == {"features": 3, "news": 0}
        assert preference_moves(content, content) == {"features": 0, "news": 0}

    def test_the_moves_count_the_news_positions_that_changed(self):
        content = _base_content(
            news_clusters=_make_sections([1, 1], "news"),
            filtered_headlines=[_headline("a"), _headline("b"), _headline("c")],
        )
        preference = {f"https://example.com/{n}": 0.0 for n in ("news-0-0", "a", "b")}
        preference |= {"https://example.com/news-1-0": 0.5, "https://example.com/c": 0.5}

        ranked = order_by_preference(content, preference)

        # Both clusters swap; c takes a's slot, a b's and b c's.
        assert preference_moves(content, ranked) == {"features": 0, "news": 5}


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


def test_count_dead_links_counts_each_sand_member_the_fold_shows():
    dead = _dead_item()
    live = DigestItem(title="live", summary="", sources=["a"], urls=["https://a.com/1"])
    merged = DigestItem(
        title="topic", summary="", sources=["n", "a"], urls=[*dead.urls, *live.urls]
    )
    content = _base_content(
        news_clusters=[DigestSection(heading="topic", items=[merged], members=[dead, live])],
    )
    assert count_dead_links(content) == 1


def test_count_dead_links_empty_content():
    content = _base_content(articles_included=0)
    assert count_dead_links(content) == 0
