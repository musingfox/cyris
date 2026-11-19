"""scripts/e2e_onboarding.py: the store modes, the verdict and the driver, with no browser.

The walk itself needs Chromium and a running deployment, so it is run by hand
(docs/operations.md). Here D1 is the repo's `SqliteD1` behind the script's `D1Client`
name, so no Cloudflare call is made, and the driver runs under real Node against a
stand-in for the global `WebSocket`, which records each DevTools call it receives.
"""

import base64
import contextlib
import importlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import zoneinfo
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import feedparser
import httpx
import pytest
import respx
from fakes import SqliteD1, seeded_sqlite

from cyris.adapters.store.d1 import D1Error, apply_schema
from cyris.adapters.store.local_d1 import LocalD1Client

pytestmark = pytest.mark.integration

ROOT = Path(__file__).parent.parent
_SCRIPTS = str(ROOT / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
walker = importlib.import_module("e2e_onboarding")
local_run = importlib.import_module("e2e_onboarding_local")

URL = "https://cyris.example"
FEED = "https://feeds.example/rss"
DATABASE_ID = "e2e-database"
ACCOUNT_ID = "e2e-account"
API_TOKEN = "e2e-token"
ARGS = ["--url", URL, "--feed", FEED, "--d1", DATABASE_ID]
RUN = {"period": "morning", "date": "2026-11-09"}
ACCESS = {"CF-Access-Client-Id": "the-id", "CF-Access-Client-Secret": "the-secret"}
TIMEZONE = {"general.timezone": "UTC"}
HOURS = ["08:00", "20:00"]


def _add_source(d1: SqliteD1) -> None:
    d1.query("INSERT INTO sources (name, url) VALUES (?, ?)", ["A feed", FEED])


def _add_run(d1: SqliteD1, status: str, *, period: str = "morning", dry_run: int = 0) -> None:
    d1.query(
        "INSERT INTO digest_runs (finished_at, status, period, dry_run, summary) "
        "VALUES (?, ?, ?, ?, ?)",
        ["2026-11-09T00:00:00+00:00", status, period, dry_run, json.dumps({"error": "boom"})],
    )


@pytest.fixture
def d1(monkeypatch: pytest.MonkeyPatch) -> SqliteD1:
    fake = SqliteD1()
    opened = {}

    def open_client(**kwargs: str) -> SqliteD1:
        opened.update(kwargs)
        return fake

    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", ACCOUNT_ID)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", API_TOKEN)
    monkeypatch.setattr(walker, "D1Client", open_client)
    fake.opened = opened
    return fake


_site_awaits_sources = walker.site_awaits_sources


_site_settings = getattr(walker, "site_settings", None)
SITE_LANGUAGES = ["zh-Hant", "en", "ja"]


@pytest.fixture(autouse=True)
def site(monkeypatch: pytest.MonkeyPatch) -> dict:
    """The site at --url: it awaits its first source and holds no setting, unless a test says
    otherwise."""
    site = {"awaiting": True, "asked": [], "values": {}, "languages": SITE_LANGUAGES, "read": []}

    def awaits(url: str, headers: dict[str, str]) -> bool:
        site["asked"].append((url, headers))
        if isinstance(site["awaiting"], Exception):
            raise site["awaiting"]
        return site["awaiting"]

    def settings(url: str, headers: dict[str, str]) -> tuple[list[str], dict]:
        site["read"].append((url, headers))
        return site["languages"], site["values"]

    monkeypatch.setattr(walker, "site_awaits_sources", awaits)
    monkeypatch.setattr(walker, "site_settings", settings, raising=False)
    return site


@pytest.fixture
def browser(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """No browser: records each time the script asks for one."""
    asked: list[str] = []

    @contextlib.contextmanager
    def chromium(path: str):
        asked.append(path)
        yield "ws://stub"

    monkeypatch.setattr(walker, "find_browser", lambda explicit: "chromium-binary")
    monkeypatch.setattr(walker, "require_node", lambda: None)
    monkeypatch.setattr(walker, "chromium", chromium)
    return asked


def _walk_that(monkeypatch: pytest.MonkeyPatch, *events: dict, then=lambda: None) -> None:
    """A walk answering `events`, every step passing unless one of them says otherwise."""

    async def drive(socket_url: str, job: dict, budget_s: int, walk: walker.Walk, confirm) -> None:
        for event in events:
            walk.take(event)
        then()

    monkeypatch.setattr(walker, "drive_walk", drive)


def _every_step_passes() -> list[dict]:
    return [{"step": step, "ok": True} for step in walker.STEP_IDS]


class TestD1Mode:
    def test_it_opens_the_existing_client_with_the_environments_credentials(
        self, d1: SqliteD1
    ) -> None:
        store = walker.open_store(walker.parse_args(ARGS))

        assert store is d1
        assert d1.opened == {
            "account_id": ACCOUNT_ID,
            "database_id": DATABASE_ID,
            "api_token": API_TOKEN,
        }

    @pytest.mark.parametrize("variable", ["CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"])
    def test_a_missing_credential_refuses_and_is_named(
        self, d1: SqliteD1, browser: list[str], monkeypatch, capsys, variable: str
    ) -> None:
        monkeypatch.delenv(variable)

        assert walker.main(ARGS) == walker.EXIT_REFUSED

        assert variable in capsys.readouterr().err
        assert browser == []

    def test_a_store_with_a_source_refuses_before_any_browser(
        self, d1: SqliteD1, browser: list[str], capsys
    ) -> None:
        _add_source(d1)

        assert walker.main(ARGS) == walker.EXIT_REFUSED

        err = capsys.readouterr().err
        assert "A feed" in err
        assert "not be walking a new install" in err
        assert browser == []

    def test_an_unreadable_store_refuses_rather_than_reading_as_empty(
        self, browser: list[str], monkeypatch, capsys
    ) -> None:
        monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", ACCOUNT_ID)
        monkeypatch.setenv("CLOUDFLARE_API_TOKEN", API_TOKEN)

        class Unreachable:
            def query(self, sql: str, params: list | None = None) -> None:
                raise D1Error("D1 request failed after 4 attempts")

        monkeypatch.setattr(walker, "D1Client", lambda **_: Unreachable())

        assert walker.main(ARGS) == walker.EXIT_REFUSED

        assert "REFUSED: D1 request failed" in capsys.readouterr().err
        assert browser == []

    def test_a_source_and_an_ok_row_exit_zero_and_print_the_row(
        self, d1: SqliteD1, browser: list[str], monkeypatch, capsys
    ) -> None:
        def run_ends() -> None:
            _add_source(d1)
            _add_run(d1, "ok")

        _walk_that(monkeypatch, {"run": RUN}, *_every_step_passes(), then=run_ends)

        assert walker.main(ARGS) == walker.EXIT_OK

        out = capsys.readouterr().out
        assert '"status": "ok"' in out
        assert browser == ["chromium-binary"]

    def test_a_run_ending_other_than_ok_fails_and_names_the_receipt(
        self, d1: SqliteD1, browser: list[str], monkeypatch, capsys
    ) -> None:
        def run_ends() -> None:
            _add_source(d1)
            _add_run(d1, "no_articles")

        _walk_that(monkeypatch, {"run": RUN}, *_every_step_passes(), then=run_ends)

        assert walker.main(ARGS) == walker.EXIT_FAILED

        out = capsys.readouterr().out
        assert "FAIL receipt digest_runs" in out
        assert "status=no_articles" in out

    def test_a_passing_walk_with_no_source_fails_on_that_receipt(
        self, d1: SqliteD1, browser: list[str], monkeypatch, capsys
    ) -> None:
        _walk_that(
            monkeypatch,
            {"run": RUN},
            *_every_step_passes(),
            then=lambda: _add_run(d1, "ok"),
        )

        assert walker.main(ARGS) == walker.EXIT_FAILED

        assert "FAIL receipt sources: no row" in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("earlier", "later"),
        [
            pytest.param({"status": "ok"}, None, id="a row from before the walk"),
            pytest.param(None, {"status": "ok", "dry_run": 1}, id="a preview's row"),
            pytest.param(None, {"status": "ok", "period": "evening"}, id="another period's row"),
        ],
    )
    def test_a_row_that_is_not_the_first_issues_does_not_count(
        self,
        d1: SqliteD1,
        browser: list[str],
        monkeypatch,
        capsys,
        earlier: dict | None,
        later: dict | None,
    ) -> None:
        if earlier:
            _add_run(d1, **earlier)

        def run_ends() -> None:
            _add_source(d1)
            if later:
                _add_run(d1, **later)

        _walk_that(monkeypatch, {"run": RUN}, *_every_step_passes(), then=run_ends)

        assert walker.main(ARGS) == walker.EXIT_FAILED

        assert "FAIL receipt digest_runs: no morning row" in capsys.readouterr().out

    def test_a_failed_step_fails_naming_it_and_prints_the_console_errors(
        self, d1: SqliteD1, browser: list[str], monkeypatch, capsys
    ) -> None:
        _walk_that(
            monkeypatch,
            {"console": "Failed to load resource: 500"},
            {"step": "open", "ok": True},
            {"step": "sources", "ok": False, "detail": "feed check refused"},
        )

        assert walker.main(ARGS) == walker.EXIT_FAILED

        out = capsys.readouterr().out
        assert "FAIL step sources: feed check refused" in out
        assert "Failed to load resource: 500" in out
        assert "FAIL receipt digest_runs: the page never posted /run" in out


SITE_FEEDS = f"{URL}/api/onboarding/feeds"
SAVED_TIMEZONE = {"path": "/api/settings/values", "answer": {"ok": True, "values": TIMEZONE}}


