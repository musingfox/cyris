"""The wizard's saves alone leave a new local install with every setting a run needs.

A `TriageServer` is built over a real `sqlite` store as `cyris triage-ui` builds it, and
each settings request the wizard's Next sends (`static/onboarding.js`) is built, as the
page builds it, from the server's own `GET /api/settings` answer, then handed to the
route's handler with no socket. The store is read back through `load_effective_config`,
as `cyris run` reads it. Which requests a browser actually sends, and when, is
`scripts/onboarding_probe.py`'s to check; `tests/test_e2e_local_install.py` walks the
whole wizard over HTTP to a first issue.
"""

import asyncio
import json
import shutil
import sqlite3
from pathlib import Path
from unittest import mock

import pytest
from aiohttp.streams import StreamReader
from aiohttp.test_utils import make_mocked_request

from cyris.bootstrap import build_settings, load_effective_config
from cyris.config import STARTING_VALUES
from cyris.diagnostics.doctor import Check
from cyris.entrypoints.triage_server import TriageServer

pytestmark = pytest.mark.integration

EXAMPLE = Path(__file__).resolve().parent.parent / "cyris.toml.example"

# What the reader answers on the steps that ask without a starting value.
ANSWERS = {
    "digest.output_language": "en",
    "general.timezone": "Asia/Taipei",
    "general.digest_schedule": ["08:00", "20:00"],
    "llm_provider.provider": "none",
    "llm_provider.model": "",
}
# A cloud trial's first-boot push, before its reader opens the wizard (ADR-0020): every
# value differs from its starting value, so one the wizard wrote over would show.
PUSHED = {
    "llm_provider.provider": "gemini",
    "llm_provider.model": "gemini-2.5-flash",
    "notify.discord_webhook_url": "https://discord.com/api/webhooks/1/trial",
    "notify.email_to": "reader@example.org",
    "notify.email_from": "digest@example.org",
    "vote_similarity.provider": "gemini",
    "vote_similarity.max_seeds": 50,
    "digest.max_articles_per_digest": 12,
    "digest.rank_by_preference": False,
    "digest.style_prompt": "Short and dry.",
}


@pytest.fixture
def install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A local install in `tmp_path`: `cyris.toml.example` copied unchanged, on the default path."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CYRIS_STORE_SQLITE_PATH")
    return Path(shutil.copy(EXAMPLE, tmp_path / "cyris.toml"))


def _config(config: Path):
    return load_effective_config(config, config.parent / "sources.yaml")


def _server(config: Path) -> TriageServer:
    """The server `cyris triage-ui` starts, as far as the settings routes read it."""
    cfg = _config(config)
    return TriageServer(
        settings=build_settings(cfg),
        values={**cfg.present_settings(), **cfg.defaulted_settings()},
    )


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


async def _walk(server: TriageServer) -> list[tuple[str, dict]]:
    """The settings requests of a reader who answers what is unset and presses Next.

    Built as `onboarding.js` builds them: a step whose field already holds a stored
    value is left as loaded, so it sends nothing; leaving Notifications turns off only
    what was never set; the model step sends the provider while none is stored, and
    each tuned setting the server offered a starting value for, as it stands.
    """
    _, state = await _call(server, "GET", "/api/settings")
    values, starting, fields = state["values"], state["starting"], state["fields"]
    sent: list[tuple[str, dict]] = []

    async def send(path: str, body: dict) -> None:
        status, answer = await _call(server, "POST", path, body)
        assert status == 200, (path, body, answer)
        sent.append((path, body))

    unset = {key: ANSWERS[key] for key in ANSWERS if values[key] is None}
    language = {
        k: v for k, v in unset.items() if k in ("digest.output_language", "general.timezone")
    }
    if language:
        await send("/api/settings/values", {"values": language})
    if "general.digest_schedule" in unset:
        await send("/api/settings/schedule", {"times": unset["general.digest_schedule"]})
    if values["notify.discord_webhook_url"] is None:
        await send("/api/settings/notify", {"off": True})
    if values["notify.email_to"] is None or values["notify.email_from"] is None:
        await send("/api/settings/email", {"email_to": "", "email_from": ""})
    if "llm_provider.provider" in unset:
        await send("/api/settings", {"provider": unset["llm_provider.provider"], "model": ""})
    vote_keys = [key for key, field in fields.items() if field["route"] == "vote"]
    if any(key in starting for key in vote_keys):
        shown = {key: starting[key] if key in starting else values[key] for key in vote_keys}
        await send(
            "/api/settings/vote-similarity",
            {key.split(".", 1)[1]: value for key, value in shown.items()},
        )
    plain = {key: value for key, value in starting.items() if fields[key]["route"] == "plain"}
    if plain:
        await send("/api/settings/values", {"values": plain})
    return sent


