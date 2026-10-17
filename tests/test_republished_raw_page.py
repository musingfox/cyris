"""A republished issue gets its raw page back from the URLs its run recorded."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fakes import FakeLLM, SqliteD1
from test_run_digest import FakeSource, make_deps

from cyris.adapters.store.digest_inputs import D1DigestInputStore
from cyris.adapters.store.digests import D1DigestStore
from cyris.domain.models import Article, DigestContent, Lane, UsageStats
from cyris.service_layer.run_digest import RunOptions, run_digest

pytestmark = pytest.mark.integration

DATE, PERIOD = "2026-04-15", "morning"
RAW = b"2026-04-15-morning-raw.html"


def _article(i: int) -> Article:
    return Article(
        id=i,
        title=f"Raw story {i}",
        url=f"https://example.com/{i}",
        content="A launch.",
        published_at=datetime.now(UTC) - timedelta(hours=1),
        source_name="TechSource",
        source_lane=Lane.READ,
        source_tags=["tech"],
    )


async def _republish(tmp_path: Path, *, raw_page: bool, record, articles=(), store_cls=None):
    deps, _ = make_deps(tmp_path, FakeLLM("{}"), FakeSource([]))
    if store_cls is not None:
        deps = replace(deps, store=store_cls(tmp_path / "agent-vault"))
    deps.store.save(list(articles))
    db = SqliteD1()
    digests = D1DigestStore(db)
    digests.save(
        DigestContent(
            date=DATE,
            period=PERIOD,
            sources_processed=1,
            articles_received=2,
            articles_included=2,
            usage=UsageStats(),
        ),
        raw_page=raw_page,
    )
    inputs = D1DigestInputStore(db)
    if record is not None:
        inputs.save(DATE, PERIOD, record)
    published: list[dict[str, bytes]] = []

    def publish_site(pages, slug):
        published.append(pages)
        return True

    summaries: list[dict] = []
    deps = replace(
        deps,
        digest_store=digests,
        digest_input_store=inputs,
        publish_site=publish_site,
        record_run=summaries.append,
    )
    await run_digest(deps, RunOptions())
    [pages] = published
    return pages, summaries


async def test_a_raw_page_is_rebuilt_from_the_record_and_linked(tmp_path: Path) -> None:
    articles = [_article(1), _article(2)]
    pages, _ = await _republish(
        tmp_path,
        raw_page=True,
        record=[a.url for a in articles],
        articles=articles,
    )

    assert b"Raw story 1" in pages[f"/{RAW.decode()}"]
    assert b"Raw story 2" in pages[f"/{RAW.decode()}"]
    assert RAW in pages[f"/{DATE}-{PERIOD}.html"]


async def test_no_record_means_no_raw_page_and_no_link(tmp_path: Path) -> None:
    pages, summaries = await _republish(
        tmp_path, raw_page=True, record=None, articles=[_article(1)]
    )

    assert f"/{RAW.decode()}" not in pages
    assert RAW not in pages[f"/{DATE}-{PERIOD}.html"]
    assert summaries[-1]["republished"] == [f"{DATE}-{PERIOD}"]


async def test_a_stored_flag_of_zero_means_no_raw_page_even_with_a_record(tmp_path: Path) -> None:
    articles = [_article(1), _article(2)]
    pages, _ = await _republish(
        tmp_path, raw_page=False, record=[a.url for a in articles], articles=articles
    )

    assert f"/{RAW.decode()}" not in pages
    assert RAW not in pages[f"/{DATE}-{PERIOD}.html"]


async def test_a_record_of_three_with_two_rows_lists_the_two(tmp_path: Path) -> None:
    articles = [_article(1), _article(2)]
    urls = [*(a.url for a in articles), "https://example.com/gone"]
    pages, _ = await _republish(tmp_path, raw_page=True, record=urls, articles=articles)

    raw = pages[f"/{RAW.decode()}"]
    assert b"Raw story 1" in raw and b"Raw story 2" in raw
    assert RAW in pages[f"/{DATE}-{PERIOD}.html"]


async def test_a_failed_article_read_republishes_without_a_link(tmp_path: Path) -> None:
    from cyris.adapters.store.article_store import ArticleStore

    class Broken(ArticleStore):
        def get_by_urls(self, urls):
            raise RuntimeError("store unavailable")

    articles = [_article(1)]
    pages, summaries = await _republish(
        tmp_path,
        raw_page=True,
        record=[a.url for a in articles],
        articles=articles,
        store_cls=Broken,
    )

    assert f"/{RAW.decode()}" not in pages
    assert RAW not in pages[f"/{DATE}-{PERIOD}.html"]
    assert summaries[-1]["republished"] == [f"{DATE}-{PERIOD}"]


async def test_a_raw_page_that_fails_to_render_republishes_without_a_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cyris.adapters.output.html_digest import HtmlDigestWriter

    def refuse(self, *args, **kwargs):
        raise RuntimeError("template broke")

    monkeypatch.setattr(HtmlDigestWriter, "render_raw", refuse)
    articles = [_article(1)]
    pages, summaries = await _republish(
        tmp_path,
        raw_page=True,
        record=[a.url for a in articles],
        articles=articles,
    )

    assert f"/{RAW.decode()}" not in pages
    assert RAW not in pages[f"/{DATE}-{PERIOD}.html"]
    assert summaries[-1]["republished"] == [f"{DATE}-{PERIOD}"]
