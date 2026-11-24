"""An edition locks a settings key: the page is told why, and no write route takes it (ADR-0043).

A `TriageServer` started by `cyris triage-ui` locks nothing. One built with locks names each in
`GET /api/settings` and refuses every route that would write one, before any probe or store
call. Each request goes through the route the app resolves it to, with no socket.
"""

import asyncio
import json
from unittest import mock

import pytest
from aiohttp.streams import StreamReader
from aiohttp.test_utils import make_mocked_request

from cyris.entrypoints.triage_server import TriageServer
from cyris.i18n import translator

pytestmark = pytest.mark.unit


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


async def test_a_server_the_cli_starts_locks_nothing() -> None:
    _, data = await _call(TriageServer(settings=FakeSettings()), "GET", "/api/settings")

    assert data["locked"] == {}


async def test_a_locked_key_is_named_with_its_reason() -> None:
    server = TriageServer(settings=FakeSettings(), locked={"digest.style_prompt": "plan-limit"})

    _, data = await _call(server, "GET", "/api/settings")

    assert data["locked"] == {"digest.style_prompt": {"reason": "plan-limit"}}


@pytest.mark.parametrize(
    ("path", "body", "key"),
    [
        ("/api/settings", {"provider": "none", "model": ""}, "llm_provider.provider"),
        ("/api/settings/schedule", {"times": ["07:00", "19:00"]}, "general.digest_schedule"),
        ("/api/settings/values", {"values": {"digest.style_prompt": "x"}}, "digest.style_prompt"),
        (
            "/api/settings/vote-similarity",
            {"enabled": False, "provider": "workers_ai", "model": "", "max_seeds": 200},
            "vote_similarity.enabled",
        ),
        ("/api/settings/notify", {"off": True}, "notify.discord_webhook_url"),
        ("/api/settings/email", {"email_to": "", "email_from": ""}, "notify.email_to"),
    ],
    ids=["llm", "schedule", "values", "vote", "notify", "email"],
)
async def test_every_write_route_refuses_a_locked_key_and_stores_nothing(path, body, key) -> None:
    settings = FakeSettings()
    server = TriageServer(settings=settings, locked={key: "operator-set"})

    status, answer = await _call(server, "POST", path, body)

    t = translator("en")
    why = t("lock_reason.operator-set")
    error = t("settings_api.locked", label=t(f"settings_field.{key}"), reason=why)
    assert status == 400
    assert answer == {"ok": False, "field": key, "error": error}
    assert settings.calls == []


async def test_a_lock_on_one_key_leaves_the_others_on_its_route_writable() -> None:
    settings = FakeSettings()
    server = TriageServer(settings=settings, locked={"digest.style_prompt": "operator-set"})

    status, answer = await _call(
        server, "POST", "/api/settings/values", {"values": {"digest.output_language": "zh-Hant"}}
    )

    assert status == 200, answer
    assert settings.calls == [{"digest.output_language": "zh-Hant"}]


@pytest.mark.parametrize(
    "locked",
    [{"no.such_key": "operator-set"}, {"digest.style_prompt": "a-whim"}],
    ids=["unknown key", "unknown reason"],
)
def test_a_lock_names_a_setting_and_one_of_the_two_reasons(locked) -> None:
    with pytest.raises(ValueError, match="cannot lock"):
        TriageServer(locked=locked)
