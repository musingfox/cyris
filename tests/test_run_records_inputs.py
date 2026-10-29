"""A publishing run records the URLs it was given; a dry run or failed save leaves the digest."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fakes import FakeLLM, SqliteD1
from test_run_digest import FakeSource, make_deps

from cyris.adapters.store.digest_inputs import D1DigestInputStore
from cyris.adapters.store.digests import D1DigestStore
from cyris.domain.models import Article, Lane
from cyris.service_layer.run_digest import RunOptions, run_digest
from cyris.utils.timezone import now_in_timezone

pytestmark = pytest.mark.integration


def _article(i: int, *, hours: int = 1, lane: Lane = Lane.READ) -> Article:
    return Article(
        id=i,
        title=f"Story {i}",
        url=f"https://example.com/{i}",
        content="A launch.",
        published_at=datetime.now(UTC) - timedelta(hours=hours),
        source_name="TechSource",
        source_lane=lane,
        source_tags=["tech"],
    )


def _scores(articles: list[Article]) -> str:
    return json.dumps(
        {"scores": [{"id": a.id, "score": 10, "language": "en", "tags": ["s"]} for a in articles]}
    )


def _deps(tmp_path: Path, articles: list[Article]):
    deps, _ = make_deps(tmp_path, FakeLLM([_scores(articles)]), FakeSource(articles))
    deps.cfg.areas = []
    deps.cfg.sources = {}
    return deps


def _recording(deps, *, digest_input_store=None):
    summaries: list[dict] = []
    db = SqliteD1()
    deps = replace(
        deps,
        digest_input_store=digest_input_store or D1DigestInputStore(db),
        digest_store=D1DigestStore(SqliteD1()),
        record_run=summaries.append,
    )
    return deps, deps.digest_input_store, summaries


def _today(deps) -> str:
    return now_in_timezone(deps.cfg.app.general.timezone).strftime("%Y-%m-%d")


async def test_the_record_holds_the_newest_capped_urls_in_original_order(tmp_path: Path) -> None:
    articles = [_article(i, hours=i) for i in range(1, 6)]
    deps = _deps(tmp_path, articles)
    deps.cfg.app.digest.max_articles_per_digest = 3
    deps, store, _ = _recording(deps)

    await run_digest(deps, RunOptions())

    assert store.load(_today(deps), "morning") == [a.url for a in articles[:3]]


async def test_a_filter_rejected_article_is_recorded(tmp_path: Path) -> None:
    articles = [_article(1, lane=Lane.SKIM)]
    deps = _deps(tmp_path, articles)
    deps, store, _ = _recording(deps)

    await run_digest(deps, RunOptions())

    assert articles[0].url in store.load(_today(deps), "morning")


async def test_a_dry_run_records_nothing(tmp_path: Path) -> None:
    articles = [_article(1)]
    deps, store, _ = _recording(_deps(tmp_path, articles))

    await run_digest(deps, RunOptions(dry_run=True))

    assert store.load(_today(deps), "morning") is None


async def test_a_failed_save_costs_the_run_nothing(tmp_path: Path) -> None:
    class Broken(D1DigestInputStore):
        def save(self, date, period, urls):
            raise RuntimeError("D1 unavailable")

    articles = [_article(1)]
    deps, _, summaries = _recording(
        _deps(tmp_path, articles), digest_input_store=Broken(SqliteD1())
    )

    report = await run_digest(deps, RunOptions())

    assert report.status == "ok"
    assert deps.digest_store.issues()
    assert summaries[-1]["digest_inputs_error"] == "D1 unavailable"


async def test_a_run_with_no_input_store_succeeds(tmp_path: Path) -> None:
    articles = [_article(1)]
    deps = replace(_deps(tmp_path, articles), digest_input_store=None)

    report = await run_digest(deps, RunOptions())

    assert report.status == "ok"
