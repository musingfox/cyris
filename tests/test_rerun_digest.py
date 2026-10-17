"""A dry rerun rebuilds an issue from the articles its run was given, with no fetch and no write."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fakes import FakeLLM, RecordingD1, SqliteD1
from test_run_digest import make_deps

from cyris.adapters.store.d1_store import D1ArticleStore
from cyris.adapters.store.digest_inputs import D1DigestInputStore
from cyris.adapters.store.digests import D1DigestStore
from cyris.adapters.store.runs import D1RunLog
from cyris.domain.areas import Area
from cyris.domain.models import Article, Lane
from cyris.service_layer.run_digest import RunOptions, run_digest

pytestmark = pytest.mark.integration

RERUN = "2026-10-14"


def _article(i: int, *, title: str | None = None) -> Article:
    return Article(
        id=i,
        title=title or f"Story {i}",
        url=f"https://example.com/{i}",
        content="A launch.",
        published_at=datetime.now(UTC) - timedelta(hours=i),
        source_name="TechSource",
        source_lane=Lane.READ,
        source_tags=["tech"],
    )


def _scores(articles: list[Article], **extra) -> str:
    entries = [
        {"id": a.id, "score": 80, "language": "en", "tags": ["s"], **extra} for a in articles
    ]
    return json.dumps({"scores": entries})


def _sections(*articles: Article) -> str:
    entries = [
        {"id": str(position), "title": a.title, "source": a.source_name}
        for position, a in enumerate(articles)
    ]
    return json.dumps(
        {"sections": [{"heading": "Tech", "summary": "Launches.", "articles": entries}]}
    )


def _items(content) -> list:
    items = []
    for name in ("featured_articles", "news_clusters", "thematic_summaries", "attention_sections"):
        for section in getattr(content, name):
            items.extend(section.items)
    return items


def _area(area_id: int, name: str) -> Area:
    return Area(
        id=area_id,
        name=name,
        position=area_id - 1,
        status="active",
        merged_into=None,
        proposal_articles=0,
        proposal_issues=0,
        last_proposed_run=None,
    )


class _Tripwire:
    async def fetch_articles(self, **kwargs):
        raise AssertionError("the rerun fetched its sources")

    async def health_check(self) -> bool:
        return True


def _deps(tmp_path: Path, *, replies, record, rows):
    """A rerun's deps: `rows` are saved, `record` names the URLs the run was given."""
    deps, _ = make_deps(tmp_path, FakeLLM(replies), _Tripwire())
    deps.cfg.sources = {}
    deps.store.save(rows)
    inputs = D1DigestInputStore(SqliteD1())
    if record is not None:
        inputs.save(RERUN, "morning", record)
    summaries: list[dict] = []

    def sync() -> int:
        sync.calls += 1
        return 0

    sync.calls = 0
    deps = replace(
        deps,
        digest_input_store=inputs,
        sync_promotions=sync,
        record_run=summaries.append,
    )
    return deps, summaries


async def test_the_rerun_renders_the_recorded_articles_and_no_others(tmp_path: Path) -> None:
    kept = [_article(1, title="Kept one"), _article(2, title="Kept two")]
    not_recorded = _article(3, title="Not recorded")
    deps, _ = _deps(
        tmp_path,
        replies=[_scores(kept), _sections(*kept)],
        record=[a.url for a in kept],
        rows=[*kept, not_recorded],
    )

    report = await run_digest(deps, RunOptions(dry_run=True, rerun_of=RERUN))

    assert report.status == "ok"
    assert "Kept one" in report.rendered and "Kept two" in report.rendered
    assert "Not recorded" not in report.rendered
    assert deps.sync_promotions.calls == 0


async def test_the_issue_keeps_the_given_date_not_todays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cyris.service_layer import digest_pipeline

    monkeypatch.setattr(
        digest_pipeline,
        "now_in_timezone",
        lambda tz: datetime(2026, 10, 16, 12, tzinfo=ZoneInfo(tz)),
    )
    a = _article(1, title="Kept one")
    deps, _ = _deps(tmp_path, replies=[_scores([a]), _sections(a)], record=[a.url], rows=[a])

    report = await run_digest(deps, RunOptions(dry_run=True, rerun_of=RERUN))

    assert "<title>" in report.rendered and RERUN in report.rendered.split("<title>")[1]
    assert "2026-10-16" not in report.rendered.split("<title>")[1].split("</title>")[0]


