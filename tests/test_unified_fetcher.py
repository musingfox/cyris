"""Tests for unified article fetcher."""

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from cyris.domain.models import NEWSLETTER_SOURCE_TYPE, Article, Lane
from cyris.service_layer.fetching import fetch_all_articles

pytestmark = pytest.mark.unit


@pytest.fixture
def rss_articles():
    return [
        Article(
            id=1,
            title="RSS 1",
            url="https://a.com/1",
            content="C",
            published_at=datetime(2026, 3, 18),
            source_name="RSS",
            source_lane=Lane.SKIM,
        ),
        Article(
            id=2,
            title="RSS 2",
            url="https://a.com/2",
            content="C",
            published_at=datetime(2026, 3, 18),
            source_name="RSS",
            source_lane=Lane.SKIM,
        ),
    ]


@pytest.fixture
def newsletter_articles():
    return [
        Article(
            id="nl-1",
            title="NL 1",
            url="https://b.com/1",
            content="C",
            published_at=datetime(2026, 3, 18, 8, 0, 0),
            source_name="NL",
            source_lane=Lane.READ,
        ),
    ]


@pytest.mark.asyncio
async def test_combines_sources(rss_articles, newsletter_articles):
    """Test that fetch_all_articles combines multiple sources."""
    mock_rss_source = AsyncMock()
    mock_rss_source.fetch_articles.return_value = rss_articles

    mock_newsletter_source = AsyncMock()
    mock_newsletter_source.fetch_articles.return_value = newsletter_articles

    result, failed = await fetch_all_articles(
        fetch_sources=[mock_rss_source, mock_newsletter_source],
        after=datetime(2026, 3, 17),
        before=datetime(2026, 3, 19),
        sources={},
    )

    assert len(result) == 3
    assert failed == []
    mock_rss_source.fetch_articles.assert_called_once()
    mock_newsletter_source.fetch_articles.assert_called_once()


@pytest.mark.asyncio
async def test_deduplicates_by_url(newsletter_articles):
    """Test that fetch_all_articles deduplicates by URL (last source wins)."""
    rss_duplicate = [
        Article(
            id=1,
            title="RSS version",
            url="https://b.com/1",
            content="RSS",
            published_at=datetime(2026, 3, 18),
            source_name="RSS",
            source_lane=Lane.SKIM,
        )
    ]

    mock_rss_source = AsyncMock()
    mock_rss_source.fetch_articles.return_value = rss_duplicate

    mock_newsletter_source = AsyncMock()
    mock_newsletter_source.fetch_articles.return_value = newsletter_articles

    result, _ = await fetch_all_articles(
        fetch_sources=[mock_rss_source, mock_newsletter_source],
        after=datetime(2026, 3, 17),
        before=datetime(2026, 3, 19),
        sources={},
    )

    # Newsletter version should win (last source wins)
    assert len(result) == 1
    assert result[0].source_name == "NL"


def _issue(article_id: str, title: str, url: str = "https://s.com/account/settings/email"):
    return Article(
        id=article_id,
        title=title,
        url=url,
        content="C",
        published_at=datetime(2026, 9, 1),
        source_name="NL",
        source_lane=Lane.READ,
        source_type=NEWSLETTER_SOURCE_TYPE,
    )


async def _fetch(*batches: list[Article]) -> list[Article]:
    fetch_sources = []
    for batch in batches:
        source = AsyncMock()
        source.fetch_articles.return_value = batch
        fetch_sources.append(source)
    result, _ = await fetch_all_articles(
        fetch_sources=fetch_sources,
        after=datetime(2026, 8, 31),
        before=datetime(2026, 9, 2),
        sources={},
    )
    return result


async def test_sibling_newsletter_issues_sharing_a_link_both_survive():
    result = await _fetch([_issue("a", "Issue 1"), _issue("b", "Issue 2")])
    assert {a.id for a in result} == {"a", "b"}


async def test_rss_then_newsletter_with_the_same_url_still_collapses():
    rss = Article(
        id=1,
        title="RSS version",
        url="https://b.com/1",
        content="RSS",
        published_at=datetime(2026, 3, 18),
        source_name="RSS",
        source_lane=Lane.SKIM,
    )
    result = await _fetch([rss], [_issue("nl", "NL 1", url="https://b.com/1")])
    assert len(result) == 1
    assert result[0].source_name == "NL"


