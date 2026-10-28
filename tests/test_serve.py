"""Tests for `cyris serve`, the digest pages' read-only HTTP server."""

from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from cyris.entrypoints.serve import PageServer

pytestmark = pytest.mark.integration


async def _client(output_dir: Path) -> TestClient:
    test_client = TestClient(TestServer(PageServer(output_dir).build_app()))
    await test_client.start_server()
    return test_client


@pytest.fixture
def pages(tmp_path: Path) -> Path:
    output_dir = tmp_path / "html"
    output_dir.mkdir()
    (output_dir / "index.html").write_text("archive", encoding="utf-8")
    (output_dir / "2026-10-28-morning.html").write_text("issue", encoding="utf-8")
    (output_dir / "2026-10-28-morning-raw.html").write_text("raw", encoding="utf-8")
    (output_dir / "favicon.svg").write_text("x", encoding="utf-8")
    return output_dir


async def test_the_writing_alone_defines_what_is_served(pages: Path) -> None:
    client = await _client(pages)
    try:
        # `/2026-10-28-morning` resolved as an issue URL is the Pages behaviour.
        for path, body in (
            ("/", "archive"),
            ("/2026-10-28-morning", "issue"),
            ("/2026-10-28-morning.html", "issue"),
            ("/2026-10-28-morning-raw", "raw"),
            ("/favicon.svg", "x"),
        ):
            response = await client.get(path)
            assert response.status == 200
            assert await response.text() == body
    finally:
        await client.close()


async def test_nothing_else_answers_least_of_all_the_vote_probe(pages: Path) -> None:
    """A 404 to `GET /api/vote` is what hides the digest's vote buttons on these pages."""
    client = await _client(pages)
    try:
        for path in (
            "/api/vote",
            "/settings",
            "/2026-10-28-morning.html%2f",
            "/..%2f..%2fcyris.toml",
            "/no-such-issue",
        ):
            assert (await client.get(path)).status == 404, path
    finally:
        await client.close()


async def test_a_page_written_after_start_is_served_without_a_restart(pages: Path) -> None:
    client = await _client(pages)
    try:
        (pages / "2026-10-28-evening.html").write_text("fresh", encoding="utf-8")
        response = await client.get("/2026-10-28-evening")
        assert response.status == 200
        assert await response.text() == "fresh"
    finally:
        await client.close()


async def test_a_missing_output_directory_is_refused_before_the_loop(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="output_dir"):
        PageServer(tmp_path / "nowhere").build_app()


async def test_an_address_that_cannot_listen_names_it(pages: Path) -> None:
    # An address no interface holds. Port 1 would also fail on Linux but binds
    # without complaint on Windows, so it is not a case here.
    with pytest.raises(RuntimeError, match="cannot listen on 100.127.255.254:8767"):
        await PageServer(pages).start("100.127.255.254", 8767)


async def test_a_port_already_listening_is_the_callers_to_report(pages: Path) -> None:
    runner = await PageServer(pages).start("127.0.0.1", 0)
    port = runner.addresses[0][1]
    try:
        with pytest.raises(RuntimeError, match=f"cannot listen on 127.0.0.1:{port}"):
            await PageServer(pages).start("127.0.0.1", port)
    finally:
        await runner.cleanup()
