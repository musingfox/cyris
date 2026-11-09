"""A stored issue re-rendered with today's templates, with no LLM call and no write."""

from dataclasses import replace
from pathlib import Path

import pytest
from fakes import FakeLLM, RecordingD1, make_config

from cyris.adapters.output.html_digest import HtmlDigestWriter
from cyris.adapters.store.digests import D1DigestStore
from cyris.bootstrap import Deps
from cyris.domain.models import DigestContent, UsageStats
from cyris.service_layer.run_digest import rerender_digest

pytestmark = pytest.mark.integration


def _content() -> DigestContent:
    return DigestContent(
        date="2026-10-14",
        period="evening",
        sources_processed=1,
        articles_received=3,
        articles_included=3,
        usage=UsageStats(api_calls=1),
        area_order=["Space", "Tech"],
    )


def _deps(tmp_path: Path, db: RecordingD1, llm: FakeLLM) -> Deps:
    async def discord(webhook_url, content, digest_url="", publish_failed=False, degraded=False):
        raise AssertionError("Discord was called")

    return Deps(
        cfg=make_config(),
        store=None,
        llm=llm,
        fetch_sources=[],
        html_writer=HtmlDigestWriter(tmp_path / "html"),
        sync_promotions=None,
        log_usage=lambda content: None,
        digest_store=D1DigestStore(db),
        send_discord=discord,
    )


def test_a_stored_issue_renders_as_the_writer_renders_it(tmp_path: Path) -> None:
    db = RecordingD1()
    deps = _deps(tmp_path, db, FakeLLM("{}"))
    deps.digest_store.save(_content(), raw_page=True)
    stored = deps.digest_store.load("2026-10-14", "evening")

    report = rerender_digest(deps, "2026-10-14", "evening")

    assert report.status == "ok"
    assert report.rendered == deps.html_writer.render(stored.content, raw_page=True, degraded=False)
    assert "2026-10-14-evening-raw.html" in report.rendered


def test_an_absent_issue_is_not_found(tmp_path: Path) -> None:
    deps = _deps(tmp_path, RecordingD1(), FakeLLM("{}"))

    report = rerender_digest(deps, "2026-10-14", "morning")

    assert report.status == "not_found"
    assert report.rendered is None


def test_a_rerender_writes_sends_and_calls_nothing(tmp_path: Path) -> None:
    db = RecordingD1()
    llm = FakeLLM(error=AssertionError("the LLM was called"))
    deps = _deps(tmp_path, db, llm)
    deps.digest_store.save(_content(), raw_page=True)
    db.statements.clear()
    published: list = []
    deps = replace(deps, publish_site=lambda pages, slug: published.append(slug) or True)

    rerender_digest(deps, "2026-10-14", "evening")

    assert db.written_tables() == set()
    assert llm.calls == []
    assert published == []


def test_a_missing_digest_store_is_an_error(tmp_path: Path) -> None:
    deps = replace(_deps(tmp_path, RecordingD1(), FakeLLM("{}")), digest_store=None)

    with pytest.raises(ValueError):
        rerender_digest(deps, "2026-10-14", "evening")