class TestOneInstall:
    """--url and the store flag are held to one install: the walk writes to one, reads the other."""

    def test_a_site_with_sources_refuses_before_any_browser(
        self, d1: SqliteD1, site: dict, browser: list[str], capsys
    ) -> None:
        site["awaiting"] = False

        assert walker.main(ARGS) == walker.EXIT_REFUSED

        err = capsys.readouterr().err
        assert f"the site, --url {URL}, says its sources are not empty" in err
        assert f"while --d1 {DATABASE_ID} holds none" in err
        assert browser == []

    def test_a_store_with_sources_names_the_store(
        self, d1: SqliteD1, site: dict, browser: list[str], capsys
    ) -> None:
        _add_source(d1)

        assert walker.main(ARGS) == walker.EXIT_REFUSED

        assert f"the store, --d1 {DATABASE_ID}, holds 1 sources row(s)" in capsys.readouterr().err
        assert browser == []

    def test_a_site_that_cannot_be_read_refuses(
        self, d1: SqliteD1, site: dict, browser: list[str], capsys
    ) -> None:
        site["awaiting"] = httpx.ConnectError("refused")

        assert walker.main(ARGS) == walker.EXIT_REFUSED

        assert "could not read /api/onboarding/feeds at --url: refused" in capsys.readouterr().err
        assert browser == []

    def test_the_site_is_asked_at_its_url_with_the_token(
        self, d1: SqliteD1, site: dict, browser: list[str], monkeypatch
    ) -> None:
        monkeypatch.setenv("CYRIS_ACCESS_CLIENT_ID", "the-id")
        monkeypatch.setenv("CYRIS_ACCESS_CLIENT_SECRET", "the-secret")
        _walk_that(monkeypatch)

        walker.main(ARGS)

        assert site["asked"] == [(URL, ACCESS)]

    @respx.mock
    @pytest.mark.parametrize(
        ("answer", "awaiting"),
        [
            pytest.param({"awaiting": True, "topics": [], "feeds": []}, True, id="awaiting"),
            pytest.param({"awaiting": False, "topics": [], "feeds": []}, False, id="has sources"),
        ],
    )
    def test_the_site_answers_through_the_wizards_own_route(
        self, answer: dict, awaiting: bool
    ) -> None:
        route = respx.get(SITE_FEEDS).mock(return_value=httpx.Response(200, json=answer))

        assert _site_awaits_sources(f"{URL}/", ACCESS) is awaiting

        assert route.call_count == 1
        sent = route.calls.last.request.headers
        assert {name: sent.get(name) for name in ACCESS} == ACCESS

    @respx.mock
    @pytest.mark.parametrize(
        "answer",
        [
            pytest.param({"ok": False, "error": "x"}, id="says nothing of it"),
            pytest.param({"awaiting": "yes"}, id="not a yes or no"),
            pytest.param([], id="not an object"),
        ],
    )
    def test_an_answer_that_says_neither_refuses_as_unreadable(
        self, d1: SqliteD1, browser: list[str], monkeypatch, capsys, answer: object
    ) -> None:
        monkeypatch.setattr(walker, "site_awaits_sources", _site_awaits_sources)
        route = respx.get(SITE_FEEDS).mock(return_value=httpx.Response(200, json=answer))

        assert walker.main(ARGS) == walker.EXIT_REFUSED

        err = capsys.readouterr().err
        assert "REFUSED: could not read /api/onboarding/feeds at --url: it answered " in err
        assert "says neither that it awaits its first source nor that it does not" in err
        assert "says its sources are not empty" not in err
        assert browser == []
        assert route.call_count == 1

    @respx.mock
    @pytest.mark.parametrize(
        "answer",
        [
            pytest.param(
                httpx.Response(302, headers={"Location": "https://team.example/login"}),
                id="a redirect",
            ),
            pytest.param(httpx.Response(500, json={"ok": False}), id="an error"),
        ],
    )
    def test_any_other_answer_is_not_read_as_empty(self, answer: httpx.Response) -> None:
        route = respx.get(SITE_FEEDS).mock(return_value=answer)
        elsewhere = respx.get("https://team.example/login").mock(return_value=httpx.Response(200))

        with pytest.raises(httpx.HTTPStatusError):
            _site_awaits_sources(URL, ACCESS)

        assert (route.call_count, elsewhere.call_count) == (1, 0)

    def _confirm(
        self, monkeypatch: pytest.MonkeyPatch, saved: dict = SAVED_TIMEZONE, wrote=lambda: None
    ) -> list[str]:
        """Runs main with a walk whose language step saves, the site having done `wrote` to the
        store; returns what the check answered."""
        answers: list[str] = []

        async def drive(socket_url: str, job: dict, budget_s: int, walk: walker.Walk, confirm):
            wrote()
            answers.append(confirm(saved))

        monkeypatch.setattr(walker, "drive_walk", drive)
        walker.main(ARGS)
        return answers

    def test_a_save_the_store_does_not_hold_names_the_mismatch(
        self, d1: SqliteD1, browser: list[str], monkeypatch
    ) -> None:
        assert self._confirm(monkeypatch) == [
            f"--url and --d1 {DATABASE_ID} are not one install: the walk saved "
            'digest.output_language = "en" at --url, and --d1 e2e-database holds nothing'
        ]

    def test_a_save_the_store_holds_otherwise_names_both_values(
        self, d1: SqliteD1, browser: list[str], monkeypatch
    ) -> None:
        wrote = {"digest.output_language": "en", "general.timezone": "Asia/Taipei"}

        [answer] = self._confirm(monkeypatch, wrote=lambda: _store_holds(d1, wrote))

        assert answer.endswith(
            'saved general.timezone = "UTC" at --url, and --d1 e2e-database holds "Asia/Taipei"'
        )

    def test_the_walks_own_answers_in_the_store_let_the_walk_on(
        self, d1: SqliteD1, browser: list[str], monkeypatch
    ) -> None:
        wrote = {"digest.output_language": "en", "general.timezone": "UTC"}

        assert self._confirm(monkeypatch, wrote=lambda: _store_holds(d1, wrote)) == [""]

    def test_a_store_that_held_what_the_site_answered_before_the_walk_is_not_its_store(
        self, d1: SqliteD1, browser: list[str], monkeypatch
    ) -> None:
        _store_holds(d1, {**TIMEZONE, "digest.output_language": "en"})

        [answer] = self._confirm(monkeypatch, SAVED_TIMEZONE)

        assert answer.startswith(f"--url and --d1 {DATABASE_ID} are not one install: ")

    def test_a_row_that_is_not_json_is_skipped_as_the_production_reader_skips_it(
        self, d1: SqliteD1, browser: list[str], monkeypatch, capsys
    ) -> None:
        _store_raw(d1, "digest.style_prompt", "not json")
        wrote = {"digest.output_language": "en", "general.timezone": "UTC"}

        assert self._confirm(monkeypatch, wrote=lambda: _store_holds(d1, wrote)) == [""]

        assert "REFUSED" not in capsys.readouterr().err

    def test_a_saved_setting_that_is_not_json_names_the_row(
        self, d1: SqliteD1, browser: list[str], monkeypatch
    ) -> None:
        def wrote() -> None:
            _store_holds(d1, {"digest.output_language": "en"})
            _store_raw(d1, "general.timezone", "UTC")

        assert self._confirm(monkeypatch, wrote=wrote) == [
            f"--url and --d1 {DATABASE_ID} are not one install: the walk saved "
            'general.timezone = "UTC" at --url, and --d1 e2e-database holds a value that is not '
            "JSON, 'UTC'"
        ]


# The language and schedule steps' own programs, on a page whose fields opened on what the
# site holds. Next records each field as it stands when pressed, which the page compares with
# what it loaded to decide what to send, and opens the step after.
STEP_PAGE = """
const page = JSON.parse(process.env.PAGE);
const shown = { offsetParent: {} };
class Field {
  constructor(value, options) { this.offsetParent = {}; this.options = options; this.held = value; }
  get value() { return this.held; }
  set value(value) {
    const text = String(value);
    this.held = !this.options || this.options.includes(text) ? text : '';
  }
  dispatchEvent() { return true; }
}
const fields = {
  '#o-output-language': new Field(page.language, page.languages),
  '#o-timezone': new Field(page.timezone, page.timezones),
  '#o-morning': new Field(page.morning),
  '#o-evening': new Field(page.evening),
};
let pressed = null;
const next = { ...shown, click() {
  pressed = Object.fromEntries(Object.entries(fields).map(([id, field]) => [id, field.value]));
  location.hash = `#${page.then}`;
} };
globalThis.window = {};
globalThis.location = { hash: `#${page.step}`, pathname: '/onboarding' };
globalThis.document = {
  querySelector(selector) {
    if (selector in fields) return fields[selector];
    if (selector === '#o-next') return next;
    return selector === `.step[data-step="${page.then}"]` ? shown : null;
  },
  querySelectorAll: () => [],
};
let program = '';
for await (const chunk of process.stdin) program += chunk;
const result = JSON.parse(await (0, eval)(program));
process.stdout.write(JSON.stringify({ result, pressed }));
"""
ZONES = sorted(zoneinfo.available_timezones())
RESET = importlib.import_module("reset_trial").shipped_defaults()


def _pressed(job: dict, step: str, then: str, site_values: dict) -> dict:
    """What `step`'s fields hold when its Next is pressed, on a page opened on `site_values`."""
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    [program] = [s["expression"] for s in job["steps"] if s["id"] == step]
    hours = site_values.get("general.digest_schedule") or ["", ""]
    morning, evening = (str(int(time[:2])) if time else "" for time in hours)
    page = {
        "step": step,
        "then": then,
        "language": site_values.get("digest.output_language") or "",
        "languages": SITE_LANGUAGES,
        "timezone": site_values.get("general.timezone") or "",
        "timezones": ZONES,
        "morning": morning,
        "evening": evening,
    }
    answered = subprocess.run(
        ["node", "--input-type=module", "-e", STEP_PAGE],
        input=program,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env={**os.environ, "PAGE": json.dumps(page)},
    )
    out = json.loads(answered.stdout)
    assert out["result"] == {"ok": True}
    return out["pressed"]


