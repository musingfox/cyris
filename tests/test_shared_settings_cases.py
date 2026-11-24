"""The settings page's contract holds on TriageServer as on the hosted Worker (ADR-0043).

`workers/app/test/shared_settings_cases.json` is the one list both suites read: vitest runs it
against the hosted Worker (`workers/app/test/hosted_shared_settings.test.js`), and this runs it
against TriageServer, as `cyris triage-ui` starts it and with the hosted edition's locks, so the
same request gets the same answer from both. Its `sources` cases are the Sources tab's, over a
`sources` table, `feed_health` and `stored_articles` the case sets; a case only a hosted tenant is
held to says `hosted` in its `editions` and is not run here. Its pins are held to the run's own
`pin_tenant_settings`, which the Worker's copy shows. Each request goes through the route the app
resolves it to, with no socket.
"""

import asyncio
import json
import re
from pathlib import Path
from unittest import mock

import pytest
from aiohttp.streams import StreamReader
from aiohttp.test_utils import make_mocked_request
from fakes import SqliteD1

from cyris.adapters.hosted import pin_tenant_settings
from cyris.adapters.store import feed_health
from cyris.adapters.store.feed_health import D1FeedHealth
from cyris.adapters.store.source_store import D1SourceStore
from cyris.config import GRADE_D_KEYS
from cyris.entrypoints.triage_server import TriageServer

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / "workers/app/test/shared_settings_cases.json").read_text("utf-8"))
HOSTED_LOCKS = {key: lock["reason"] for key, lock in CASES["editions"]["hosted"]["locked"].items()}
# A home holding every key, in English, so the answers are worded as the hosted tenant's are.
STORED = {"digest.output_language": "en"}


class FakeSettings:
    """Stands in for `D1Settings`; records every write."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def set(self, values: dict) -> None:
        self.calls.append(dict(values))


async def _call(server: TriageServer, method: str, path: str, body=None) -> tuple[int, dict]:
    """One request, through the route the app resolves it to, with no socket."""
    payload = StreamReader(mock.Mock(_reading_paused=False), 2**16, loop=asyncio.get_running_loop())
    if body is not None:
        payload.feed_data(json.dumps(body).encode())
    payload.feed_eof()
    request = make_mocked_request(
        method,
        path,
        headers={"Content-Type": "application/json"},
        payload=payload,
        app=server._app,
    )
    match = await server._app.router.resolve(request)
    # What the app sets before a handler runs, so `match_info["name"]` reads.
    request._match_info = match
    response = await match.handler(request)
    return response.status, json.loads(response.text)


async def test_the_answer_has_the_shared_shape() -> None:
    server = TriageServer(settings=FakeSettings(), values=STORED)
    _, data = await _call(server, "GET", "/api/settings")

    assert sorted(data) == CASES["answer_keys"]
    assert all(sorted(p) == CASES["provider_keys"] for p in data["providers"])
    assert all(sorted(p) == CASES["provider_keys"] for p in data["embedding_providers"])
    assert all(set(CASES["field_keys"]) <= set(f) for f in data["fields"].values())


async def test_a_server_the_cli_starts_answers_the_self_hosted_edition() -> None:
    server = TriageServer(settings=FakeSettings(), values=STORED)
    _, data = await _call(server, "GET", "/api/settings")

    edition = CASES["editions"]["self_hosted"]
    assert data["locked"] == edition["locked"]


def test_the_hosted_edition_locks_only_settings_for_one_of_the_two_reasons() -> None:
    """The constructor refuses any other key or reason, so the hosted locks are well formed."""
    TriageServer(locked=HOSTED_LOCKS)

    assert set(HOSTED_LOCKS) <= set(GRADE_D_KEYS)


async def test_a_server_given_the_hosted_locks_answers_them() -> None:
    server = TriageServer(settings=FakeSettings(), values=STORED, locked=HOSTED_LOCKS)

    _, data = await _call(server, "GET", "/api/settings")

    assert data["locked"] == CASES["editions"]["hosted"]["locked"]


@pytest.mark.parametrize("case", CASES["writes"], ids=lambda case: case["name"])
async def test_a_values_write_gets_the_answer_the_hosted_worker_gives(case) -> None:
    settings = FakeSettings()
    server = TriageServer(settings=settings, values=STORED, locked=HOSTED_LOCKS)

    status, answer = await _call(server, "POST", "/api/settings/values", case["body"])

    assert (status, answer) == (case["status"], case["answer"])
    if case["status"] != 200:
        assert settings.calls == []
        return
    _, data = await _call(server, "GET", "/api/settings")
    assert {key: data["values"][key] for key in case["reads_back"]} == case["reads_back"]


@pytest.mark.parametrize("case", CASES["pins"], ids=lambda case: case["name"])
def test_what_the_page_shows_a_tenant_is_what_its_run_takes(case) -> None:
    pinned = pin_tenant_settings(case["stored"])

    assert {key: pinned.get(key) for key in case["shown"]} == case["shown"]


_BOTH = ["hosted", "self_hosted"]
SOURCE_CASES = [c for c in CASES["sources"]["cases"] if "self_hosted" in c.get("editions", _BOTH)]
HOSTED_ONLY = [case for case in CASES["sources"]["cases"] if case not in SOURCE_CASES]


def _arranged(case: dict) -> SqliteD1:
    """A store holding what the case says, as the hosted Worker's tenant tables hold it."""
    db = SqliteD1()
    for row in case.get("rows", []):
        db.query(
            "INSERT INTO sources (name, url, type, config) VALUES (?, ?, ?, ?)",
            [row["name"], row["url"], row["type"], json.dumps(row["config"])],
        )
    for row in case.get("feed_health", []):
        db.query(
            "INSERT INTO feed_health (name, consecutive_failures, last_error, last_failed_at, "
            "last_ok_at) VALUES (?, ?, ?, ?, ?)",
            [
                row[key]
                for key in (
                    "name",
                    "consecutive_failures",
                    "last_error",
                    "last_failed_at",
                    "last_ok_at",
                )
            ],
        )
    for n, row in enumerate(case.get("articles", [])):
        db.query(
            "INSERT INTO stored_articles (url, published_at, source_name, source_lane, "
            "first_seen_at) VALUES (?, ?, ?, 'skim', ?)",
            [
                f"https://stored.example/{n}",
                row["first_seen_at"],
                row["source_name"],
                row["first_seen_at"],
            ],
        )
    return db