async def test_the_same_newsletter_issue_twice_collapses():
    result = await _fetch([_issue("a", "Issue 1"), _issue("a", "Issue 1")])
    assert len(result) == 1


async def test_a_redelivered_sibling_is_not_kept_twice():
    result = await _fetch([_issue("a", "Issue 1"), _issue("b", "Issue 2"), _issue("b", "Issue 2")])
    assert sorted(a.id for a in result) == ["a", "b"]


async def test_one_issue_whose_id_is_built_at_runtime_fetched_twice_is_one_article():
    # Newsletter ids are hashes built at runtime: equal strings, never the same object.
    first, again = "".join(["nl-", "abc123"]), "".join(["nl-", "abc", "123"])
    assert first == again and first is not again

    result = await _fetch([_issue(first, "Issue")], [_issue(again, "Issue")])

    assert len(result) == 1


async def test_siblings_whose_source_type_is_built_at_runtime_both_survive():
    # A type read back from a row or a parsed mail equals the constant without being it.
    def read_back(issue: Article) -> Article:
        return issue.model_copy(update={"source_type": "".join(["news", "letter"])})

    assert read_back(_issue("a", "x")).source_type is not NEWSLETTER_SOURCE_TYPE

    result = await _fetch([read_back(_issue("a", "Issue 1"))], [read_back(_issue("b", "Issue 2"))])

    assert sorted(a.id for a in result) == ["a", "b"]


@pytest.mark.parametrize(
    ("held_type", "later_type"),
    [("mail", NEWSLETTER_SOURCE_TYPE), ("mail", "mail")],
    ids=["other-then-newsletter", "two-others"],
)
async def test_only_two_newsletters_sharing_a_link_are_siblings(held_type, later_type):
    # Article.source_type is a plain str: any other pair sharing a link collapses to the
    # later article, as two RSS items do.
    held = _issue("one", "Held").model_copy(update={"source_type": held_type})
    later = _issue("two", "Later").model_copy(update={"source_type": later_type})

    result = await _fetch([held], [later])

    assert [a.id for a in result] == ["two"]


@pytest.mark.asyncio
async def test_passes_parameters_to_sources(rss_articles):
    """The window and source configs reach every source; the run cap does not.

    The cap keeps the newest N of what the store holds for the window, so a source
    that cut at it first would drop articles before they were ever stored.
    """
    mock_source = AsyncMock()
    mock_source.fetch_articles.return_value = rss_articles

    test_sources = {"test": "config"}

    await fetch_all_articles(
        fetch_sources=[mock_source],
        after=datetime(2026, 3, 17),
        before=datetime(2026, 3, 19),
        sources=test_sources,
    )

    mock_source.fetch_articles.assert_called_once_with(
        after=datetime(2026, 3, 17),
        before=datetime(2026, 3, 19),
        sources=test_sources,
    )


@pytest.mark.asyncio
async def test_handles_empty_sources():
    """Test that fetch_all_articles handles empty source list."""
    result, failed = await fetch_all_articles(
        fetch_sources=[],
        after=datetime(2026, 3, 17),
        before=datetime(2026, 3, 19),
        sources={},
    )

    assert result == []
    assert failed == []


@pytest.mark.asyncio
async def test_handles_source_failure(rss_articles):
    """Test that fetch_all_articles continues when one source fails."""
    mock_bad_source = AsyncMock()
    mock_bad_source.fetch_articles.side_effect = Exception("Source down")

    mock_good_source = AsyncMock()
    mock_good_source.fetch_articles.return_value = rss_articles

    result, failed = await fetch_all_articles(
        fetch_sources=[mock_bad_source, mock_good_source],
        after=datetime(2026, 3, 17),
        before=datetime(2026, 3, 19),
        sources={},
    )

    assert len(result) == 2
    assert failed == ["AsyncMock"]
    mock_bad_source.fetch_articles.assert_called_once()
    mock_good_source.fetch_articles.assert_called_once()
