"""The site-default rule: a picture one source's articles share in an issue is not shown."""

from datetime import UTC, datetime

import pytest

from cyris.domain.models import Article, DigestItem, DigestSection, Lane
from cyris.domain.story_images import article_images, stamp_story_images

pytestmark = pytest.mark.unit

LOGO = "https://news.example/logo.png"


def _article(n: int, source: str, image_url: str | None) -> Article:
    return Article(
        id=n,
        title=f"Article {n}",
        url=f"https://{source}.example/{n}",
        content="",
        published_at=datetime(2026, 10, 31, tzinfo=UTC),
        source_name=source,
        source_lane=Lane.READ,
        image_url=image_url,
    )


def test_a_picture_only_its_own_article_names_is_kept() -> None:
    articles = [_article(1, "news", "https://news.example/1.jpg"), _article(2, "news", None)]

    assert article_images(articles) == {"https://news.example/1": "https://news.example/1.jpg"}


def test_a_picture_two_articles_of_one_source_share_is_dropped_for_both() -> None:
    articles = [
        _article(1, "news", LOGO),
        _article(2, "news", LOGO),
        _article(3, "news", "https://news.example/3.jpg"),
    ]

    assert article_images(articles) == {"https://news.example/3": "https://news.example/3.jpg"}


def test_the_same_picture_from_two_sources_is_each_ones_own() -> None:
    articles = [_article(1, "news", LOGO), _article(2, "wire", LOGO)]

    assert article_images(articles) == {
        "https://news.example/1": LOGO,
        "https://wire.example/2": LOGO,
    }


def test_one_article_listed_twice_does_not_share_with_itself() -> None:
    article = _article(1, "news", LOGO)

    assert article_images([article, article]) == {"https://news.example/1": LOGO}


def test_the_stamp_pictures_single_article_items_and_never_a_merged_one() -> None:
    articles = [_article(1, "news", "https://news.example/1.jpg"), _article(2, "news", None)]
    single = DigestItem(title="One", summary="", sources=["news"], urls=[articles[0].url])
    plain = DigestItem(title="Two", summary="", sources=["news"], urls=[articles[1].url])
    merged = DigestItem(title="Both", summary="", sources=["news"], urls=[a.url for a in articles])

    stamp_story_images([DigestSection(heading="S", items=[single, plain, merged])], articles)

    assert [single.image_url, plain.image_url, merged.image_url] == [
        "https://news.example/1.jpg",
        None,
        None,
    ]