async def _sources(server: TriageServer) -> list[dict]:
    _, data = await _call(server, "GET", "/api/sources")
    return data["sources"]


def _without_health(sources: list[dict]) -> list[dict]:
    return [{k: v for k, v in source.items() if k != "health"} for source in sources]


@pytest.mark.parametrize("case", SOURCE_CASES, ids=lambda case: case["name"])
async def test_a_sources_request_gets_the_answer_the_hosted_worker_gives(case) -> None:
    db = _arranged(case)
    server = TriageServer(source_store=D1SourceStore(db), feed_health=D1FeedHealth(db))
    before = await _sources(server)

    status, answer = await _call(server, case["method"], case["path"], case.get("body"))

    assert status == case["status"]
    if case["method"] == "GET":
        assert answer.pop("max_sources") == CASES["sources"]["max_sources"]["self_hosted"]
    assert answer == case["answer"]
    if case["method"] == "GET":
        return
    after = await _sources(server)
    if case.get("reads_back") is not None:
        assert _without_health(after) == case["reads_back"]
    else:
        assert after == before


def test_every_source_case_a_hosted_tenant_alone_is_held_to_is_a_refusal_or_a_rewrite() -> None:
    """TriageServer has no URL rule, field lock or cap, so these run in vitest only; they still
    answer in TriageServer's refusal shape, `{ok, error, field?}`."""
    assert HOSTED_ONLY
    for case in HOSTED_ONLY:
        assert set(case["answer"]) <= {"ok", "error", "field", "name", "note"}, case["name"]
        assert case["answer"]["ok"] is (case["status"] == 200), case["name"]
        if not case["answer"]["ok"]:
            assert case["answer"]["error"], case["name"]


async def test_a_server_with_no_cap_says_so() -> None:
    server = TriageServer(source_store=D1SourceStore(SqliteD1()))
    _, data = await _call(server, "GET", "/api/sources")

    assert data["max_sources"] is None
    assert CASES["sources"]["max_sources"]["self_hosted"] is None


def test_the_feed_health_thresholds_are_the_one_file_the_hosted_worker_reads_too() -> None:
    rules = json.loads((ROOT / "src/cyris/feed_health_rules.json").read_text("utf-8"))
    hosted = (ROOT / "workers/app/src/hosted_feed_health.js").read_text("utf-8")

    assert rules["unhealthy_failure_streak"] == feed_health.UNHEALTHY_FAILURE_STREAK
    assert rules["quiet_days"] == feed_health.QUIET_DAYS
    assert re.search(r'import RULES from "[./]+/src/cyris/feed_health_rules\.json"', hosted)
    assert (
        CASES["sources"]["max_sources"]["hosted"]
        == json.loads((ROOT / "workers/app/src/hosted_settings_policy.json").read_text("utf-8"))[
            "max_sources"
        ]
    )
