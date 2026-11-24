"""`cyris ingest` on a local SQLite file records each polled feed's health.

The command runs for real, over a real file, with the feeds answered at the HTTP edge. Each
poll of a shared case (`workers/rss/test/feed_health_cases.json`) is one `cyris ingest`, and the
rows left must be the ones the rss Worker's `recordHealth` leaves for the same outcomes.
"""

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from fakes import seeded_sqlite
from typer.testing import CliRunner

from cyris.adapters.store.feed_health import D1FeedHealth, D1FeedHealthWriter
from cyris.adapters.store.local_d1 import LocalD1Client
from cyris.adapters.store.source_store import D1SourceStore
from cyris.domain.models import SourceConfig
from cyris.entrypoints.cli import app

pytestmark = pytest.mark.integration

CASES = json.loads(
    (Path(__file__).parents[1] / "workers/rss/test/feed_health_cases.json").read_text("utf-8")
)["cases"]


def feed_xml(name: str) -> str:
    link = f"https://{name}.test/post/{datetime.now(UTC).timestamp()}"
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>F</title>'
        f"<item><title>Post</title><link>{link}</link><guid>{link}</guid>"
        f"<pubDate>{datetime.now(UTC).strftime('%a, %d %b %Y %H:%M:%S GMT')}</pubDate>"
        "<description>body</description></item></channel></rss>"
    )


class Clock(datetime):
    """`datetime` whose `now` is the poll's instant, so the stamps are the case's."""

    at: datetime

    @classmethod
    def now(cls, tz=None):
        return cls.at.astimezone(tz) if tz else cls.at


def install(tmp_path: Path) -> tuple[Path, Path, LocalD1Client]:
    state = tmp_path / "cyris.sqlite3"
    d1 = seeded_sqlite(state)
    config_path = tmp_path / "cyris.toml"
    config_path.write_text(
        f'[store]\nsqlite_path = "{state}"\n\n[agent_vault]\npath = "{tmp_path / "vault"}"\n'
    )
    return config_path, tmp_path / "sources.yaml", d1


def ingest(config_path: Path, sources_path: Path):
    return CliRunner().invoke(
        app, ["ingest", "--config", str(config_path), "--sources", str(sources_path)]
    )


def answering(outcomes: dict[str, str | None]):
    """A respx handler for `https://<feed>.test/feed`: the status its outcome names, else a feed."""

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.host.removesuffix(".test")
        error = outcomes[name]
        if error is None:
            return httpx.Response(200, text=feed_xml(name))
        return httpx.Response(int(re.fullmatch(r"HTTP (\d+)", error).group(1)))

    return handler


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_ingests_leave_the_rows_the_worker_leaves(
    case: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, sources_path, d1 = install(tmp_path)
    monkeypatch.setattr("cyris.adapters.fetch.rss_source.datetime", Clock)

    for poll in case["polls"]:
        d1.query("DELETE FROM sources")
        for outcome in poll["outcomes"]:
            name = outcome["name"]
            D1SourceStore(d1).upsert(SourceConfig(name=name, url=f"https://{name}.test/feed"))
        Clock.at = datetime.fromisoformat(poll["at"])
        with respx.mock:
            feeds = respx.get(url__regex=r"https://\w+\.test/feed").mock(
                side_effect=answering({o["name"]: o["error"] for o in poll["outcomes"]})
            )
            result = ingest(config_path, sources_path)
        assert feeds.call_count == len(poll["outcomes"])
        failed = [o for o in poll["outcomes"] if o["error"]]
        # One dead feed is the poll's to absorb; all of them is a failed fetch.
        assert result.exit_code == (1 if len(failed) == len(poll["outcomes"]) else 0), result.output

    rows = d1.query("SELECT * FROM feed_health ORDER BY name").rows
    assert {row.pop("name"): row for row in rows} == case["rows"]


def test_a_failed_then_a_successful_ingest_read_back_through_the_settings_reader(
    tmp_path: Path,
) -> None:
    """The row `cyris ingest` leaves is the one `/settings` and `cyris doctor` read."""
    config_path, sources_path, d1 = install(tmp_path)
    D1SourceStore(d1).upsert(SourceConfig(name="Blog", url="https://blog.test/feed"))

    with respx.mock:
        blog = respx.get("https://blog.test/feed").mock(return_value=httpx.Response(503))
        assert ingest(config_path, sources_path).exit_code == 1
    assert blog.call_count == 1
    failing = D1FeedHealth(d1).read()["Blog"]
    assert (failing.consecutive_failures, failing.last_error) == (1, "HTTP 503")

    with respx.mock:
        blog = respx.get("https://blog.test/feed").mock(
            return_value=httpx.Response(200, text=feed_xml("blog"))
        )
        assert ingest(config_path, sources_path).exit_code == 0
    assert blog.call_count == 1
    ok = D1FeedHealth(d1).read()["Blog"]
    assert (ok.consecutive_failures, ok.last_error, ok.last_failed_at) == (
        0,
        "HTTP 503",
        failing.last_failed_at,
    )
    assert ok.last_ok_at is not None


def test_a_health_write_that_fails_fails_no_ingest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    config_path, sources_path, d1 = install(tmp_path)
    D1SourceStore(d1).upsert(SourceConfig(name="Blog", url="https://blog.test/feed"))

    def refuse(self, outcomes, at):
        raise RuntimeError("disk full")

    monkeypatch.setattr(D1FeedHealthWriter, "record", refuse)
    with respx.mock, caplog.at_level(logging.WARNING):
        blog = respx.get("https://blog.test/feed").mock(
            return_value=httpx.Response(200, text=feed_xml("blog"))
        )
        result = ingest(config_path, sources_path)

    assert blog.call_count == 1

    assert result.exit_code == 0, result.output
    assert "Stored 1 new article" in result.output
    assert any("feed_health" in r.getMessage() for r in caplog.records)