def _store_raw(d1: SqliteD1, key: str, value: str) -> None:
    d1.query(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
        [key, value, "2026-11-09T00:00:00+00:00"],
    )


def _store_holds(d1: SqliteD1, values: dict) -> None:
    for key, value in values.items():
        _store_raw(d1, key, json.dumps(value))


class TestTheWalksOwnAnswers:
    """The language step's save is always sent, and holds what neither side held before."""

    def _job(self, monkeypatch: pytest.MonkeyPatch) -> dict:
        jobs: list[dict] = []

        async def drive(socket_url: str, job: dict, budget_s: int, walk: walker.Walk, confirm):
            jobs.append(job)

        monkeypatch.setattr(walker, "drive_walk", drive)
        walker.main(ARGS)
        [job] = jobs
        return job

    @pytest.mark.parametrize(
        "side", [pytest.param("site", id="--url holds them"), pytest.param("store", id="the store")]
    )
    def test_a_reset_trials_values_are_not_what_the_steps_save(
        self, d1: SqliteD1, site: dict, browser: list[str], monkeypatch, side: str
    ) -> None:
        if side == "site":
            site["values"] = RESET
        else:
            _store_holds(d1, RESET)

        job = self._job(monkeypatch)

        language = _pressed(job, "language", "schedule", site["values"])
        assert language["#o-output-language"] not in ("", RESET["digest.output_language"])
        assert language["#o-timezone"] not in ("", RESET["general.timezone"])
        if side == "site":
            assert language["#o-output-language"] != site["values"]["digest.output_language"]
        schedule = _pressed(job, "schedule", "sources", site["values"])
        hours = [f"{int(schedule[f'#o-{name}']):02d}:00" for name in ("morning", "evening")]
        assert hours != RESET["general.digest_schedule"]

    def test_the_page_is_answered_with_the_next_choice_when_both_sides_hold_the_first(
        self, d1: SqliteD1, site: dict, browser: list[str], monkeypatch
    ) -> None:
        first = {
            "digest.output_language": walker.READER_LANGUAGE,
            "general.timezone": walker.READER_TIMEZONE,
            "general.digest_schedule": walker.schedule_of(walker.READER_HOURS),
        }
        site["values"] = first
        _store_holds(d1, first)
        choice = walker.choose(SITE_LANGUAGES, first, first, "--d1 x")

        job = self._job(monkeypatch)

        assert choice.language != walker.READER_LANGUAGE
        assert choice.timezone != walker.READER_TIMEZONE
        language = _pressed(job, "language", "schedule", site["values"])
        assert language["#o-output-language"] == choice.language
        assert language["#o-timezone"] == choice.timezone
        schedule = _pressed(job, "schedule", "sources", site["values"])
        assert (int(schedule["#o-morning"]), int(schedule["#o-evening"])) == choice.hours

    def test_what_neither_side_holds_is_the_first_choice(self) -> None:
        assert walker.choose(SITE_LANGUAGES, {}, {}, "--d1 x") == walker.Choice(
            "en", "UTC", (8, 20)
        )

    def test_what_either_side_holds_is_passed_over_for_the_next_offered(self) -> None:
        site = {
            "digest.output_language": "en",
            "general.timezone": "UTC",
            "general.digest_schedule": HOURS,
        }
        store = {
            "digest.output_language": "zh-Hant",
            "general.timezone": ZONES[0],
            "general.digest_schedule": ["09:00", "21:00"],
        }

        choice = walker.choose(SITE_LANGUAGES, site, store, "--d1 x")

        assert choice == walker.Choice("ja", ZONES[1], (10, 22))

    def test_an_answer_the_page_does_not_offer_is_never_chosen(self) -> None:
        assert walker.choose(["ja"], {}, {}, "--d1 x").language == "ja"

    def test_no_language_left_refuses_before_any_browser(
        self, d1: SqliteD1, site: dict, browser: list[str], capsys
    ) -> None:
        site["languages"] = ["en", "zh-Hant"]
        site["values"] = {"digest.output_language": "en"}
        _store_holds(d1, {"digest.output_language": "zh-Hant"})

        assert walker.main(ARGS) == walker.EXIT_REFUSED

        assert capsys.readouterr().err == (
            'REFUSED: the walk needs an output language that neither --url ("en") nor '
            f'--d1 {DATABASE_ID} ("zh-Hant") holds, so that the store can show the language '
            "step's save as this run's, and the page offers no other (2 offered)\n"
        )
        assert browser == []

    def test_the_site_is_read_at_its_url_with_the_token(
        self, d1: SqliteD1, site: dict, browser: list[str], monkeypatch
    ) -> None:
        monkeypatch.setenv("CYRIS_ACCESS_CLIENT_ID", "the-id")
        monkeypatch.setenv("CYRIS_ACCESS_CLIENT_SECRET", "the-secret")
        _walk_that(monkeypatch)

        walker.main(ARGS)

        assert site["read"] == [(URL, ACCESS)]

    @respx.mock
    def test_the_site_settings_are_the_pages_own_route(self) -> None:
        answer = {"values": RESET, "languages": [{"tag": "zh-Hant", "name": "x"}], "fields": {}}
        route = respx.get(f"{URL}/api/settings").mock(return_value=httpx.Response(200, json=answer))

        assert _site_settings(f"{URL}/", ACCESS) == (["zh-Hant"], RESET)

        assert route.call_count == 1
        sent = route.calls.last.request.headers
        assert {name: sent.get(name) for name in ACCESS} == ACCESS

    @respx.mock
    @pytest.mark.parametrize(
        "answer",
        [
            pytest.param(httpx.Response(200, json={"ok": False}), id="no settings"),
            pytest.param(httpx.Response(200, json={"values": {}, "languages": [{}]}), id="no tag"),
            pytest.param(
                httpx.Response(302, headers={"Location": "https://team.example/login"}),
                id="a redirect",
            ),
        ],
    )
    def test_an_answer_that_names_no_settings_refuses(
        self, d1: SqliteD1, browser: list[str], monkeypatch, capsys, answer: httpx.Response
    ) -> None:
        monkeypatch.setattr(walker, "site_settings", _site_settings)
        route = respx.get(f"{URL}/api/settings").mock(return_value=answer)
        elsewhere = respx.get("https://team.example/login").mock(return_value=httpx.Response(200))

        assert walker.main(ARGS) == walker.EXIT_REFUSED

        assert "REFUSED: could not read /api/settings at --url: " in capsys.readouterr().err
        assert browser == []
        assert (route.call_count, elsewhere.call_count) == (1, 0)


AMBIGUOUS_URLS = [
    pytest.param("http://evil.example\\@127.0.0.1/", id="the host differs by reader"),
    pytest.param("http://127.0.0.1\\.evil.example/", id="a backslash in the host"),
    pytest.param("http://127.0.0.1:1/a\\b", id="a backslash in the path"),
    pytest.param("http://user@127.0.0.1:1/", id="a user"),
    pytest.param("http://user:secret@127.0.0.1:1/", id="a user and password"),
    pytest.param("https://evil.example@cyris.example/", id="userinfo on a public host"),
]


class TestAmbiguousUrls:
    @pytest.mark.parametrize("url", AMBIGUOUS_URLS)
    def test_url_refuses_it(self, url: str, capsys) -> None:
        with pytest.raises(SystemExit) as exit_info:
            walker.parse_args(["--url", url, "--feed", FEED, "--d1", DATABASE_ID])

        assert exit_info.value.code == walker.EXIT_REFUSED
        assert "backslash or userinfo" in capsys.readouterr().err

    @pytest.mark.parametrize("url", AMBIGUOUS_URLS)
    def test_feed_refuses_it(self, url: str, capsys) -> None:
        with pytest.raises(SystemExit) as exit_info:
            walker.parse_args(["--url", URL, "--feed", FEED, url, "--d1", DATABASE_ID])

        assert exit_info.value.code == walker.EXIT_REFUSED
        assert "backslash or userinfo" in capsys.readouterr().err

    @pytest.mark.parametrize("url", AMBIGUOUS_URLS)
    def test_it_does_not_pass_as_loopback(self, url: str, capsys) -> None:
        with pytest.raises(SystemExit) as exit_info:
            walker.parse_args(
                ["--url", url, "--feed", url, "--d1", DATABASE_ID, "--skip-feed-checker"]
            )

        assert exit_info.value.code == walker.EXIT_REFUSED
        assert "backslash or userinfo" in capsys.readouterr().err

    def test_an_at_sign_past_the_host_is_a_plain_address(self) -> None:
        feed = "https://feeds.example/@someone/rss?by=me@example.org"

        assert walker.parse_args(["--url", URL, "--feed", feed, "--d1", DATABASE_ID]).feed == [feed]


class TestAFailedWalk:
    def test_an_ok_row_does_not_rescue_a_step_that_failed(
        self, d1: SqliteD1, browser: list[str], monkeypatch, capsys
    ) -> None:
        def run_ends() -> None:
            _add_source(d1)
            _add_run(d1, "ok")

        _walk_that(
            monkeypatch,
            {"run": RUN},
            {"step": "first-issue-ends", "ok": False, "detail": "the page said it failed"},
            then=run_ends,
        )

        assert walker.main(ARGS) == walker.EXIT_FAILED

        out = capsys.readouterr().out
        assert "FAIL step first-issue-ends: the page said it failed" in out
        assert '"status": "ok"' in out
        assert "OK:" not in out


