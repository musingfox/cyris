"""GET /api/onboarding/first-issue: the key the wizard's first issue takes, and its run.

The run itself is the app Worker's `POST /run?period=&date=` (workers/app); this
route only picks the key, says whether an issue already holds it, and reads the
newest recorded run back, which is how the page tells when its run ended.
Which key is safe at each hour is `tests/test_first_issue_period.py`'s.
"""

import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from cyris.domain.models import SourceConfig
from cyris.entrypoints.triage_server import TriageServer

pytestmark = pytest.mark.unit

TAIPEI = ZoneInfo("Asia/Taipei")
VALUES = {"general.timezone": "Asia/Taipei", "general.digest_schedule": ["08:00", "20:00"]}
A_SOURCE = SourceConfig(name="Feed", url="https://feed.test/rss")


class FakeSourceStore:
    def __init__(self, sources: dict[str, SourceConfig]) -> None:
        self.sources = sources

    def list_sources(self) -> dict[str, SourceConfig]:
        return dict(self.sources)


class FakeDigestStore:
    def __init__(self, issues: list[tuple[str, str]]) -> None:
        self._issues = issues

    def issues(self) -> list[tuple[str, str]]:
        return list(self._issues)


class FakeRunLog:
    def __init__(self, newest: dict | None) -> None:
        self._newest = newest

    def newest(self) -> dict | None:
        return self._newest


class FailingRunLog:
    def newest(self) -> dict | None:
        raise RuntimeError("D1 timed out")


RUN = {
    "id": 7,
    "finished_at": "2026-10-20T01:00:00+00:00",
    "status": "ok",
    "period": "evening",
    "digest_url": "https://x/2026-10-19-evening",
    "error": None,
    "failed_sources": [],
}


def _server(
    *,
    at: datetime,
    sources: dict | None = None,
    issues: list | None = None,
    run_log=None,
    values: dict | None = None,
    partial: bool = False,
) -> TriageServer:
    if partial:
        return TriageServer(values=VALUES, clock=lambda: at)
    return TriageServer(
        values=VALUES if values is None else values,
        source_store=FakeSourceStore({"Feed": A_SOURCE} if sources is None else sources),
        digest_store=FakeDigestStore(issues or []),
        run_log=run_log or FakeRunLog(RUN),
        clock=lambda: at,
    )


async def _get(server: TriageServer) -> tuple[int, dict]:
    response = await server._handle_get_first_issue(None)
    return response.status, json.loads(response.text)


def _local(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 20, hour, minute, tzinfo=TAIPEI)


async def test_between_the_two_hours_the_first_issue_is_todays_morning() -> None:
    status, body = await _get(_server(at=_local(10, 30)))

    assert status == 200
    assert (body["date"], body["period"], body["published"]) == ("2026-10-20", "morning", False)
    assert body["today"] == "2026-10-20"
    assert (body["sources"], body["timezone"], body["hours"]) == (
        1,
        "Asia/Taipei",
        ["08:00", "20:00"],
    )


async def test_the_date_is_the_deployments_own_not_utcs() -> None:
    """12:30 UTC is 20:30 in Taipei: the period and the date are read on Taipei's clock."""
    status, body = await _get(_server(at=datetime(2026, 10, 20, 12, 30, tzinfo=UTC)))

    assert status == 200
    assert (body["date"], body["period"]) == ("2026-10-20", "evening")


async def test_before_the_morning_hour_the_key_is_yesterdays_evening() -> None:
    _, body = await _get(_server(at=_local(6, 30)))

    assert (body["date"], body["period"], body["today"]) == ("2026-10-19", "evening", "2026-10-20")


async def test_published_is_read_for_yesterdays_key_before_the_morning() -> None:
    held = await _get(_server(at=_local(6, 30), issues=[("2026-10-19", "evening")]))
    other = await _get(_server(at=_local(6, 30), issues=[("2026-10-20", "evening")]))

    assert (held[1]["published"], other[1]["published"]) == (True, False)


async def test_late_in_the_evening_the_key_stays_todays_evening() -> None:
    _, body = await _get(_server(at=_local(23, 55)))

    assert (body["date"], body["period"]) == ("2026-10-20", "evening")


async def test_an_issue_already_under_the_key_is_said() -> None:
    _, body = await _get(_server(at=_local(10), issues=[("2026-10-20", "morning")]))

    assert body["published"] is True


async def test_an_issue_under_another_key_is_not_this_one() -> None:
    _, body = await _get(
        _server(at=_local(10), issues=[("2026-10-19", "morning"), ("2026-10-20", "evening")])
    )

    assert body["published"] is False


async def test_the_newest_run_rides_along_for_the_page_to_wait_on() -> None:
    _, body = await _get(_server(at=_local(10)))

    assert body["run"] == RUN


async def test_no_run_yet_is_null() -> None:
    _, body = await _get(_server(at=_local(10), run_log=FakeRunLog(None)))

    assert body["run"] is None


async def test_an_empty_table_is_counted_as_no_source() -> None:
    _, body = await _get(_server(at=_local(10), sources={}))

    assert body["sources"] == 0


async def test_a_partial_server_has_no_run_log_to_wait_on() -> None:
    status, body = await _get(_server(at=_local(10), partial=True))

    assert status == 409
    assert body["ok"] is False


async def test_a_schedule_no_tick_can_honour_is_refused_with_where_to_fix_it() -> None:
    values = {**VALUES, "general.digest_schedule": ["08:30", "20:00"]}

    status, body = await _get(_server(at=_local(10), values=values))

    assert status == 409
    assert "first two steps" in body["error"]


async def test_an_unknown_timezone_is_refused() -> None:
    status, body = await _get(
        _server(at=_local(10), values={**VALUES, "general.timezone": "Mars/Base"})
    )

    assert status == 409
    assert body["ok"] is False


async def test_a_failed_read_says_why() -> None:
    status, body = await _get(_server(at=_local(10), run_log=FailingRunLog()))

    assert status == 500
    assert body["error"] == "D1 timed out"