def _stored(config: Path) -> dict:
    return _config(config).present_settings()


async def test_an_empty_store_is_complete_after_the_wizard(install: Path) -> None:
    assert _config(install).missing_settings != []

    await _walk(_server(install))

    assert _config(install).missing_settings == []


async def test_the_saves_land_in_the_file_at_the_default_path(install: Path) -> None:
    await _walk(_server(install))

    with sqlite3.connect(install.parent / "state" / "cyris.sqlite3") as db:
        keys = {row[0] for row in db.execute("SELECT key FROM settings")}
    assert keys >= set(STARTING_VALUES)


async def test_every_tuned_setting_lands_at_its_starting_value(install: Path) -> None:
    await _walk(_server(install))

    stored = _stored(install)
    assert {key: stored[key] for key in STARTING_VALUES} == STARTING_VALUES
    assert {key: stored[key] for key in ANSWERS} == ANSWERS
    assert {key: stored[key] for key in stored if key.startswith("notify.")} == {
        "notify.discord_webhook_url": "",
        "notify.email_to": "",
        "notify.email_from": "",
    }


async def test_a_pushed_value_is_never_written_over(install: Path) -> None:
    """A trial's first boot pushes settings; the wizard opened after it keeps each one."""
    build_settings(_config(install)).set(PUSHED)

    sent = await _walk(_server(install))

    stored = _stored(install)
    assert {key: stored[key] for key in PUSHED} == PUSHED
    assert _config(install).missing_settings == []
    written = {key for _, body in sent for key in body.get("values", {})}
    assert written.isdisjoint(PUSHED)


async def test_a_complete_store_is_sent_no_setting(install: Path) -> None:
    await _walk(_server(install))
    before = _stored(install)

    sent = await _walk(_server(install))

    assert sent == []
    assert _stored(install) == before


async def test_the_server_offers_no_starting_value_for_a_stored_key(install: Path) -> None:
    build_settings(_config(install)).set(PUSHED)

    _, state = await _call(_server(install), "GET", "/api/settings")

    assert set(state["starting"]) == set(STARTING_VALUES) - set(PUSHED)


async def test_the_first_issue_turns_vote_similarity_on_after_the_walk(
    install: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the embedder the model step stored, so the save is not refused for a missing one."""
    probe = mock.AsyncMock(return_value=Check(name="embedder", status="ok", detail="ok"))
    monkeypatch.setattr("cyris.entrypoints.triage_server.probe_embedder", probe)
    server = _server(install)
    await _walk(server)
    _, state = await _call(server, "GET", "/api/settings")
    values, starting = state["values"], state["starting"]

    def stored(key: str):
        return values[key] if values[key] is not None else starting[key]

    status, answer = await _call(
        server,
        "POST",
        "/api/settings/vote-similarity",
        {
            "enabled": True,
            "provider": stored("vote_similarity.provider"),
            "model": stored("vote_similarity.model"),
            "max_seeds": stored("vote_similarity.max_seeds"),
        },
    )

    assert status == 200, answer
    probe.assert_awaited_once_with(STARTING_VALUES["vote_similarity.provider"], "")
    stored_now = _stored(install)
    assert stored_now["vote_similarity.enabled"] is True
    assert stored_now["vote_similarity.max_seeds"] == STARTING_VALUES["vote_similarity.max_seeds"]