class TestSqliteMode:
    @pytest.fixture
    def named(self, tmp_path: Path) -> Path:
        path = tmp_path / "named.sqlite3"
        seeded_sqlite(path)
        return path

    @pytest.fixture
    def elsewhere(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """A second store, on the default path of the directory the script runs in."""
        monkeypatch.chdir(tmp_path)
        path = tmp_path / "state" / "cyris.sqlite3"
        client = seeded_sqlite(path)
        client.query("INSERT INTO sources (name, url) VALUES (?, ?)", ["Elsewhere", FEED])
        _add_run(client, "error")
        return path

    def _args(self, path: Path) -> list[str]:
        return ["--url", URL, "--feed", FEED, "--sqlite", str(path)]

    def test_the_verdict_is_read_from_the_file_it_names(
        self, named: Path, elsewhere: Path, browser: list[str], monkeypatch, capsys
    ) -> None:
        def run_ends() -> None:
            client = LocalD1Client(named)
            _add_source(client)
            _add_run(client, "ok")

        _walk_that(monkeypatch, {"run": RUN}, *_every_step_passes(), then=run_ends)

        assert walker.main(self._args(named)) == walker.EXIT_OK

        out = capsys.readouterr().out
        assert '["A feed"]' in out
        assert "Elsewhere" not in out

    def test_the_refusal_is_read_from_the_file_it_names(
        self, elsewhere: Path, browser: list[str], capsys
    ) -> None:
        assert walker.main(self._args(elsewhere)) == walker.EXIT_REFUSED

        assert "Elsewhere" in capsys.readouterr().err
        assert browser == []

    def test_a_file_that_is_not_there_refuses_and_is_not_created(
        self, tmp_path: Path, browser: list[str], capsys
    ) -> None:
        missing = tmp_path / "state" / "typo.sqlite3"

        assert walker.main(self._args(missing)) == walker.EXIT_REFUSED

        assert "is not there" in capsys.readouterr().err
        assert not missing.exists()
        assert not missing.parent.exists()
        assert browser == []


class TestAccessHeaders:
    def test_unset_sends_none(self) -> None:
        assert walker.access_headers({}) == {}

    def test_both_set_send_both(self) -> None:
        environ = {"CYRIS_ACCESS_CLIENT_ID": "id", "CYRIS_ACCESS_CLIENT_SECRET": "secret"}

        assert walker.access_headers(environ) == {
            "CF-Access-Client-Id": "id",
            "CF-Access-Client-Secret": "secret",
        }

    def test_one_alone_is_refused(self) -> None:
        with pytest.raises(ValueError, match="both or neither"):
            walker.access_headers({"CYRIS_ACCESS_CLIENT_ID": "id"})

    def test_the_token_in_the_environment_reaches_the_driver(
        self, d1: SqliteD1, browser: list[str], monkeypatch
    ) -> None:
        monkeypatch.setenv("CYRIS_ACCESS_CLIENT_ID", "the-id")
        monkeypatch.setenv("CYRIS_ACCESS_CLIENT_SECRET", "the-secret")
        jobs: list[dict] = []

        async def drive(
            socket_url: str, job: dict, budget_s: int, walk: walker.Walk, confirm
        ) -> None:
            jobs.append(job)

        monkeypatch.setattr(walker, "drive_walk", drive)

        walker.main(ARGS)

        assert jobs[0]["headers"] == {
            "CF-Access-Client-Id": "the-id",
            "CF-Access-Client-Secret": "the-secret",
        }


# Answers as Chromium does and records each call: a syntax error in a page-side step
# fails here, since each expression is compiled as the page would compile it.
WEBSOCKET_STAND_IN = """
import { appendFileSync } from 'node:fs';
import vm from 'node:vm';
const plan = JSON.parse(process.env.STUB_PLAN);
globalThis.WebSocket = class {
  listeners = {};
  constructor() { setTimeout(() => this.emit('open', {}), 0); }
  addEventListener(name, listener) { (this.listeners[name] ??= []).push(listener); }
  emit(name, event) { (this.listeners[name] ?? []).forEach((listener) => listener(event)); }
  // Whether each request last paused at its response: letting that through is what a page
  // waiting on the request hears.
  atResponse = {};
  push(message) {
    setTimeout(() => {
      if (message.method === 'Fetch.requestPaused') {
        const { requestId, responseStatusCode, responseErrorReason } = message.params;
        this.atResponse[requestId] = responseStatusCode !== undefined || !!responseErrorReason;
      }
      this.emit('message', { data: JSON.stringify(message) });
    }, 0);
  }
  close() {}
  answer(method, params) {
    if (method === 'Page.navigate') return { errorText: plan.navigateError };
    if (method === 'Fetch.getResponseBody') return { body: plan.savedBody, base64Encoded: false };
    if (method !== 'Runtime.evaluate') return {};
    new vm.Script(params.expression);
    if (plan.exitOn && params.expression.includes(plan.exitOn)) {
      process.stderr.write(plan.exitSays ?? '');
      process.exit(plan.exitCode);
    }
    if (!params.expression.startsWith('(async')) return { result: { value: true } };
    const failing = plan.failOn && params.expression.includes(plan.failOn);
    const value = JSON.stringify(failing ? { ok: false, detail: 'it failed' } : { ok: true });
    return { result: { value } };
  }
  reply(id, method, params) {
    // As Chromium answers an evaluation whose document was replaced under it, by a reload.
    if (plan.navigatesOn && params?.expression?.includes(plan.navigatesOn)) {
      return { id, error: { message: 'Inspected target navigated or closed' } };
    }
    return { id, result: this.answer(method, params) };
  }
  send(text) {
    const { id, method, params } = JSON.parse(text);
    // One line as each call arrives, so a driver that is killed leaves its calls so far.
    appendFileSync(process.env.STUB_CALLS, JSON.stringify({ method, params }) + '\\n');
    // As Chromium answers it for a response that has no body, such as a redirect.
    if (method === 'Fetch.getResponseBody' && plan.bodyError) {
      this.push({ id, error: { message: plan.bodyError } });
      return;
    }
    // A real socket keeps the driver alive while a call is out, and this one holds no handle.
    if (plan.hangOn && params?.expression?.includes(plan.hangOn)) {
      setInterval(() => {}, 1000);
      return;
    }
    // The step that saves answers only once the save's response is let through, as a page
    // waits on it in Chromium.
    if (plan.saveOn && params?.expression?.includes(plan.saveOn)) {
      const answer = this.reply(id, method, params);
      this.release = () => this.push(answer);
      [].concat(plan.saved).forEach((event) => this.push(event));
      return;
    }
    // A later step's own requests, which pause as its program runs.
    for (const cue of plan.cues ?? []) {
      if (params?.expression?.includes(cue.at)) cue.events.forEach((event) => this.push(event));
    }
    this.push(this.reply(id, method, params));
    if (method === 'Log.enable') (plan.events ?? []).forEach((event) => this.push(event));
    const lets = method === 'Fetch.fulfillRequest' || method === 'Fetch.continueRequest';
    if (lets && this.release && this.atResponse[params.requestId]) {
      this.release();
      this.release = null;
    }
  }
};
"""

STARTS_THE_RUN = {
    "method": "Network.requestWillBeSent",
    "params": {"request": {"method": "POST", "url": f"{URL}/run?period=morning&date=2026-11-09"}},
}
PAUSED_FEED_CHECK = {
    "method": "Fetch.requestPaused",
    "params": {"requestId": "paused-1", "request": {"url": f"{URL}/api/onboarding/feed-check"}},
}
PAGE_HEADERS = {"Accept": "text/html", "User-Agent": "HeadlessChrome"}
LOGIN = "https://team.example/login"
# The language step's save is held at the response, on the deployment's own origin only.
HELD = [{"urlPattern": f"{URL}/api/settings/values", "requestStage": "Response"}]
EVERY_REQUEST = {"urlPattern": "*"}
# Where a redirect off the deployment can lead, and hosts that only look like the deployment's.
ELSEWHERE = [
    "https://team.cloudflareaccess.com/cdn-cgi/access/login/cyris.example",
    "https://accounts.google.com/o/oauth2/auth",
    "https://cyris.example.evil.example/onboarding",
    "http://cyris.example/onboarding",
    "https://cyris.example:8443/onboarding",
    "https://fonts.googleapis.com/css2",
]


def _paused(request_id: str, url: str, method: str = "GET") -> dict:
    request = {"url": url, "method": method, "headers": PAGE_HEADERS}
    return {
        "method": "Fetch.requestPaused",
        "params": {"requestId": request_id, "request": request},
    }


CONSOLE_EVENTS = [
    {
        "method": "Log.entryAdded",
        "params": {"entry": {"level": "error", "text": "Failed to load", "url": f"{URL}/x"}},
    },
    {
        "method": "Log.entryAdded",
        "params": {"entry": {"level": "error", "text": "net::ERR_BLOCKED_BY_CLIENT"}},
    },
    {"method": "Runtime.consoleAPICalled", "params": {"type": "error", "args": [{"value": "bad"}]}},
    {"method": "Runtime.consoleAPICalled", "params": {"type": "log", "args": [{"value": "fine"}]}},
    {
        "method": "Runtime.exceptionThrown",
        "params": {"exceptionDetails": {"text": "Uncaught", "exception": {"description": "Boom"}}},
    },
]


class TestDriver:
    @pytest.fixture
    def drive(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Runs the walk's real driver, answering `plan`; returns the Walk and its calls."""
        if shutil.which("node") is None:
            pytest.skip("node not installed")
        stand_in = tmp_path / "stand_in.mjs"
        stand_in.write_text(WEBSOCKET_STAND_IN, encoding="utf-8")
        calls = tmp_path / "calls.jsonl"
        monkeypatch.setenv("NODE_OPTIONS", f"--import={stand_in.as_uri()}")
        monkeypatch.setenv("STUB_CALLS", str(calls))

        async def run(
            plan: dict | None = None,
            headers: dict[str, str] | None = None,
            broken: str | None = None,
            feed_check_answers: list[dict] | None = None,
            budget_s: int = 60,
            confirm=lambda saved: "",
        ) -> tuple[walker.Walk, list[dict]]:
            monkeypatch.setenv("STUB_PLAN", json.dumps(plan or {}))
            walk = walker.Walk()
            job = walker.build_job(f"{URL}/", [FEED], 60, headers or {}, broken, feed_check_answers)
            await walker.drive_walk("ws://stub", job, budget_s, walk, confirm)
            text = calls.read_text(encoding="utf-8") if calls.exists() else ""
            recorded = [json.loads(line) for line in text.splitlines()]
            return walk, recorded

        return run

    async def test_every_step_runs_in_order_on_the_onboarding_page(self, drive) -> None:
        walk, calls = await drive()

        assert walk.finished
        assert walk.failure == ""
        assert tuple(walk.passed) == walker.STEP_IDS
        [navigation] = [call for call in calls if call["method"] == "Page.navigate"]
        assert navigation["params"]["url"] == f"{URL}/onboarding"

    async def test_the_access_headers_go_only_to_the_deployments_own_origin(self, drive) -> None:
        paused = [
            _paused(f"paused-{n}", url)
            for n, url in enumerate([f"{URL}/onboarding", *ELSEWHERE, f"{URL}/api/settings"])
        ]

        walk, calls = await drive(plan={"events": paused}, headers=ACCESS)

        assert walk.failure == ""
        methods = [call["method"] for call in calls]
        assert "Network.setExtraHTTPHeaders" not in methods
        [enabled] = [call for call in calls if call["method"] == "Fetch.enable"]
        assert enabled["params"]["patterns"] == [*HELD, EVERY_REQUEST]
        assert methods.index("Fetch.enable") < methods.index("Page.navigate")
        continued = {
            c["params"]["requestId"]: {h["name"]: h["value"] for h in c["params"]["headers"]}
            for c in calls
            if c["method"] == "Fetch.continueRequest"
        }
        assert continued == {
            "paused-0": {**PAGE_HEADERS, **ACCESS},
            **{f"paused-{n}": PAGE_HEADERS for n in range(1, len(ELSEWHERE) + 1)},
            f"paused-{len(ELSEWHERE) + 1}": {**PAGE_HEADERS, **ACCESS},
        }

    async def test_a_feed_check_answered_in_the_browser_still_lets_the_rest_through(
        self, drive
    ) -> None:
        answers = [{"ok": True, "name": "First", "url": FEED}]
        paused = [{**PAUSED_FEED_CHECK}, _paused("paused-2", f"{URL}/onboarding")]

        walk, calls = await drive(
            plan={"events": paused}, headers=ACCESS, feed_check_answers=answers
        )

        assert walk.failure == ""
        [enabled] = [call for call in calls if call["method"] == "Fetch.enable"]
        assert enabled["params"]["patterns"] == [
            *HELD,
            {"urlPattern": "*/api/onboarding/feed-check"},
            EVERY_REQUEST,
        ]
        assert [
            c["params"]["requestId"] for c in calls if c["method"] == "Fetch.fulfillRequest"
        ] == ["paused-1"]
        assert [
            c["params"]["requestId"] for c in calls if c["method"] == "Fetch.continueRequest"
        ] == ["paused-2"]

    async def test_the_font_hosts_the_page_links_are_blocked(self, drive) -> None:
        template = (ROOT / "src/cyris/entrypoints/templates/onboarding.html.j2").read_text(
            encoding="utf-8"
        )
        linked = set(re.findall(r'href="https://([^/"]+)', template))

        _, calls = await drive()

        [blocked] = [call for call in calls if call["method"] == "Network.setBlockedURLs"]
        assert {"fonts.googleapis.com", "fonts.gstatic.com"} <= linked
        assert blocked["params"]["urls"] == [f"*{host}*" for host in sorted(linked)]

    async def test_no_token_pauses_every_request_and_sets_no_headers(self, drive) -> None:
        paused = [_paused("own", f"{URL}/onboarding"), _paused("other", ELSEWHERE[0])]

        walk, calls = await drive(plan={"events": paused})

        assert walk.finished
        [enabled] = [call for call in calls if call["method"] == "Fetch.enable"]
        assert enabled["params"]["patterns"] == [*HELD, EVERY_REQUEST]
        assert "Network.setExtraHTTPHeaders" not in [call["method"] for call in calls]
        continued = [c["params"] for c in calls if c["method"] == "Fetch.continueRequest"]
        assert continued == [{"requestId": "own"}, {"requestId": "other"}]

    async def test_break_feed_check_answers_that_request_with_an_error(self, drive) -> None:
        _, calls = await drive(plan={"events": [PAUSED_FEED_CHECK]}, broken="feed-check")

        [enabled] = [call for call in calls if call["method"] == "Fetch.enable"]
        assert enabled["params"]["patterns"] == [
            *HELD,
            {"urlPattern": "*/api/onboarding/feed-check"},
            EVERY_REQUEST,
        ]
        [answer] = [call for call in calls if call["method"] == "Fetch.fulfillRequest"]
        assert answer["params"]["requestId"] == "paused-1"
        assert answer["params"]["responseCode"] == 500
        body = json.loads(base64.b64decode(answer["params"]["body"]))
        assert body["ok"] is False

    async def test_skip_feed_checker_answers_each_check_with_its_feeds_title_and_address(
        self, drive
    ) -> None:
        answers = [
            {"ok": True, "name": "First", "url": "http://127.0.0.1:1/a.xml"},
            {"ok": True, "name": "Second", "url": "http://127.0.0.1:1/b.xml"},
        ]
        paused = [
            {**PAUSED_FEED_CHECK, "params": {**PAUSED_FEED_CHECK["params"], "requestId": rid}}
            for rid in ("paused-1", "paused-2")
        ]

        walk, calls = await drive(plan={"events": paused}, feed_check_answers=[*answers])

        assert walk.failure == ""
        [enabled] = [call for call in calls if call["method"] == "Fetch.enable"]
        assert enabled["params"]["patterns"] == [
            *HELD,
            {"urlPattern": "*/api/onboarding/feed-check"},
            EVERY_REQUEST,
        ]
        fulfilled = [c["params"] for c in calls if c["method"] == "Fetch.fulfillRequest"]
        assert [(f["requestId"], f["responseCode"]) for f in fulfilled] == [
            ("paused-1", 200),
            ("paused-2", 200),
        ]
        assert [json.loads(base64.b64decode(f["body"])) for f in fulfilled] == answers

    async def test_a_failed_step_ends_the_walk_and_is_named(self, drive) -> None:
        walk, calls = await drive(plan={"failOn": "#o-own-add"})

        assert walk.failure == "step sources: it failed"
        assert walk.passed == ["open", "opening", "language", "schedule"]
        evaluated = [c["params"]["expression"] for c in calls if c["method"] == "Runtime.evaluate"]
        assert not any("#o-cards-loading" in expression for expression in evaluated)

    async def test_a_page_that_will_not_open_fails_before_any_step(self, drive) -> None:
        walk, _ = await drive(plan={"navigateError": "net::ERR_CONNECTION_REFUSED"})

        assert walk.failure == f"step open: {URL}/onboarding: net::ERR_CONNECTION_REFUSED"
        assert walk.passed == []

    async def test_a_walk_with_no_end_is_stopped_and_names_its_step(self, drive) -> None:
        walk, _ = await drive(plan={"hangOn": "#o-own-add"}, budget_s=2)

        assert walk.failure == "step sources: no end after 2 seconds"
        assert walk.passed == ["open", "opening", "language", "schedule"]

    async def test_a_driver_that_dies_says_where_and_why(self, drive) -> None:
        plan = {"exitOn": "#o-own-add", "exitCode": 3, "exitSays": "the socket went away"}

        walk, _ = await drive(plan=plan)

        assert walk.failure == "step sources: the driver failed: the socket went away"

    async def test_a_driver_that_ends_early_and_clean_is_still_a_failure(self, drive) -> None:
        walk, _ = await drive(plan={"exitOn": "#o-own-add", "exitCode": 0})

        assert walk.failure == "step sources: the driver ended before this step answered"

    async def test_the_run_and_the_console_errors_reach_the_walk(self, drive) -> None:
        walk, _ = await drive(plan={"events": [STARTS_THE_RUN, *CONSOLE_EVENTS]})

        assert walk.run == RUN
        assert list(walk.console) == ["Failed to load " + f"{URL}/x", "bad", "Boom"]

    @staticmethod
    def _saves(answer: dict, *then: dict, response: dict | None = None) -> dict:
        """A plan whose language step saves, answered `answer` with a compressed response, or
        as `response` says, and whose page then makes the requests `then`."""
        response = response or {
            "responseStatusCode": 200 if answer.get("ok") else 400,
            "responseHeaders": [
                {"name": "Content-Type", "value": "application/json"},
                {"name": "Content-Encoding", "value": "br"},
                {"name": "content-length", "value": "12"},
            ],
        }
        save = _paused("save-1", f"{URL}/api/settings/values", "POST")
        return {
            "saveOn": "#o-timezone",
            "savedBody": json.dumps(answer),
            "saved": [save, *then, {**save, "params": {**save["params"], **response}}],
        }

    async def test_the_first_save_reaches_the_page_once_the_store_holds_it(self, drive) -> None:
        answer = {"ok": True, "values": TIMEZONE, "note": "Saved."}
        shown: list[dict] = []

        def confirm(saved: dict) -> str:
            shown.append(saved)
            return ""

        walk, calls = await drive(plan=self._saves(answer), confirm=confirm)

        assert walk.finished
        assert shown == [{"path": "/api/settings/values", "answer": answer}]
        [fulfilled] = [c["params"] for c in calls if c["method"] == "Fetch.fulfillRequest"]
        assert fulfilled["requestId"] == "save-1"
        assert fulfilled["responseCode"] == 200
        assert fulfilled["responseHeaders"] == [
            {"name": "Content-Type", "value": "application/json"}
        ]
        assert json.loads(base64.b64decode(fulfilled["body"])) == answer
        methods = [c["method"] for c in calls]
        assert methods.index("Fetch.continueRequest") < methods.index("Fetch.getResponseBody")
        evaluated = [c["params"].get("expression", "") for c in calls]
        schedule = next(i for i, e in enumerate(evaluated) if 'await next("sources",' in e)
        assert methods.index("Fetch.fulfillRequest") < schedule

    async def test_a_held_answer_without_its_headers_reaches_the_page(self, drive) -> None:
        # CDP marks the response's headers optional.
        answer = {"ok": True, "values": TIMEZONE}
        plan = self._saves(answer, response={"responseStatusCode": 200})

        walk, calls = await drive(plan=plan, budget_s=5)

        assert walk.finished
        [fulfilled] = [c["params"] for c in calls if c["method"] == "Fetch.fulfillRequest"]
        assert fulfilled["requestId"] == "save-1"
        assert fulfilled["responseHeaders"] == []
        assert json.loads(base64.b64decode(fulfilled["body"])) == answer

    async def test_a_save_the_store_does_not_hold_stops_the_walk_at_that_step(self, drive) -> None:
        answer = {"ok": True, "values": TIMEZONE, "note": "Saved."}

        def confirm(saved: dict) -> str:
            # Time for a driver that did not wait to let the save through and walk on.
            time.sleep(0.5)
            return "two installs"

        started = time.monotonic()
        walk, calls = await drive(plan=self._saves(answer), confirm=confirm)

        assert walk.failure == "step language: two installs"
        assert walk.passed == ["open", "opening"]
        methods = [c["method"] for c in calls]
        assert "Fetch.fulfillRequest" not in methods
        failed = [c["params"]["requestId"] for c in calls if c["method"] == "Fetch.failRequest"]
        assert failed == ["save-1"]
        assert methods.index("Fetch.getResponseBody") < methods.index("Fetch.failRequest")
        # Ended by itself, not by the kill behind it.
        assert time.monotonic() - started < walker.HALT_S

    async def test_a_store_row_that_is_not_json_fails_the_walk_with_a_line(
        self, drive, tmp_path: Path
    ) -> None:
        # The check runs on a thread of its own, as the store a real walk opens allows.
        store = LocalD1Client(tmp_path / "store.sqlite3")
        apply_schema(store)
        _store_raw(store, "digest.output_language", "not json")
        _store_raw(store, "general.timezone", json.dumps("UTC"))

        def confirm(saved: dict) -> str:
            return walker.store_mismatch(store, "--d1 x", walker.FIRST_CHOICE)

        walk, _ = await drive(plan=self._saves({"ok": True, "values": TIMEZONE}), confirm=confirm)

        assert walk.failure == (
            "step language: --url and --d1 x are not one install: the walk saved "
            'digest.output_language = "en" at --url, and --d1 x holds a value that is not JSON, '
            "'not json'"
        )

    @pytest.mark.parametrize(
        "headers", [pytest.param({}, id="no token"), pytest.param(ACCESS, id="a token")]
    )
    @pytest.mark.parametrize("status", [302, 307])
    @pytest.mark.parametrize(
        ("sent", "where"),
        [
            pytest.param(
                {"responseHeaders": [{"name": "Location", "value": LOGIN}]}, LOGIN, id="a location"
            ),
            pytest.param({"responseHeaders": []}, "nowhere", id="no location"),
            # CDP marks the response's headers optional.
            pytest.param({}, "nowhere", id="no headers"),
        ],
    )
    async def test_a_redirected_answer_fails_the_walk_at_once_and_is_not_followed(
        self, drive, status: int, headers: dict, sent: dict, where: str
    ) -> None:
        redirect = {"responseStatusCode": status, **sent}
        plan = {
            **self._saves({"ok": True}, response=redirect),
            "bodyError": "Can only get response body on requests captured after headers received.",
        }

        started = time.monotonic()
        walk, calls = await drive(plan=plan, headers=headers, budget_s=5)

        assert walk.failure == (
            f"step language: --url answered POST {URL}/api/settings/values with {status}, a "
            f"redirect to {where}: the walk follows no redirect of an answer it holds, so it "
            "stopped there"
        )
        let_go = [
            c["params"]["requestId"]
            for c in calls
            if c["method"] in ("Fetch.continueRequest", "Fetch.fulfillRequest")
        ]
        # Once, at the request stage: the redirect itself is never let through.
        assert let_go == ["save-1"]
        failed = [c["params"]["requestId"] for c in calls if c["method"] == "Fetch.failRequest"]
        assert failed == ["save-1"]
        assert "Fetch.getResponseBody" not in [c["method"] for c in calls]
        assert time.monotonic() - started < walker.HALT_S

    @pytest.mark.parametrize(
        "headers", [pytest.param({}, id="no token"), pytest.param(ACCESS, id="a token")]
    )
    async def test_an_answer_that_never_came_reaches_the_page_unread(
        self, drive, headers: dict
    ) -> None:
        plan = self._saves({"ok": True}, response={"responseErrorReason": "ConnectionReset"})

        walk, calls = await drive(plan=plan, headers=headers, budget_s=5)

        assert walk.finished
        methods = [c["method"] for c in calls]
        assert not {"Fetch.getResponseBody", "Fetch.fulfillRequest", "Fetch.failRequest"} & set(
            methods
        )
        continued = [c["params"] for c in calls if c["method"] == "Fetch.continueRequest"]
        # The second is at the response stage, where the request's headers are already sent.
        assert [c["requestId"] for c in continued] == ["save-1", "save-1"]
        assert continued[1] == {"requestId": "save-1"}

    async def test_a_reload_under_the_language_step_walks_on_in_the_new_document(
        self, drive
    ) -> None:
        # Once its save changes the catalog the page is worded from, the page opens the
        # schedule step in a reload, and the language step's program dies with its document.
        walk, calls = await drive(plan={"navigatesOn": "#o-timezone"})

        assert walk.finished
        evaluated = [c["params"]["expression"] for c in calls if c["method"] == "Runtime.evaluate"]

        def first(cue: str) -> int:
            return next(i for i, expression in enumerate(evaluated) if cue in expression)

        assert (
            evaluated.index("window.__walkLeft = true")
            < first("#o-timezone")
            < first('window.__walkLeft === undefined && location.pathname === "/onboarding"')
            < first('await arrive("schedule",')
            < first('await next("sources",')
        )

    async def test_a_reload_that_opens_no_schedule_step_fails_the_language_step(
        self, drive
    ) -> None:
        plan = {"navigatesOn": "#o-timezone", "failOn": 'await arrive("schedule",'}

        walk, _ = await drive(plan=plan)

        assert walk.failure == "step language: it failed"
        assert walk.passed == ["open", "opening"]

    async def test_a_reload_under_any_other_step_fails_it(self, drive) -> None:
        walk, calls = await drive(plan={"navigatesOn": "#o-own-add"})

        assert walk.failure == (
            "step sources: Runtime.evaluate: Inspected target navigated or closed"
        )
        assert walk.passed == ["open", "opening", "language", "schedule"]
        evaluated = [c["params"]["expression"] for c in calls if c["method"] == "Runtime.evaluate"]
        assert "window.__walkLeft === undefined" not in "".join(evaluated)

    async def test_a_refused_save_is_not_held(self, drive) -> None:
        shown: list[dict] = []

        walk, calls = await drive(
            plan=self._saves({"ok": False, "error": "no"}), confirm=shown.append
        )

        assert walk.finished
        assert shown == []
        [fulfilled] = [c["params"] for c in calls if c["method"] == "Fetch.fulfillRequest"]
        assert fulfilled["responseCode"] == 400

    @pytest.mark.parametrize(
        "headers", [pytest.param({}, id="no token"), pytest.param(ACCESS, id="a token")]
    )
    @pytest.mark.parametrize(
        ("step", "plan"),
        [
            pytest.param(
                "sources",
                {"saveOn": "#o-own-add", "saved": _paused("write", f"{URL}/api/sources", "POST")},
                id="a source, with no save before it",
            ),
            pytest.param(
                "schedule",
                {
                    "saveOn": 'await next("sources",',
                    "saved": _paused("write", f"{URL}/api/settings/schedule", "POST"),
                },
                id="the publish hours, with no save before them",
            ),
            pytest.param(
                "language",
                _saves({"ok": True}, _paused("write", f"{URL}/api/settings/values", "POST")),
                id="a second save while the first is checked",
            ),
            pytest.param(
                "language",
                {"saveOn": "#o-timezone", "saved": _paused("write", f"{URL}/api/x", "DELETE")},
                id="a delete in the language step",
            ),
            pytest.param(
                "model",
                {
                    "saveOn": 'await next("first-issue",',
                    "saved": _paused("write", f"{URL}/api/settings/values", "POST"),
                },
                id="the save's own route, from another step",
            ),
        ],
    )
    async def test_a_write_before_the_check_fails_the_walk_and_is_never_sent(
        self, drive, step: str, plan: dict, headers: dict
    ) -> None:
        saved = plan["saved"] if isinstance(plan["saved"], list) else [plan["saved"]]
        [write] = [event for event in saved if event["params"]["requestId"] == "write"]
        request = write["params"]["request"]

        walk, calls = await drive(plan=plan, headers=headers, budget_s=5)

        assert walk.failure == (
            f"step {step}: the page sent {request['method']} {request['url']} before the store "
            "was found to hold the language step's save, the one write let through until then, "
            "so it was stopped there and never sent"
        )
        assert walk.passed == list(walker.STEP_IDS[: walker.STEP_IDS.index(step)])
        let_go = [
            c["params"]["requestId"]
            for c in calls
            if c["method"] in ("Fetch.continueRequest", "Fetch.fulfillRequest")
        ]
        assert "write" not in let_go
        failed = [c["params"] for c in calls if c["method"] == "Fetch.failRequest"]
        assert {"requestId": "write", "errorReason": "BlockedByClient"} in failed

    async def test_a_request_paused_after_a_stop_is_failed_not_let_through(self, drive) -> None:
        after = [
            _paused("read", f"{URL}/api/settings"),
            _paused("elsewhere", "https://elsewhere.example/collect", "POST"),
        ]
        plan = {
            "saveOn": "#o-own-add",
            "saved": [_paused("write", f"{URL}/api/sources", "POST"), *after],
        }

        walk, calls = await drive(plan=plan, budget_s=5)

        assert walk.failure.startswith(f"step sources: the page sent POST {URL}/api/sources ")
        let_go = [
            c["params"]["requestId"]
            for c in calls
            if c["method"] in ("Fetch.continueRequest", "Fetch.fulfillRequest")
        ]
        assert let_go == []
        failed = [c["params"]["requestId"] for c in calls if c["method"] == "Fetch.failRequest"]
        assert failed == ["write", "read", "elsewhere"]

    async def test_reads_and_other_hosts_writes_go_on_before_the_check(self, drive) -> None:
        paused = [
            _paused("page", f"{URL}/onboarding"),
            _paused("settings", f"{URL}/api/settings"),
            _paused("elsewhere", "https://elsewhere.example/collect", "POST"),
        ]

        walk, calls = await drive(plan={"events": paused})

        assert walk.finished
        continued = [
            c["params"]["requestId"] for c in calls if c["method"] == "Fetch.continueRequest"
        ]
        assert continued == ["page", "settings", "elsewhere"]
        assert "Fetch.failRequest" not in [c["method"] for c in calls]

    async def test_writes_after_the_check_go_on(self, drive) -> None:
        plan = {
            **self._saves({"ok": True, "values": TIMEZONE}),
            "cues": [
                {"at": "#o-own-add", "events": [_paused("source", f"{URL}/api/sources", "POST")]}
            ],
        }

        walk, calls = await drive(plan=plan)

        assert walk.finished
        continued = [
            c["params"]["requestId"] for c in calls if c["method"] == "Fetch.continueRequest"
        ]
        assert continued == ["save-1", "source"]
        assert "Fetch.failRequest" not in [c["method"] for c in calls]


def test_a_failure_after_the_first_does_not_replace_it(capsys) -> None:
    walk = walker.Walk()

    walk.take({"blocked": {"method": "POST", "url": f"{URL}/api/sources"}})
    walk.take({"step": "opening", "ok": False, "detail": "Could not reach cyris"})

    assert walk.failure.startswith(f"step open: the page sent POST {URL}/api/sources before")
    assert capsys.readouterr().out.count("FAIL ") == 1


class _Recording(BaseHTTPRequestHandler):
    """Records each request's path and headers; `/onboarding` redirects to `server.redirect`."""

    def do_GET(self) -> None:
        self.server.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
        if self.path == walker.PAGE and self.server.redirect:
            self.send_response(302)
            self.send_header("Location", self.server.redirect)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b"<!doctype html><title>elsewhere</title><p>Sign in</p>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@contextlib.contextmanager
def _recording_server(redirect: str = ""):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recording)
    server.seen, server.redirect = [], redirect
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class TestTheTokenInARealBrowser:
    """Chromium itself follows the redirect, so what the second server received is the receipt.

    Opt-in by `CYRIS_CHROMIUM`, the binary to run, since it starts a browser and two servers.
    """

    async def test_a_redirect_off_the_deployment_does_not_carry_the_token(self) -> None:
        browser = os.environ.get("CYRIS_CHROMIUM")
        if not browser or shutil.which("node") is None:
            pytest.skip("set CYRIS_CHROMIUM to a Chromium binary, with node installed")
        with _recording_server() as elsewhere:
            login = f"http://127.0.0.1:{elsewhere.server_port}/login"
            with _recording_server(redirect=login) as deployment:
                url = f"http://127.0.0.1:{deployment.server_port}"
                job = walker.build_job(url, [FEED], 60, ACCESS, None)
                with walker.chromium(browser) as socket_url:
                    await walker.drive_walk(socket_url, job, 60, walker.Walk())

        sent = {name.lower(): value for name, value in ACCESS.items()}
        [(path, headers)] = [seen for seen in deployment.seen if seen[0] == walker.PAGE]
        assert {name: headers.get(name) for name in sent} == sent
        assert "/login" in [path for path, _ in elsewhere.seen]
        carried = [(path, set(sent) & set(headers)) for path, headers in elsewhere.seen]
        assert [(path, names) for path, names in carried if names] == []


def _tables(path: Path) -> dict[str, list[tuple]]:
    """Every row of every table in a local install's store."""
    with contextlib.closing(sqlite3.connect(path)) as db:
        names = [
            name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        return {
            name: sorted(db.execute(f"SELECT * FROM {name}").fetchall(), key=repr) for name in names
        }


def _reset(path: Path) -> None:
    """What `reset_trial.py --yes` sends a trial's D1, sent to a local install's store."""
    reset_trial = importlib.import_module("reset_trial")
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        present = [
            name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        for statement in reset_trial.reset_sql(present, RESET, "2026-11-10T00:00:00+00:00"):
            db.execute(statement)


class TestTheTieOnAResetTrial:
    """The walk on a store cleared by `reset_trial.py`'s own SQL, in a real Chromium against real
    `cyris triage-ui` servers: with --url and --sqlite naming two installs, then one.

    Opt-in by `CYRIS_CHROMIUM`, as `TestTheTokenInARealBrowser` is: it starts a browser, a feed
    and two servers, and the matched walk runs a first issue, with no provider key.
    """

    def test_two_installs_get_only_the_language_save_and_one_install_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        browser = os.environ.get("CYRIS_CHROMIUM")
        if not browser or shutil.which("node") is None:
            pytest.skip("set CYRIS_CHROMIUM to a Chromium binary, with node installed")
        for variable in walker.ACCESS_HEADERS.values():
            monkeypatch.delenv(variable, raising=False)
        trial, other = tmp_path / "trial", tmp_path / "other"
        for install in (trial, other):
            install.mkdir()
            shutil.copyfile(ROOT / "cyris.toml.example", install / "cyris.toml")
        trial_store, other_store = (install / local_run.SQLITE_PATH for install in (trial, other))

        def walk(url: str, store: Path, feed: str) -> int:
            return walker.main(
                ["--url", url, "--sqlite", str(store), "--feed", feed, "--skip-feed-checker"]
                + ["--first-issue-timeout", "300", "--browser", browser]
            )

        with local_run.feed_server() as feed:
            # Each server creates its install's store as it starts.
            for install in (trial, other):
                with local_run.triage_ui(install):
                    pass
            _reset(trial_store)
            with local_run.triage_ui(trial) as url:
                before = _tables(trial_store)
                two = walk(url, other_store, feed)
                after = _tables(trial_store)
            two_said = capsys.readouterr().out
            _reset(trial_store)
            with local_run.triage_ui(trial) as url:
                one = walk(url, trial_store, feed)
            one_said = capsys.readouterr().out

        assert two == walker.EXIT_FAILED, two_said
        assert (
            f"FAIL step language: --url and --sqlite {other_store} are not one install" in two_said
        )
        assert {name: rows for name, rows in after.items() if name != "settings"} == {
            name: rows for name, rows in before.items() if name != "settings"
        }
        settings_before = {row[0]: row[1:] for row in before["settings"]}
        settings_after = {row[0]: row[1:] for row in after["settings"]}
        written = {"digest.output_language": "en", "general.timezone": "UTC"}
        assert {key: json.loads(settings_after[key][0]) for key in written} == written
        assert {k: v for k, v in settings_after.items() if k not in written} == {
            k: v for k, v in settings_before.items() if k not in written
        }
        assert one == walker.EXIT_OK, one_said


LOCAL_FEED = "http://127.0.0.1:8123/feed.xml"
LOCAL_ARGS = ["--url", "http://127.0.0.1:8124", "--feed", LOCAL_FEED, "--d1", DATABASE_ID]
SKIP = "--skip-feed-checker"
REAL_FEED_XML = local_run.feed_xml(datetime(2026, 11, 9, tzinfo=UTC), "http://127.0.0.1:8123")


class TestSkipFeedChecker:
    def test_it_is_off_unless_asked_for(self) -> None:
        assert walker.parse_args(LOCAL_ARGS).skip_feed_checker is False

    def test_it_is_taken_on_loopback(self) -> None:
        assert walker.parse_args([*LOCAL_ARGS, SKIP]).skip_feed_checker is True

    @pytest.mark.parametrize(
        "args",
        [
            pytest.param(["--url", URL, "--feed", LOCAL_FEED], id="a deployment's url"),
            pytest.param(["--url", "http://127.0.0.1:8124", "--feed", FEED], id="a public feed"),
            pytest.param(
                ["--url", "http://127.0.0.1:8124", "--feed", LOCAL_FEED, FEED],
                id="one public feed among loopback ones",
            ),
        ],
    )
    def test_anything_that_can_reach_the_real_check_refuses_it(self, args, capsys) -> None:
        with pytest.raises(SystemExit) as exit_info:
            walker.parse_args([*args, "--d1", DATABASE_ID, SKIP])

        assert exit_info.value.code == walker.EXIT_REFUSED
        assert "so the real feed check runs" in capsys.readouterr().err

    def test_it_cannot_be_combined_with_a_break(self) -> None:
        with pytest.raises(SystemExit):
            walker.parse_args([*LOCAL_ARGS, SKIP, "--break", "feed-check"])

    @respx.mock
    def test_the_answer_is_the_feeds_own_title_and_address(self) -> None:
        route = respx.get(LOCAL_FEED).mock(return_value=httpx.Response(200, content=REAL_FEED_XML))

        assert walker.read_feed_check_answers([LOCAL_FEED]) == [
            {"ok": True, "name": "Walk feed", "url": LOCAL_FEED}
        ]
        assert route.call_count == 1

    @respx.mock
    def test_two_feeds_of_one_title_are_named_apart(self) -> None:
        other = "http://localhost:8123/feed.xml"
        one = respx.get(LOCAL_FEED).mock(return_value=httpx.Response(200, content=REAL_FEED_XML))
        two = respx.get(other).mock(return_value=httpx.Response(200, content=REAL_FEED_XML))

        first, second = walker.read_feed_check_answers([LOCAL_FEED, other])

        assert first["name"] != second["name"]
        assert (one.call_count, two.call_count) == (1, 1)

    @respx.mock
    @pytest.mark.parametrize(
        "answer",
        [
            pytest.param(httpx.Response(200, content=b"<html></html>"), id="not a feed"),
            pytest.param(
                httpx.Response(200, content=b'<rss version="2.0"><channel></channel></rss>'),
                id="a feed with no articles",
            ),
        ],
    )
    def test_a_feed_with_nothing_to_read_is_refused(self, answer: httpx.Response) -> None:
        route = respx.get(LOCAL_FEED).mock(return_value=answer)

        with pytest.raises(ValueError, match="not a feed with articles"):
            walker.read_feed_check_answers([LOCAL_FEED])

        assert route.call_count == 1

    @respx.mock
    def test_an_unreadable_feed_refuses_before_any_browser(
        self, d1: SqliteD1, browser: list[str], capsys
    ) -> None:
        route = respx.get(LOCAL_FEED).mock(return_value=httpx.Response(500))

        assert walker.main([*LOCAL_ARGS, SKIP]) == walker.EXIT_REFUSED

        assert "could not read the feed" in capsys.readouterr().err
        assert browser == []
        assert route.call_count == 1

    @respx.mock
    def test_a_passing_run_says_it_did_not_exercise_the_check(
        self, d1: SqliteD1, browser: list[str], monkeypatch, capsys
    ) -> None:
        route = respx.get(LOCAL_FEED).mock(return_value=httpx.Response(200, content=REAL_FEED_XML))
        jobs: list[dict] = []

        async def drive(
            socket_url: str, job: dict, budget_s: int, walk: walker.Walk, confirm
        ) -> None:
            jobs.append(job)
            _add_source(d1)
            _add_run(d1, "ok")
            walk.take({"run": RUN})
            for event in _every_step_passes():
                walk.take(event)

        monkeypatch.setattr(walker, "drive_walk", drive)

        assert walker.main([*LOCAL_ARGS, SKIP]) == walker.EXIT_OK

        assert route.call_count == 1
        [rule] = jobs[0]["intercepts"]
        assert rule["path"] == "/api/onboarding/feed-check"
        assert rule["answers"] == [{"ok": True, "name": "Walk feed", "url": LOCAL_FEED}]
        out = capsys.readouterr().out
        assert "SKIPPING FeedChecker" in out
        assert "did not exercise FeedChecker" in out

    def test_a_run_without_it_leaves_the_feed_check_to_the_server(
        self, d1: SqliteD1, browser: list[str], monkeypatch, capsys
    ) -> None:
        jobs: list[dict] = []

        async def drive(
            socket_url: str, job: dict, budget_s: int, walk: walker.Walk, confirm
        ) -> None:
            jobs.append(job)

        monkeypatch.setattr(walker, "drive_walk", drive)

        walker.main(ARGS)

        assert jobs[0]["intercepts"] == []
        assert "FeedChecker" not in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("scenario", "flags"),
        [
            ("pass", [SKIP]),
            ("break", ["--break", "feed-check"]),
            ("non-empty", []),
        ],
    )
    def test_only_the_local_pass_run_uses_it(self, scenario: str, flags: list[str]) -> None:
        bare = ["--url", "http://x", "--sqlite", "s.sqlite3", "--feed", LOCAL_FEED]

        command = local_run.walk_command(scenario, "http://x", Path("s.sqlite3"), LOCAL_FEED)

        assert command[2:] == [*bare, *flags]


# The source step's own program, on a page whose feed list has loaded and says the
# deployment already has sources. Next, if the step ever pressed it, says so.
SOURCES_HAD_PAGE = """
const shown = { offsetParent: {} };
globalThis.window = {};
globalThis.location = { hash: '#sources', pathname: '/onboarding' };
globalThis.document = {
  querySelector(selector) {
    if (selector === '#o-feeds-had') return shown;
    if (selector === '#o-next') return { ...shown, click() { throw new Error('pressed Next'); } };
    return null;
  },
  querySelectorAll: () => [],
};
let program = '';
for await (const chunk of process.stdin) program += chunk;
process.stdout.write(await (0, eval)(program));
"""


def test_the_source_step_stops_where_the_page_says_it_has_sources() -> None:
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    steps = walker.build_job(URL, [FEED], 60, {}, None)["steps"]
    [program] = [step["expression"] for step in steps if step["id"] == "sources"]

    answered = subprocess.run(
        ["node", "--input-type=module", "-e", SOURCES_HAD_PAGE],
        input=program,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )

    assert json.loads(answered.stdout) == {
        "ok": False,
        "detail": "the wizard says this deployment already has sources",
    }


FAILED_ISSUE_PAGE = """
const page = JSON.parse(process.env.PAGE);
const shown = { offsetParent: {} };
const hidden = { offsetParent: null };
const elements = {
  '#o-start': { ...shown, textContent: page.retry },
  '#o-back': shown,
  '#o-finish': shown,
  '#o-result': { ...shown, hidden: false, textContent: page.why, hasAttribute: () => false },
  '#o-issue-plan': { ...hidden, textContent: '' },
};
globalThis.window = {};
globalThis.location = { hash: '#first-issue', pathname: '/onboarding' };
globalThis.document = { querySelector: (s) => elements[s] ?? null, querySelectorAll: () => [] };
let program = '';
for await (const chunk of process.stdin) program += chunk;
process.stdout.write(await (0, eval)(program));
"""


@pytest.mark.parametrize("tag", ["en", "zh-Hant"])
def test_a_failed_first_issue_fails_its_step_in_the_language_the_page_saved(tag: str) -> None:
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    catalog = json.loads((ROOT / f"src/cyris/locales/{tag}.json").read_text(encoding="utf-8"))
    retry = catalog["onboarding.try_again"]
    page = {"retry": retry if isinstance(retry, str) else retry["text"], "why": "No articles."}
    steps = walker.build_job(URL, [FEED], 1, {}, None)["steps"]
    [program] = [step["expression"] for step in steps if step["id"] == "first-issue-ends"]

    answered = subprocess.run(
        ["node", "--input-type=module", "-e", FAILED_ISSUE_PAGE],
        input=program,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env={**os.environ, "PAGE": json.dumps(page)},
    )

    assert json.loads(answered.stdout) == {"ok": False, "detail": "No articles."}


def test_a_failed_first_issue_is_the_one_state_showing_start_beside_finish() -> None:
    script = (ROOT / "src/cyris/entrypoints/static/onboarding.js").read_text(encoding="utf-8")

    assert 'const open = ["loading", "ready"].includes(issueState);' in script
    assert '$("o-start").hidden = !["ready", "failed"].includes(issueState);' in script
    assert '$("o-finish").hidden = open;' in script


class TestTheStepsMatchThePage:
    """No browser runs here: what the steps name is held to what the page holds."""

    @staticmethod
    def _steps_source() -> str:
        prelude = walker.wizard_prelude([], 1, walker.FIRST_CHOICE)
        scripts = [step.script for step in walker.STEPS] + list(walker.ARRIVALS.values())
        return "\n".join(scripts) + prelude

    def test_every_id_a_step_names_is_in_the_template(self) -> None:
        template = (ROOT / "src/cyris/entrypoints/templates/onboarding.html.j2").read_text(
            encoding="utf-8"
        )

        named = set(re.findall(r"#(o-[a-z-]+)", self._steps_source()))

        assert named
        assert {name for name in named if f'id="{name}"' not in template} == set()

    def test_every_step_a_next_waits_for_is_one_the_page_opens(self) -> None:
        script = (ROOT / "src/cyris/entrypoints/static/onboarding.js").read_text(encoding="utf-8")
        page_steps = re.search(r"const STEPS = \[(.*?)\];", script).group(1)
        feeds_screen = re.search(r'const FEEDS_SCREEN = "(.*?)";', script).group(1)

        waited_for = re.findall(r'await next\("([^"]+)"', self._steps_source())

        language, schedule, sources, *rest = re.findall(r'"([^"]+)"', page_steps)
        assert language == "language"
        assert waited_for == [schedule, sources, feeds_screen, *rest]

    def test_the_language_step_arrives_in_a_reload_where_its_next_goes(self) -> None:
        script = (ROOT / "src/cyris/entrypoints/static/onboarding.js").read_text(encoding="utf-8")
        [language] = [step.script for step in walker.STEPS if step.id == "language"]

        # The page reloads only from advance(), on the step its Next would have opened.
        assert "if (relanguage) return reloadOn(STEPS[index + 1]);" in script
        assert set(walker.ARRIVALS) == {"language"}
        assert re.findall(r'await arrive\("([^"]+)", (\S+),', walker.ARRIVALS["language"]) == (
            re.findall(r'await next\("([^"]+)", (\S+)\)', language)
        )

    def test_the_first_issue_waits_past_the_pages_own_limit(self) -> None:
        script = (ROOT / "src/cyris/entrypoints/static/onboarding.js").read_text(encoding="utf-8")
        minutes = re.search(r"const RUN_WAIT_MS = (\d+) \* 60 \* 1000;", script).group(1)

        assert int(minutes) * 60 < walker.FIRST_ISSUE_TIMEOUT_S

    def test_the_timeout_flag_defaults_to_the_stated_wait(self) -> None:
        assert walker.parse_args(ARGS).first_issue_timeout == walker.FIRST_ISSUE_TIMEOUT_S


def test_the_local_feeds_newest_half_is_inside_the_runs_window() -> None:
    now = datetime(2026, 11, 9, 12, tzinfo=UTC)
    window_start = now - timedelta(hours=24)

    entries = feedparser.parse(local_run.feed_xml(now, "http://127.0.0.1:1")).entries
    dates = sorted(datetime(*entry.published_parsed[:6], tzinfo=UTC) for entry in entries)

    assert len(dates) == len(local_run.ARTICLE_AGES_H)
    assert [date >= window_start for date in dates] == [False, False, False, True, True, True]
    assert all(date <= now for date in dates)
