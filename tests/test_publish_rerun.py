"""A publishing rerun replaces the newest issue, and only the newest, and says what it published."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fakes import FakeLLM, RecordingD1
from test_run_digest import make_deps

from cyris.adapters.store.d1_store import D1ArticleStore
from cyris.adapters.store.digest_inputs import D1DigestInputStore
from cyris.adapters.store.digests import D1DigestStore
from cyris.adapters.store.runs import D1RunLog
from cyris.domain.models import Article, ArticleState, DigestContent, Lane, UsageStats
from cyris.service_layer.run_digest import RunOptions, run_digest

pytestmark = pytest.mark.integration

RERUN = "2026-10-14"
SLUG = "2026-10-14-evening"


def _article(i: int, title: str) -> Article:
    return Article(
        id=i,
        title=title,
        url=f"https://example.com/{i}",
        content="A launch.",
        published_at=datetime.now(UTC) - timedelta(hours=i),
        source_name="TechSource",
        source_lane=Lane.READ,
        source_tags=["tech"],
    )


def _scores(articles: list[Article]) -> str:
    entries = [{"id": a.id, "score": 80, "language": "en", "tags": ["s"]} for a in articles]
    return json.dumps({"scores": entries})


def _sections(articles: list[Article]) -> str:
    entries = [
        {"id": str(position), "title": a.title, "source": a.source_name}
        for position, a in enumerate(articles)
    ]
    return json.dumps(
        {"sections": [{"heading": "Tech", "summary": "Launches.", "articles": entries}]}
    )


class _Site:
    """Pages Cloudflare would serve: what was published is what a page check reads back."""

    def __init__(self, *, serves: bool = True) -> None:
        self.live: dict[str, bytes] = {}
        self.deploys: list[str] = []
        self._serves = serves

    def publish(self, pages, slug) -> bool:
        self.deploys.append(slug)
        self.live.update(pages)
        return True

    def page_serves(self, path, expected) -> bool:
        return self._serves and self.live.get(path) == expected


class _Stories:
    def __init__(self) -> None:
        self.saved: list[tuple[str, str]] = []

    def save(self, date, period, records) -> None:
        self.saved.append((date, period))


def _content(date: str, period: str, *, articles_included: int = 0) -> DigestContent:
    return DigestContent(
        date=date,
        period=period,
        sources_processed=1,
        articles_received=2,
        articles_included=articles_included,
        usage=UsageStats(api_calls=1),
    )


def _setup(tmp_path: Path, articles, *, replies, serves: bool = True, db=None):
    db = db or RecordingD1()
    deps, notifications = make_deps(tmp_path, FakeLLM(replies), _NoFetch())
    deps.cfg.sources = {}
    deps.cfg.app.notify.email_to = "me@example.org"
    deps.cfg.app.notify.email_from = "digest@example.org"
    store = D1ArticleStore(db)
    store.save(articles)
    digests = D1DigestStore(db)
    inputs = D1DigestInputStore(db)
    inputs.save(RERUN, "evening", [a.url for a in articles])
    site = _Site(serves=serves)
    mailed: list = []
    usage: list = []

    async def email(recipient, sender, content, **kwargs):
        mailed.append(recipient)

    stories = _Stories()
    summaries: list[dict] = []
    deps = replace(
        deps,
        store=store,
        digest_store=digests,
        digest_input_store=inputs,
        record_run=lambda s: (summaries.append(s), D1RunLog(db, "").record(s)),
        publish_site=site.publish,
        page_serves=site.page_serves,
        send_email=email,
        log_usage=usage.append,
        story_store=stories,
        sync_promotions=None,
    )
    return deps, db, site, notifications, mailed, usage, stories, summaries


class _NoFetch:
    async def fetch_articles(self, **kwargs):
        raise AssertionError("the rerun fetched its sources")

    async def health_check(self) -> bool:
        return True


@pytest.fixture
def today_is_the_16th(monkeypatch: pytest.MonkeyPatch) -> None:
    from zoneinfo import ZoneInfo

    from cyris.service_layer import digest_pipeline

    monkeypatch.setattr(
        digest_pipeline,
        "now_in_timezone",
        lambda tz: datetime(2026, 10, 16, 12, tzinfo=ZoneInfo(tz)),
    )


async def test_the_newest_issue_is_replaced_published_and_notifies_nobody(
    tmp_path: Path, today_is_the_16th
) -> None:
    u1, u2 = _article(1, "Kept one"), _article(2, "Kept two")
    deps, db, site, notifications, mailed, _, stories, _ = _setup(
        tmp_path, [u1, u2], replies=[_scores([u1, u2]), _sections([u1, u2])]
    )
    deps.digest_store.save(_content("2026-10-14", "evening"), raw_page=False)

    report = await run_digest(deps, RunOptions(dry_run=False, rerun_of=RERUN, period="evening"))

    assert report.status == "ok"
    assert report.slug == SLUG
    stored = deps.digest_store.load(RERUN, "evening")
    assert stored.content.date == RERUN
    assert ("2026-10-14", "evening") in stories.saved
    assert f"/{SLUG}.html" in site.live
    assert notifications == [] and mailed == []


async def test_an_older_issue_is_not_latest_and_writes_nothing_but_its_run_record(
    tmp_path: Path,
) -> None:
    a = _article(1, "Kept one")
    db = RecordingD1()
    deps, _, site, _, _, _, _, _ = _setup(tmp_path, [a], replies=[_scores([a])], db=db)
    deps.digest_store.save(_content("2026-10-14", "evening"), raw_page=False)
    deps.digest_store.save(_content("2026-10-15", "morning"), raw_page=False)
    db.statements.clear()

    report = await run_digest(deps, RunOptions(dry_run=False, rerun_of=RERUN, period="evening"))

    assert report.status == "not_latest"
    assert report.later == ("2026-10-15", "morning")
    assert deps.llm.calls == []
    assert db.written_tables() == {"digest_runs"}
    assert site.deploys == []


async def test_a_human_verdict_survives_and_a_pending_article_is_accepted(tmp_path: Path) -> None:
    u1, u2, u3 = _article(1, "Kept one"), _article(2, "Cut earlier"), _article(3, "Human reject")
    deps, _, _, _, _, _, _, _ = _setup(
        tmp_path,
        [u1, u2, u3],
        replies=[_scores([u1, u2, u3]), _sections([u1, u2, u3])],
    )
    deps.store.reject([u3.url], reason="filtered")
    deps.store.update_triage_timestamp([u3.url], datetime.now(UTC))
    deps.digest_store.save(_content("2026-10-14", "evening"), raw_page=False)

    await run_digest(deps, RunOptions(dry_run=False, rerun_of=RERUN, period="evening"))

    states = {a.url: a for a in deps.store.get_by_urls([u1.url, u2.url, u3.url])}
    assert states[u2.url].state == ArticleState.ACCEPTED
    assert states[u2.url].digest_date == RERUN
    assert states[u3.url].state == ArticleState.REJECTED


async def test_the_input_record_is_left_as_it_was_recorded(tmp_path: Path) -> None:
    u1, u2 = _article(1, "Kept one"), _article(2, "Kept two")
    deps, db, _, _, _, _, _, _ = _setup(
        tmp_path, [u1, u2], replies=[_scores([u1, u2]), _sections([u1, u2])]
    )
    db.query("DELETE FROM stored_articles WHERE url = ?", [u1.url])
    deps.digest_store.save(_content("2026-10-14", "evening"), raw_page=False)

    await run_digest(deps, RunOptions(dry_run=False, rerun_of=RERUN, period="evening"))

    assert deps.digest_input_store.load(RERUN, "evening") == [u1.url, u2.url]


async def test_a_failed_page_check_reports_publish_failed_and_keeps_the_new_content(
    tmp_path: Path,
) -> None:
    a = _article(1, "Kept one")
    deps, _, _, _, _, _, _, summaries = _setup(
        tmp_path, [a], replies=[_scores([a]), _sections([a])], serves=False
    )
    deps.digest_store.save(_content("2026-10-14", "evening"), raw_page=False)

    report = await run_digest(deps, RunOptions(dry_run=False, rerun_of=RERUN, period="evening"))

    assert report.status == "publish_failed"
    assert summaries[-1]["status"] == "publish_failed"
    assert deps.digest_store.load(RERUN, "evening").content.articles_included == 1


async def test_usage_is_logged_once_with_the_rerun_content(tmp_path: Path) -> None:
    a = _article(1, "Kept one")
    deps, _, _, _, _, usage, _, _ = _setup(tmp_path, [a], replies=[_scores([a]), _sections([a])])
    deps.digest_store.save(_content("2026-10-14", "evening"), raw_page=False)

    await run_digest(deps, RunOptions(dry_run=False, rerun_of=RERUN, period="evening"))

    [content] = usage
    assert content.date == RERUN


async def test_the_deploy_holds_the_raw_page_of_the_input_rows(tmp_path: Path) -> None:
    u1, u2 = _article(1, "Kept one"), _article(2, "Kept two")
    deps, _, site, _, _, _, _, _ = _setup(
        tmp_path, [u1, u2], replies=[_scores([u1, u2]), _sections([u1, u2])]
    )
    deps.digest_store.save(_content("2026-10-14", "evening"), raw_page=False)

    await run_digest(deps, RunOptions(dry_run=False, rerun_of=RERUN, period="evening"))

    raw = site.live[f"/{SLUG}-raw.html"]
    assert b"Kept one" in raw and b"Kept two" in raw
    assert f"/{SLUG}.html" in site.live and "/index.html" in site.live
    assert SLUG.encode() in site.live["/index.html"]


async def test_the_raw_page_shows_the_states_the_rerun_left(tmp_path: Path) -> None:
    u1, u2 = _article(1, "Kept one"), _article(2, "Kept two")
    deps, _, site, _, _, _, _, _ = _setup(
        tmp_path, [u1, u2], replies=[_scores([u1, u2]), _sections([u1, u2])]
    )
    deps.digest_store.save(_content("2026-10-14", "evening"), raw_page=False)

    await run_digest(deps, RunOptions(dry_run=False, rerun_of=RERUN, period="evening"))

    raw = site.live[f"/{SLUG}-raw.html"]
    assert b'class="state accepted"' in raw
    assert b'class="state pending"' not in raw


async def test_the_run_record_names_the_issue_it_reran(tmp_path: Path) -> None:
    a = _article(1, "Kept one")
    db = RecordingD1()
    deps, _, _, _, _, _, _, _ = _setup(tmp_path, [a], replies=[_scores([a]), _sections([a])], db=db)
    deps.digest_store.save(_content("2026-10-14", "evening"), raw_page=False)

    await run_digest(deps, RunOptions(dry_run=False, rerun_of=RERUN, period="evening"))

    [row] = db.query("SELECT summary FROM digest_runs").rows
    assert json.loads(row["summary"])["rerun_of"] == RERUN


async def test_the_newest_is_judged_by_date_and_period_not_by_when_it_was_saved(
    tmp_path: Path,
) -> None:
    a = _article(1, "Kept one")
    db = RecordingD1()
    deps, _, _, _, _, _, _, _ = _setup(tmp_path, [a], replies=[_scores([a]), _sections([a])], db=db)
    deps.digest_store.save(_content("2026-10-15", "morning"), raw_page=False)
    deps.digest_store.save(_content("2026-10-14", "evening"), raw_page=False)

    deps.digest_input_store.save("2026-10-15", "morning", [a.url])
    older = await run_digest(deps, RunOptions(dry_run=False, rerun_of=RERUN, period="evening"))
    newer = await run_digest(
        deps,
        RunOptions(dry_run=False, rerun_of="2026-10-15", period="morning"),
    )

    assert older.status == "not_latest"
    assert newer.status == "ok"
