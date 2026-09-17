"""Newsletter dedup in the store: a sender's repeated link must not swallow an issue.

Every case runs against both ArticleRepository implementations.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fakes import CompoundSelectLimitedD1, SqliteD1

from cyris.adapters.fetch.email_parser import ParsedNewsletter
from cyris.adapters.fetch.newsletter import newsletter_article
from cyris.adapters.store.article_store import ArticleStore
from cyris.adapters.store.d1_store import D1ArticleStore
from cyris.domain.models import NEWSLETTER_SOURCE_TYPE, Article, SourceConfig, Tier
from cyris.service_layer.fetching import fetch_all_articles

NOW = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
NAV = "https://s.com/account/settings/email"


@pytest.fixture(params=["json", "d1"])
def store(request, tmp_path: Path):
    return ArticleStore(tmp_path) if request.param == "json" else D1ArticleStore(SqliteD1())


def _issue(article_id: str, title: str, url: str = NAV, source_name: str = "NL") -> Article:
    return Article(
        id=article_id,
        title=title,
        url=url,
        content="body",
        published_at=NOW,
        source_name=source_name,
        source_tier=Tier.SUMMARIZE,
        source_type=NEWSLETTER_SOURCE_TYPE,
    )


def _rows(store) -> list:
    return store.load_by_time_range(NOW - timedelta(days=1), NOW + timedelta(days=1))


def test_a_colliding_issue_is_stored_under_its_synthetic_url(store) -> None:
    store.save([_issue("a", "Issue 1")], now=NOW)

    result = store.save([_issue("b", "Issue 2")], now=NOW)

    assert (result.saved_count, result.skipped_count) == (1, 0)
    by_title = {r.title: r for r in _rows(store)}
    assert by_title["Issue 2"].url == "newsletter:b"
    assert by_title["Issue 2"].ref_urls == []
    assert by_title["Issue 1"].url == NAV


def test_siblings_in_one_batch_are_both_stored(store) -> None:
    result = store.save([_issue("a", "Issue 1"), _issue("b", "Issue 2")], now=NOW)

    assert result.saved_count == 2
    assert {r.url for r in _rows(store)} == {NAV, "newsletter:b"}


def _mail(n: int) -> ParsedNewsletter:
    text = f"Settings {NAV}\nRead https://s.com/posts/issue-{n}\nUnsubscribe {NAV}\n"
    return ParsedNewsletter(
        source_name="NL",
        subject=f"Issue {n}",
        from_email="list@s.com",
        date=NOW,
        html_content="",
        text_content=text,
    )


async def test_two_issues_through_fetch_and_save_both_reach_the_digest_window(store) -> None:
    source = SourceConfig(
        name="NL",
        type="newsletter",
        tier=Tier.SUMMARIZE,
        email_match="from:list@s.com",
        homepage="https://s.com",
    )
    fetch_source = AsyncMock()
    fetch_source.fetch_articles.return_value = [
        newsletter_article(_mail(1), source),
        newsletter_article(_mail(2), source),
    ]
    articles, _ = await fetch_all_articles(
        [fetch_source], NOW - timedelta(days=1), NOW, {"NL": source}
    )

    store.save(articles, now=NOW)

    rows = _rows(store)
    assert len(rows) == 2
    assert {r.title for r in rows} == {"Issue 1", "Issue 2"}
    assert len({r.url for r in rows}) == 2


def test_d1_pre_read_stays_under_the_compound_select_ceiling() -> None:
    store = D1ArticleStore(CompoundSelectLimitedD1())
    articles = [_issue(f"n{i}", f"Issue {i}", url=f"https://s.com/posts/{i}") for i in range(95)]
    articles.append(_issue("dup", "Issue X", url="https://s.com/posts/0"))

    result = store.save(articles, now=NOW)

    assert result.saved_count == 96