async def test_no_record_stops_before_the_llm(tmp_path: Path) -> None:
    a = _article(1)
    deps, _ = _deps(tmp_path, replies=[_scores([a])], record=None, rows=[a])

    report = await run_digest(deps, RunOptions(dry_run=True, rerun_of=RERUN))

    assert report.status == "no_inputs"
    assert deps.llm.calls == []


async def test_a_partly_missing_record_runs_and_counts_what_is_gone(tmp_path: Path) -> None:
    a, b = _article(1), _article(2)
    deps, summaries = _deps(
        tmp_path,
        replies=[_scores([a, b]), _sections(a, b)],
        record=[a.url, "https://example.com/gone", b.url],
        rows=[a, b],
    )

    report = await run_digest(deps, RunOptions(dry_run=True, rerun_of=RERUN))

    assert report.status == "ok"
    assert summaries[-1]["inputs_missing"] == 1


async def test_a_record_whose_rows_are_all_gone_stops_before_the_llm(tmp_path: Path) -> None:
    a = _article(1)
    deps, _ = _deps(tmp_path, replies=[_scores([a])], record=["https://example.com/gone"], rows=[])

    report = await run_digest(deps, RunOptions(dry_run=True, rerun_of=RERUN))

    assert report.status == "no_inputs"
    assert deps.llm.calls == []


async def test_a_rerun_rescored_into_another_area_is_placed_there(tmp_path: Path) -> None:
    a = _article(1)
    deps, _ = _deps(
        tmp_path,
        replies=[_scores([a], area="Space"), _sections(a)],
        record=[a.url],
        rows=[a],
    )
    deps.cfg.areas = [_area(1, "Tech"), _area(2, "Space")]
    rendered: list = []
    original = deps.html_writer.render

    def spy(content, **kwargs):
        rendered.append(content)
        return original(content, **kwargs)

    deps.html_writer.render = spy

    await run_digest(deps, RunOptions(dry_run=True, rerun_of=RERUN))

    [item] = [i for i in _items(rendered[-1]) if a.url in i.urls]
    assert item.area == "Space"


async def test_a_dry_rerun_writes_only_its_run_record_and_keeps_scores(tmp_path: Path) -> None:
    db = RecordingD1()
    a = _article(1)
    store = D1ArticleStore(db)
    store.save([a])
    store.update_scores({a.url: (42.0, "en")})
    D1DigestInputStore(db).save(RERUN, "morning", [a.url])
    db.statements.clear()
    deps, _ = make_deps(tmp_path, FakeLLM(_scores([a]), _sections(a)), _Tripwire())
    deps.cfg.sources = {}
    summaries: list[dict] = []
    run_log = D1RunLog(db, "")

    def record(summary: dict) -> None:
        summaries.append(summary)
        run_log.record(summary)

    deps = replace(
        deps,
        store=store,
        digest_input_store=D1DigestInputStore(db),
        digest_store=D1DigestStore(db),
        record_run=record,
    )

    report = await run_digest(deps, RunOptions(dry_run=True, rerun_of=RERUN))

    assert report.status == "ok"
    assert db.written_tables() == {"digest_runs"}
    assert summaries[-1]["rerun_of"] == RERUN
    assert store.get_by_urls([a.url])[0].score == 42.0


async def test_a_rerun_reads_the_record_in_as_many_statements_as_it_needs(tmp_path: Path) -> None:
    db = RecordingD1()
    urls = [f"https://example.com/{i:04d}" for i in range(400)]
    D1DigestInputStore(db).save(RERUN, "morning", urls)
    db.statements.clear()
    deps, _ = make_deps(tmp_path, FakeLLM("{}"), _Tripwire())
    deps = replace(deps, store=D1ArticleStore(db), digest_input_store=D1DigestInputStore(db))

    report = await run_digest(deps, RunOptions(dry_run=True, rerun_of=RERUN))

    assert report.status == "no_inputs"
    assert sum("WHERE url IN" in sql for sql in db.statements) == 5
