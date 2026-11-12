"""The container draws the interest map from D1 on request; no run publishes it to Pages."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from fakes import FakeLLM, SqliteD1
from test_run_digest import FakeSource, make_deps

from cyris.adapters.store.d1 import D1Error
from cyris.adapters.store.interest import D1InterestReader
from cyris.domain.models import Article, Lane
from cyris.entrypoints.triage_server import TriageServer
from cyris.service_layer.run_digest import RunOptions, run_digest

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 24, 6, 0, tzinfo=UTC)


def _seeded_db() -> SqliteD1:
    db = SqliteD1()
    at = NOW.isoformat(timespec="seconds")
    for url, state, tags in [
        ("https://example.test/acc", "accepted", ["x", "y"]),
        ("https://example.test/rej", "rejected", ["y"]),
    ]:
        db.query(
            "INSERT INTO stored_articles (url, title, published_at, source_name, source_lane, "
            "first_seen_at, state, triaged_at) VALUES (?, ?, ?, 'Source', 'skim', ?, ?, ?)",
            [url, "Secret title", at, at, state, at],
        )
        for tag in tags:
            db.query(
                "INSERT INTO article_tags (article_url, tag, tagged_at) VALUES (?, ?, ?)",
                [url, tag, at],
            )
    db.query(
        "INSERT INTO suppressions (run_at, period, model, candidate_url, reason, "
        "nearest_down_url, down_cosine, up_cosine, outlier_score, until) "
        "VALUES (?, 'morning', 'm', 'https://example.test/cand', 'topic_near_downvote', "
        "'https://example.test/rej', 0.9, NULL, 0.1, ?)",
        [at, (NOW + timedelta(days=3)).isoformat(timespec="seconds")],
    )
    return db


@pytest.fixture
async def serve():
    clients: list[TestClient] = []

    async def start(reader) -> TestClient:
        server = TriageServer(interest_reader=reader, clock=lambda: NOW)
        client = TestClient(TestServer(server._app))
        await client.start_server()
        clients.append(client)
        return client

    yield start
    for client in clients:
        await client.close()


async def test_the_container_draws_the_map_from_d1_as_it_is_now(serve) -> None:
    client = await serve(D1InterestReader(_seeded_db()).read)

    resp = await client.get("/interest")

    assert resp.status == 200
    assert resp.content_type == "text/html"
    page = await resp.text()
    assert 'aria-label="Interest map"' in page
    assert ">x<" in page and ">y<" in page
    assert f"As of {NOW.isoformat(timespec='seconds')}" in page


async def test_the_map_carries_tags_and_counts_only(serve) -> None:
    client = await serve(D1InterestReader(_seeded_db()).read)

    page = await (await client.get("/interest")).text()

    assert "https://example.test" not in page
    assert "Secret title" not in page


async def test_a_deployment_without_d1_says_it_has_no_map(serve) -> None:
    client = await serve(None)

    resp = await client.get("/interest")

    assert resp.status == 409
    assert '"d1"' in await resp.text()


async def test_a_failed_read_says_why(serve) -> None:
    def down(now: str):
        raise D1Error("down")

    client = await serve(down)

    resp = await client.get("/interest")

    assert resp.status == 500
    assert "down" in await resp.text()


def _article() -> Article:
    return Article(
        id=1,
        title="Enterprise AI Adoption",
        url="https://example.com/ai",
        content="Enterprises accelerate AI adoption.",
        published_at=datetime.now(UTC) - timedelta(hours=1),
        source_name="TechSource",
        source_lane=Lane.READ,
        source_tags=["tech"],
    )


def _llm() -> FakeLLM:
    section = {
        "heading": "AI",
        "summary": "AI",
        "articles": [{"id": "0", "title": "Enterprise AI Adoption", "source": "TechSource"}],
    }
    return FakeLLM(
        [
            json.dumps({"scores": [{"id": 0, "score": 85, "language": "en"}]}),
            json.dumps({"sections": [section]}),
        ]
    )


async def test_a_publishing_run_puts_no_interest_page_on_pages(tmp_path: Path) -> None:
    deps, _ = make_deps(tmp_path, _llm(), FakeSource([_article()]))
    published: list[dict[str, bytes]] = []

    def publish_site(pages, slug):
        published.append(pages)
        return True

    deps = replace(deps, publish_site=publish_site, site_filenames=lambda: ["interest.html"])

    report = await run_digest(deps, RunOptions())

    assert report.status == "ok"
    [pages] = published
    assert not any("interest" in path for path in pages)
    assert b'href="interest.html"' not in pages["/index.html"]
