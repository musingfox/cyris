"""A publishing run writes the interest map beside its issue; a failed map costs no issue."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fakes import FakeLLM, SqliteD1
from test_run_digest import FakeSource, make_deps

from cyris.adapters.store.d1 import D1Error
from cyris.adapters.store.interest import D1InterestReader
from cyris.domain.models import Article, Tier
from cyris.service_layer.run_digest import RunOptions, run_digest

pytestmark = pytest.mark.integration

INTEREST = "/interest.html"


def _article() -> Article:
    return Article(
        id=1,
        title="Enterprise AI Adoption",
        url="https://example.com/ai",
        content="Enterprises accelerate AI adoption.",
        published_at=datetime.now(UTC) - timedelta(hours=1),
        source_name="TechSource",
        source_tier=Tier.SUMMARIZE,
        source_tags=["tech"],
    )


def _llm() -> FakeLLM:
    return FakeLLM(
        [
            json.dumps({"scores": [{"id": 1, "score": 85, "language": "en"}]}),
            json.dumps(
                {
                    "sections": [
                        {
                            "heading": "AI",
                            "summary": "AI",
                            "articles": [
                                {
                                    "id": "0",
                                    "title": "Enterprise AI Adoption",
                                    "source": "TechSource",
                                }
                            ],
                        }
                    ]
                }
            ),
        ]
    )


def _seeded_db() -> SqliteD1:
    db = SqliteD1()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    for url, state, triaged, tags in [
        ("https://example.test/acc", "accepted", now, ["x", "y"]),
        ("https://example.test/rej", "rejected", now, ["y"]),
    ]:
        db.query(
            "INSERT INTO stored_articles (url, title, published_at, source_name, source_tier, "
            "first_seen_at, state, triaged_at) VALUES (?, ?, ?, 'Source', 'filter', ?, ?, ?)",
            [url, "Secret title", now, now, state, triaged],
        )
        for tag in tags:
            db.query(
                "INSERT INTO article_tags (article_url, tag, tagged_at) VALUES (?, ?, ?)",
                [url, tag, now],
            )
    until = (datetime.now(UTC) + timedelta(days=3)).isoformat(timespec="seconds")
    db.query(
        "INSERT INTO suppressions (run_at, period, model, candidate_url, reason, "
        "nearest_down_url, down_cosine, up_cosine, outlier_score, until) "
        "VALUES (?, 'morning', 'm', 'https://example.test/cand', 'topic_near_downvote', "
        "'https://example.test/rej', 0.9, NULL, 0.1, ?)",
        [now, until],
    )
    return db


async def _run(
    tmp_path: Path, *, reader, render_fails: bool = False, live: list[str] | None = None
):
    deps, _ = make_deps(tmp_path, _llm(), FakeSource([_article()]))
    published: list[dict[str, bytes]] = []

    def publish_site(pages, slug):
        published.append(pages)
        return True

    summaries: list[dict] = []
    if render_fails:

        def broken(*args, **kwargs):
            raise RuntimeError("render broke")

        deps.html_writer.render_interest = broken
    deps = replace(
        deps,
        publish_site=publish_site,
        interest_reader=reader,
        record_run=summaries.append,
        site_filenames=lambda: live or [],
    )
    report = await run_digest(deps, RunOptions())
    return report, published, summaries


async def test_the_run_publishes_the_interest_map_and_links_it_from_the_archive(
    tmp_path: Path,
) -> None:
    reader = D1InterestReader(_seeded_db()).read

    _, [pages], _ = await _run(tmp_path, reader=reader)

    assert INTEREST in pages
    assert b'href="interest.html"' in pages["/index.html"]


async def test_a_failed_reader_omits_the_map_and_still_publishes_the_issue(tmp_path: Path) -> None:
    def down(now: str):
        raise D1Error("down")

    _, [pages], summaries = await _run(tmp_path, reader=down)

    assert INTEREST not in pages
    assert any(key.endswith("-morning.html") for key in pages)
    assert summaries[-1]["interest_page_error"] == "down"


async def test_no_reader_means_no_map(tmp_path: Path) -> None:
    _, [pages], _ = await _run(tmp_path, reader=None)

    assert INTEREST not in pages


async def test_the_map_carries_tags_and_counts_only(tmp_path: Path) -> None:
    reader = D1InterestReader(_seeded_db()).read

    _, [pages], _ = await _run(tmp_path, reader=reader)

    page = pages[INTEREST]
    assert b"https://example.test" not in page
    assert b"Secret title" not in page
    assert b"tok-secret-123" not in page


async def test_a_failed_render_still_publishes_and_lists_the_map_from_the_site(
    tmp_path: Path,
) -> None:
    reader = D1InterestReader(_seeded_db()).read

    report, [pages], summaries = await _run(
        tmp_path, reader=reader, render_fails=True, live=[INTEREST.lstrip("/")]
    )

    assert report.status == "ok"
    assert INTEREST not in pages
    assert summaries[-1]["interest_page_error"] == "render broke"
    assert b'href="interest.html"' in pages["/index.html"]


async def test_the_map_is_built_with_the_private_archive_on_or_off(
    tmp_path: Path, monkeypatch
) -> None:
    reader = D1InterestReader(_seeded_db()).read

    monkeypatch.setenv("CYRIS_PRIVATE_ARCHIVE", "true")
    _, [on], _ = await _run(tmp_path / "on", reader=reader)
    monkeypatch.delenv("CYRIS_PRIVATE_ARCHIVE")
    _, [off], _ = await _run(tmp_path / "off", reader=reader)

    assert INTEREST in on
    assert INTEREST in off
