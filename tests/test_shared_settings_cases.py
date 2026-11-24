"""The settings page's contract holds on TriageServer as on the hosted Worker (ADR-0043).

`workers/app/test/shared_settings_cases.json` is the one list both suites read: vitest runs it
against the hosted Worker (`workers/app/test/hosted_shared_settings.test.js`), and this runs it
against TriageServer, as `cyris triage-ui` starts it and with the hosted edition's locks, so the
same request gets the same answer from both, or the answer a case names for a self-hosted server
where the editions' own limits differ. Its pins are held to the run's own
`pin_tenant_settings`, which the Worker's copy shows. Each request goes through the route the app
resolves it to, with no socket.
"""

import asyncio
import json
from pathlib import Path
from unittest import mock

import pytest
from aiohttp.streams import StreamReader
from aiohttp.test_utils import make_mocked_request

from cyris.adapters.hosted import pin_tenant_settings
from cyris.config import GRADE_D_KEYS
from cyris.entrypoints.triage_server import TriageServer

pytestmark = pytest.mark.unit

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
    assert data["limits"] == edition["limits"]


def test_the_hosted_edition_locks_only_settings_for_one_of_the_two_reasons() -> None:
    """The constructor refuses any other key or reason, so the hosted locks are well formed."""
    TriageServer(locked=HOSTED_LOCKS)

    assert set(HOSTED_LOCKS) <= set(GRADE_D_KEYS)


async def test_a_server_given_the_hosted_locks_answers_them() -> None:
    server = TriageServer(settings=FakeSettings(), values=STORED, locked=HOSTED_LOCKS)

    _, data = await _call(server, "GET", "/api/settings")

    assert data["locked"] == CASES["editions"]["hosted"]["locked"]
    # Its limits are its own, whatever it locks.
    assert data["limits"] == CASES["editions"]["self_hosted"]["limits"]


async def _holds_to(server: TriageServer, settings: FakeSettings, path: str, case: dict) -> None:
    """`case`'s request to `path` gets its answer here, or the one it names for this edition."""
    expected = case.get("self_hosted", case)
    status, answer = await _call(server, "POST", path, case["body"])

    assert status == expected["status"], answer
    if "refuses" in expected:
        assert answer["ok"] is False and answer["field"] == expected["refuses"], answer
    else:
        assert answer == expected["answer"]
    if status != 200:
        assert settings.calls == []
        return
    _, data = await _call(server, "GET", "/api/settings")
    reads_back = expected.get("reads_back", {})
    assert {key: data["values"][key] for key in reads_back} == reads_back


@pytest.mark.parametrize("case", CASES["writes"], ids=lambda case: case["name"])
async def test_a_values_write_gets_the_answer_the_hosted_worker_gives(case) -> None:
    settings = FakeSettings()
    server = TriageServer(settings=settings, values=STORED, locked=HOSTED_LOCKS)

    await _holds_to(server, settings, "/api/settings/values", case)


@pytest.mark.parametrize("case", CASES["schedules"], ids=lambda case: case["name"])
async def test_a_schedule_write_gets_the_answer_the_hosted_worker_gives(case) -> None:
    settings = FakeSettings()
    server = TriageServer(settings=settings, values=STORED, locked=HOSTED_LOCKS)

    await _holds_to(server, settings, "/api/settings/schedule", case)


@pytest.mark.parametrize("case", CASES["route_locks"], ids=lambda case: case["name"])
async def test_a_route_refuses_only_a_change_to_a_locked_key(case) -> None:
    settings = FakeSettings()
    values = {**STORED, **case["stored"]}
    server = TriageServer(settings=settings, values=values, locked=case["locked"])

    await _holds_to(server, settings, case["path"], case)


@pytest.mark.parametrize("case", CASES["pins"], ids=lambda case: case["name"])
def test_what_the_page_shows_a_tenant_is_what_its_run_takes(case) -> None:
    pinned = pin_tenant_settings(case["stored"])

    assert {key: pinned.get(key) for key in case["shown"]} == case["shown"]
