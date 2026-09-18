#!/usr/bin/env python3
"""Drive /settings in headless Chromium and check what a reader actually gets.

pytest holds the settings page's markup and CSS, but not what its script does
in a browser: which category a hash opens, when Save can be pressed, where a
result appears, how the source editor behaves, and whether the page fits a
phone. This runs those checks against a real `TriageServer`, served in-process
from fixtures whose stores live in memory, so what a check wrote can be read
back afterwards.

Every check carries a sabotage that breaks the thing it reads. `--self-test`
runs each check clean, where it must pass, and sabotaged, where it must fail,
so a check that cannot fail is reported rather than trusted.

Chromium is driven over the DevTools Protocol through a short Node script, as
`css_computed.py` does and for the same reason: Node 22+ ships a WebSocket
client and Python's standard library has none. No browser-automation package is
installed for this, and none may be.

This script must never be collected by pytest: it needs Chromium and Node,
which makes it a reviewer-run gate rather than a test. Importing it is safe,
and `tests/test_css_receipt.py` does so to check its fixtures and registry.
"""

import argparse
import asyncio
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import aiohttp
from aiohttp import web
from css_computed import _devtools_port, _page_socket, find_browser

from cyris.adapters.notify import mask_discord_webhook_url
from cyris.config import LLMProviderConfig
from cyris.diagnostics.doctor import Check as DoctorCheck
from cyris.domain.models import SourceConfig, Tier
from cyris.entrypoints.triage_server import TriageServer

# Named rather than random so a leak test can look for exactly these strings.
SENTINEL_KEY = "cyris-probe-sentinel-key"
STORED_WEBHOOK = "https://discord.com/api/webhooks/123/abcTOKEN"
OFFLINE_DETAIL = "cyris-probe: answered offline"

# What /api/settings must resolve inside the probe environment. Asserted from the
# server's answer, not from the variables this script set, because a key can
# also arrive from somewhere this script does not control.
EXPECTED_READINESS = {"anthropic": True, "gemini": True, "openai": False, "workers_ai": False}

CHECK_TIMEOUT_S = 30
HEIGHT = 900
MINIMUM_NODE = 22


class FakeSettings:
    """Stands in for `D1Settings` and records every write the page makes."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def set(self, values: dict) -> None:
        self.calls.append(dict(values))


class FakeSourceStore:
    """Stands in for the D1 `sources` table; `sources` is the receipt."""

    def __init__(self, sources: dict[str, SourceConfig]) -> None:
        self.sources = sources

    def list_sources(self) -> dict[str, SourceConfig]:
        return dict(self.sources)

    def upsert(self, source: SourceConfig) -> None:
        self.sources[source.name] = source

    def delete(self, name: str) -> None:
        self.sources.pop(name, None)

    def replace_all(self, sources: dict[str, SourceConfig]) -> None:
        self.sources.clear()
        self.sources.update(sources)


class _NoArticles:
    """The settings routes never touch the article store."""


def _seed_sources() -> dict[str, SourceConfig]:
    listed = [
        SourceConfig(
            name="Simon Willison",
            type="rss",
            tier=Tier.SUMMARIZE,
            url="https://simonwillison.net/atom/everything/",
            tags=["ai", "tools"],
        ),
        SourceConfig(
            name="Hacker News",
            type="rss",
            tier=Tier.FILTER,
            url="https://hnrss.org/frontpage?points=200",
            tags=["news", "tech"],
        ),
        SourceConfig(
            name="曼報",
            type="newsletter",
            tier=Tier.SUMMARIZE,
            email_match="from:manpao@substack.com",
            homepage="https://manpaoreport.com",
            tags=["business"],
        ),
        # Markup as a name: the page must print it, never parse it.
        SourceConfig(name="<b>x</b>", type="rss", tier=Tier.FAN, url="https://example.test/x.xml"),
    ]
    return {source.name: source for source in listed}


@dataclass
class Fixture:
    """One server and the in-memory stores a check reads its receipt from."""

    app: web.Application
    settings: FakeSettings | None
    sources: dict[str, SourceConfig]


def build_fixture(kind: str) -> Fixture:
    """A `readonly` deployment (no settings store, no source table) or a `writable` one."""
    common = {
        "llm_provider": LLMProviderConfig(provider="gemini", model=""),
        "schedule": ["08:00", "20:00"],
        "max_featured": 5,
        "notify_webhook": STORED_WEBHOOK,
        "sources": _seed_sources(),
        "sources_origin": "sources.yaml",
    }
    if kind == "readonly":
        server = TriageServer(_NoArticles(), **common)
        return Fixture(server._app, None, server._sources)
    if kind == "writable":
        settings = FakeSettings()
        store = FakeSourceStore(_seed_sources())
        server = TriageServer(_NoArticles(), settings=settings, source_store=store, **common)
        return Fixture(server._app, settings, store.sources)
    raise ValueError(f"unknown fixture {kind!r}")


@contextlib.contextmanager
def probe_environment() -> Iterator[SimpleNamespace]:
    """Pin the provider keys, and answer the LLM and Discord probes offline.

    Runs from an empty directory so no `.env` there can bind a key. Yields the
    two patched probes so a caller can see they were the ones answering.
    """
    absent = {
        LLMProviderConfig(provider="openai").api_key_env_var,
        LLMProviderConfig(provider="workers_ai").api_key_env_var,
        # workers_ai falls back to these, so removing its own variable is not enough.
        "CLOUDFLARE_EMBEDDING_API_TOKEN",
        "CLOUDFLARE_ACCOUNT_ID",
    }
    offline = DoctorCheck(name="probe", status="ok", detail=OFFLINE_DETAIL)
    llm = mock.AsyncMock(return_value=offline)
    discord = mock.AsyncMock(return_value=offline)
    present = {"GEMINI_API_KEY": SENTINEL_KEY, "ANTHROPIC_API_KEY": SENTINEL_KEY}
    with (
        tempfile.TemporaryDirectory(prefix="settings-probe-") as home,
        contextlib.chdir(home),
        mock.patch.dict(os.environ, present),
        mock.patch("cyris.diagnostics.doctor.probe_llm", llm),
        mock.patch("cyris.entrypoints.triage_server.probe_discord", discord),
    ):
        for name in absent:
            os.environ.pop(name, None)
        yield SimpleNamespace(llm=llm, discord=discord)


@dataclass(frozen=True)
class Check:
    """One behaviour, read from the DOM and, where something was written, the stores.

    `act` performs the steps and may record observations in `ctx`; `sabotage`
    runs after it in a self-test; `script` then asserts with `expect`. A
    sabotage that cannot be applied through the page is a `sabotage_preload`
    instead, installed before the page loads.
    """

    id: str
    fixture: str
    path: str | tuple[str, ...]
    script: str
    sabotage: str = ""
    width: int = 1440
    act: str = ""
    preload: str = ""
    sabotage_preload: str = ""
    reload: bool = False
    setup: Callable[[Fixture], None] | None = None
    receipt: Callable[[Fixture], str | None] | None = None

    @property
    def paths(self) -> tuple[str, ...]:
        return (self.path,) if isinstance(self.path, str) else self.path


# Installed before the page loads: the settings request never answers.
HOLD_SETTINGS = """
const realFetch = window.fetch;
window.fetch = (input, init) =>
  String(input).endsWith("/api/settings") && !(init && init.method)
    ? new Promise(() => {})
    : realFetch(input, init);
"""


def rewrite_source_post(mutation: str) -> str:
    """A preload that changes the body of the page's POST /api/sources before it leaves."""
    return f"""
const realFetch = window.fetch;
window.fetch = (input, init) => {{
  if (String(input).endsWith("/api/sources") && init && init.method === "POST") {{
    const body = JSON.parse(init.body);
    {mutation}
    init = {{...init, body: JSON.stringify(body)}};
  }}
  return realFetch(input, init);
}};
"""


def _stored(name: str, **wanted) -> Callable[[Fixture], str | None]:
    """A receipt: the source store holds `name` with exactly these field values."""

    def receipt(fixture: Fixture) -> str | None:
        source = fixture.sources.get(name)
        if source is None:
            return f"{name} is not stored"
        wrong = {k: getattr(source, k) for k, v in wanted.items() if getattr(source, k) != v}
        return f"{name} stored {wrong}" if wrong else None

    return receipt


def _last_call(wanted: dict) -> Callable[[Fixture], str | None]:
    """A receipt: the settings store's last write was exactly `wanted`."""

    def receipt(fixture: Fixture) -> str | None:
        calls = fixture.settings.calls
        return None if calls and calls[-1] == wanted else f"settings writes: {calls}"

    return receipt


NEW_WEBHOOK = "https://discord.com/api/webhooks/9/NEWTOKEN"
NEW_WEBHOOK_MASKED = mask_discord_webhook_url(NEW_WEBHOOK)

SAVE_FEATURED_7 = """
await settingsLoaded();
setValue($("#max-featured"), "7");
saveOf("digest").click();
await waitFor(() => visible(noticeOf("digest")) && saveOf("digest").disabled, "the save");
"""

CHECKS: list[Check] = [
    Check(
        id="site-bar-current",
        fixture="readonly",
        path="/settings",
        script="""
            const current = $$('.site-nav a[aria-current="page"]');
            const names = current.map((a) => a.textContent.trim());
            expect(names.length === 1 && names[0] === "Settings", `current: ${names}`);
        """,
        sabotage="""$('.site-nav a[aria-current="page"]').removeAttribute("aria-current");""",
    ),
    Check(
        id="no-credential-in-dom",
        fixture="writable",
        path="/settings#notifications",
        act="""await waitFor(() => $("#discord-webhook").value, "the stored webhook");""",
        script=f"""
            const secrets = {json.dumps(["abcTOKEN", SENTINEL_KEY])};
            const values = $$("input").map((input) => input.value);
            const text = [document.documentElement.outerHTML, ...values].join("\\n");
            const leaked = secrets.filter((secret) => text.includes(secret));
            expect(leaked.length === 0, `leaked: ${{leaked}}`);
            expect($("#discord-webhook").value, "the webhook field is empty");
        """,
        sabotage=f"""$("#discord-webhook").value = {json.dumps(STORED_WEBHOOK)};""",
    ),
    Check(
        id="hash-direct",
        fixture="readonly",
        path="/settings#digest",
        script="""
            expect(same(panels(), ["digest"]), `visible panels: ${panels()}`);
            expect(same(currentTabs(), ["#digest"]), `current: ${currentTabs()}`);
        """,
        sabotage="""$('.tab[data-tab="model"]').hidden = false;""",
    ),
    Check(
        id="hash-refresh",
        fixture="readonly",
        path="/settings#sources",
        reload=True,
        script="""expect(same(panels(), ["sources"]), `visible panels: ${panels()}`);""",
        sabotage="""$('.tab[data-tab="sources"]').hidden = true;""",
    ),
    Check(
        id="hash-default",
        fixture="readonly",
        path=("/settings", "/settings#nope"),
        script="""
            expect(same(panels(), ["model"]), `visible panels: ${panels()}`);
            expect(same(currentTabs(), ["#model"]), `current: ${currentTabs()}`);
        """,
        sabotage="""$('.tab[data-tab="digest"]').hidden = false;""",
    ),
    Check(
        id="hash-nav-click",
        fixture="readonly",
        path="/settings",
        act="""
            $('.settings-nav a[href="#notifications"]').click();
            await waitFor(() => same(panels(), ["notifications"]), "the notifications panel");
        """,
        script="""
            expect(location.hash === "#notifications", `hash: ${location.hash}`);
            expect(same(panels(), ["notifications"]), `visible panels: ${panels()}`);
            expect(same(currentTabs(), ["#notifications"]), `current: ${currentTabs()}`);
        """,
        sabotage="""$('.settings-nav a[aria-current]').removeAttribute("aria-current");""",
    ),
    Check(
        id="model-readiness",
        fixture="writable",
        path="/settings#model",
        act="await providersLoaded();",
        script="""
            const gemini = choice("gemini"), openai = choice("openai");
            const state = (row) => $(".label", row).textContent.trim();
            expect(state(gemini) === "Key ready", `gemini: ${state(gemini)}`);
            expect(!$("input", gemini).disabled && $("input", gemini).checked, "gemini unchosen");
            expect(state(openai) === "OPENAI_API_KEY missing", `openai: ${state(openai)}`);
            expect(openai.classList.contains("unavailable"), "openai is not unavailable");
            expect($("input", openai).disabled, "openai can be chosen");
        """,
        sabotage="""$('input[value="openai"]').disabled = false;""",
    ),
    Check(
        id="model-placeholder",
        fixture="writable",
        path="/settings#model",
        act="""
            await providersLoaded();
            const answer = await (await fetch("/api/settings")).json();
            ctx.fallback = answer.providers.find((p) => p.name === "gemini").default_model;
        """,
        script="""
            const placeholder = $("#model-input").placeholder;
            expect(placeholder === `Empty uses ${ctx.fallback}`, `placeholder: ${placeholder}`);
        """,
        sabotage="""$("#model-input").placeholder = "";""",
    ),
    Check(
        id="save-disabled-while-loading",
        fixture="writable",
        path="/settings",
        preload=HOLD_SETTINGS,
        act="await sleep(300);",
        script="""
            const live = ["model", "digest", "notifications"].filter((t) => !saveOf(t).disabled);
            expect(live.length === 0, `enabled before settings loaded: ${live}`);
        """,
        sabotage="""saveOf("digest").disabled = false;""",
    ),
    Check(
        id="dirty-enables-save",
        fixture="writable",
        path="/settings#digest",
        act="""
            await settingsLoaded();
            setValue($("#max-featured"), "7");
        """,
        script="""
            expect(!saveOf("digest").disabled, "the digest Save is disabled");
            expect(navOf("digest").classList.contains("dirty"), "Digest has no dirty mark");
            const dot = getComputedStyle($(".dirty-dot", navOf("digest"))).visibility;
            expect(dot === "visible", `the dot is ${dot}`);
            expect(saveOf("model").disabled, "the Model Save is enabled");
        """,
        sabotage="""saveOf("digest").disabled = true;""",
    ),
    Check(
        id="revert-disables-save",
        fixture="writable",
        path="/settings#digest",
        act="""
            await settingsLoaded();
            setValue($("#max-featured"), "7");
            setValue($("#max-featured"), "5");
        """,
        script="""
            expect(saveOf("digest").disabled, "the digest Save is enabled");
            expect(!navOf("digest").classList.contains("dirty"), "Digest is still marked");
        """,
        sabotage="""navOf("digest").classList.add("dirty");""",
    ),
    Check(
        id="dirty-radio",
        fixture="writable",
        path="/settings#model",
        act="""
            await settingsLoaded();
            $('input[value="anthropic"]').click();
            ctx.enabledByAnthropic = !saveOf("model").disabled;
            $('input[value="gemini"]').click();
        """,
        script="""
            expect(ctx.enabledByAnthropic, "choosing anthropic left Save disabled");
            expect(saveOf("model").disabled, "choosing gemini again left Save enabled");
        """,
        sabotage="""saveOf("model").disabled = false;""",
    ),
    Check(
        id="sources-columns",
        fixture="writable",
        path="/settings#sources",
        act="await sourcesLoaded();",
        script="""
            const heads = $$("table.src th").map((th) => th.textContent);
            const wanted = ["Name", "Type", "Tier", "Feed or sender", "Tags"];
            expect(same(heads, wanted), `headers: ${heads}`);
            expect(rowNames().length === 4, `rows: ${rowNames()}`);
            expect(rowNames().includes("<b>x</b>"), `names: ${rowNames()}`);
            expect(!$("table.src b"), "a source name was parsed as markup");
        """,
        sabotage="""$("tr.src-row td").innerHTML = "<b>x</b>";""",
    ),
    Check(
        id="sources-filter",
        fixture="writable",
        path="/settings#sources",
        act="""
            await sourcesLoaded();
            $('[data-filter="newsletter"]').click();
        """,
        script="""
            expect(same(rowNames(), ["曼報"]), `rows: ${rowNames()}`);
            const pressed = (f) => $(`[data-filter="${f}"]`).getAttribute("aria-pressed");
            expect(pressed("newsletter") === "true", "Newsletter is not pressed");
            expect(pressed("all") === "false", "All is still pressed");
        """,
        sabotage="""
            const row = $("#src-body").insertRow();
            row.className = "src-row";
            row.insertCell().textContent = "Hacker News";
        """,
    ),
    Check(
        id="sources-empty-filter",
        fixture="writable",
        path="/settings#sources",
        setup=lambda fixture: fixture.sources.pop("曼報"),
        act="""
            await sourcesLoaded();
            $('[data-filter="newsletter"]').click();
        """,
        script="""
            const rows = $$("#src-body tr").map((row) => row.textContent.trim());
            expect(same(rows, ["No newsletter sources."]), `rows: ${rows}`);
        """,
        sabotage="""$("#src-body").replaceChildren();""",
    ),
    Check(
        id="editor-opens-under-row",
        fixture="writable",
        path="/settings#sources",
        act="""await openRow("Hacker News");""",
        script="""
            expect(rowOf("Hacker News").nextElementSibling === editor(), "not under its row");
            const title = $(".editor-title", editor()).textContent;
            expect(title === "Editing Hacker News", `heading: ${title}`);
            const name = $("#e-name", editor());
            expect(name.readOnly && name.value === "Hacker News", "the name is editable");
            expect(visible(editorAct("retire")), "Retire is hidden");
            expect($$("tr.editor").length === 1, "more than one editor");
        """,
        sabotage="""$("#src-body").prepend(editor());""",
    ),
    Check(
        id="editor-cancel",
        fixture="writable",
        path="/settings#sources",
        act="""
            await openRow("Hacker News");
            setValue($("#e-tags", editor()), "changed");
            editorAct("cancel").click();
        """,
        script="""
            expect(!editor(), "the editor is still open");
            expect(!$("tr.src-row.open"), "a row is still marked open");
            expect(!navOf("sources").classList.contains("dirty"), "Sources is still marked");
        """,
        sabotage="""
            $("#src-body").append($("#editor-tpl").content.firstElementChild.cloneNode(true));
        """,
        receipt=lambda fixture: (
            None
            if fixture.sources["Hacker News"].tags == ["news", "tech"]
            else f"Hacker News changed: {fixture.sources['Hacker News']}"
        ),
    ),
    Check(
        id="editor-dirty",
        fixture="writable",
        path="/settings#sources",
        act="""
            await openRow("Hacker News");
            ctx.disabledUntouched = editorAct("save").disabled;
            setValue($("#e-tags", editor()), "news, tech, x");
        """,
        script="""
            expect(ctx.disabledUntouched, "Save source was enabled before any change");
            expect(!editorAct("save").disabled, "Save source stayed disabled");
            expect(navOf("sources").classList.contains("dirty"), "Sources is not marked");
        """,
        sabotage="""editorAct("save").disabled = true;""",
    ),
    Check(
        id="editor-heading-escapes",
        fixture="writable",
        path="/settings#sources",
        act="""await openRow("<b>x</b>");""",
        script="""
            const title = $(".editor-title", editor());
            expect(title.textContent === "Editing <b>x</b>", `heading: ${title.textContent}`);
            expect(!$("b", title), "the name was parsed as markup");
        """,
        sabotage="""$(".editor-title", editor()).innerHTML = "Editing <b>x</b>";""",
    ),
    Check(
        id="editor-add-at-top",
        fixture="writable",
        path="/settings#sources",
        act="""
            await sourcesLoaded();
            $("#add-source").click();
            await waitFor(editor, "the editor");
            ctx.disabledEmpty = editorAct("save").disabled;
            const name = $("#e-name", editor());
            ctx.name = {readOnly: name.readOnly, value: name.value};
            setValue($("#e-name", editor()), "New Feed");
        """,
        script="""
            expect($("#src-body").firstElementChild === editor(), "not at the top of the table");
            const title = $(".editor-title", editor()).textContent;
            expect(title === "New source", `heading: ${title}`);
            expect(!ctx.name.readOnly && ctx.name.value === "", "the name is not an empty field");
            expect(!visible(editorAct("retire")), "Retire is shown for a new source");
            expect(ctx.disabledEmpty, "Save source was enabled without a name");
            expect(!editorAct("save").disabled, "Save source stayed disabled with a name");
        """,
        sabotage="""editorAct("retire").hidden = false;""",
    ),
    Check(
        id="editor-type-fields",
        fixture="writable",
        path="/settings#sources",
        act="""
            await openRow("曼報");
            ctx.newsletter = shownFields();
            $('[data-type="rss"]', editor()).click();
        """,
        script="""
            const both = ["e-email", "e-home"];
            expect(same(ctx.newsletter, both), `newsletter shows ${ctx.newsletter}`);
            expect(same(shownFields(), ["e-url"]), `rss shows ${shownFields()}`);
        """,
        sabotage="""$("#e-home", editor()).closest("[data-for]").hidden = false;""",
    ),
    Check(
        id="source-save-nulls-hidden-fields",
        fixture="writable",
        path="/settings#sources",
        act="""
            await openRow("Hacker News");
            $('[data-type="newsletter"]', editor()).click();
            setValue($("#e-email", editor()), "from:hn@example.com");
            editorAct("save").click();
            await waitFor(() => editor() && visible($(".notice", editor())), "the save");
        """,
        script="",
        sabotage_preload=rewrite_source_post(
            'body.url = "https://hnrss.org/frontpage?points=200";'
        ),
        receipt=_stored(
            "Hacker News",
            type="newsletter",
            url=None,
            email_match="from:hn@example.com",
            homepage=None,
        ),
    ),
    Check(
        id="source-save-tags",
        fixture="writable",
        path="/settings#sources",
        act="""
            await openRow("Simon Willison");
            setValue($("#e-tags", editor()), " ai, , tools ");
            editorAct("save").click();
            await waitFor(() => editor() && visible($(".notice", editor())), "the save");
        """,
        script="",
        sabotage_preload=rewrite_source_post('body.tags = ["ai", "", "tools"];'),
        receipt=_stored("Simon Willison", tags=["ai", "tools"], email_match=None, homepage=None),
    ),
    Check(
        id="notice-ok-beside-save",
        fixture="writable",
        path="/settings#digest",
        act=SAVE_FEATURED_7,
        script="""
            const notice = noticeOf("digest");
            expect(!notice.classList.contains("err"), `an error: ${notice.textContent}`);
            expect(notice.textContent.includes("Featured sections: 7."), notice.textContent);
            expect(notice.textContent.includes("Effective next digest."), notice.textContent);
            expect(saveOf("digest").disabled, "the digest Save is still enabled");
        """,
        sabotage="""noticeOf("digest").classList.add("err");""",
        receipt=_last_call({"digest.max_featured": 7}),
    ),
    Check(
        id="notice-err-beside-save",
        fixture="writable",
        path="/settings#digest",
        act="""
            await settingsLoaded();
            setValue($("#morning"), "25");
            saveOf("digest").click();
            await waitFor(() => visible(noticeOf("digest")), "the notice");
        """,
        script="""
            const notice = noticeOf("digest"), text = notice.textContent.trim();
            expect(notice.classList.contains("err"), `not an error: ${text}`);
            expect(visible(notice) && text && text !== "HTTP 400", `notice: ${text}`);
            expect(!saveOf("digest").disabled, "the digest Save was disabled");
            expect(navOf("digest").classList.contains("dirty"), "Digest lost its dirty mark");
        """,
        sabotage="""noticeOf("digest").hidden = true;""",
        receipt=lambda fixture: (
            f"a schedule was stored: {fixture.settings.calls}"
            if any("general.digest_schedule" in call for call in fixture.settings.calls)
            else None
        ),
    ),
    Check(
        id="notice-hides-on-edit",
        fixture="writable",
        path="/settings#digest",
        act=SAVE_FEATURED_7 + """setValue($("#max-featured"), "8");""",
        script="""expect(!visible(noticeOf("digest")), "the old result is still shown");""",
        sabotage="""noticeOf("digest").hidden = false;""",
    ),
    Check(
        id="model-notice-ok",
        fixture="writable",
        path="/settings#model",
        act="""
            await settingsLoaded();
            $('input[value="anthropic"]').click();
            saveOf("model").click();
            await waitFor(() => saveOf("model").disabled && !noticeOf("model").textContent
              .startsWith("Checking"), "the provider's answer");
        """,
        script=f"""
            const notice = noticeOf("model");
            expect(!notice.classList.contains("err"), `an error: ${{notice.textContent}}`);
            expect(notice.textContent.includes({json.dumps(OFFLINE_DETAIL)}), notice.textContent);
        """,
        sabotage="""noticeOf("model").classList.add("err");""",
        receipt=_last_call({"llm_provider.provider": "anthropic", "llm_provider.model": ""}),
    ),
    Check(
        id="notify-notice-masked",
        fixture="writable",
        path="/settings#notifications",
        act=f"""
            await settingsLoaded();
            setValue($("#discord-webhook"), {json.dumps(NEW_WEBHOOK)});
            saveOf("notifications").click();
            await waitFor(() => visible($("#notify-result")), "the notice");
        """,
        script=f"""
            const notice = $("#notify-result"), field = $("#discord-webhook");
            expect(!notice.classList.contains("err"), `an error: ${{notice.textContent}}`);
            expect(field.value === {json.dumps(NEW_WEBHOOK_MASKED)}, `field: ${{field.value}}`);
            expect(!document.body.textContent.includes("NEWTOKEN"), "the token is on the page");
        """,
        sabotage=f"""$("#discord-webhook").value = {json.dumps(NEW_WEBHOOK)};""",
        receipt=_last_call({"notify.discord_webhook_url": NEW_WEBHOOK}),
    ),
    Check(
        id="source-notice-beside-save",
        fixture="writable",
        path="/settings#sources",
        act="""
            await openRow("Hacker News");
            setValue($("#e-tags", editor()), "news");
            editorAct("save").click();
            await waitFor(() => editor() && visible($(".notice", editor())), "the save");
        """,
        script="""
            expect(rowOf("Hacker News").nextElementSibling === editor(), "not under its row");
            const title = $(".editor-title", editor()).textContent;
            expect(title === "Editing Hacker News", `heading: ${title}`);
            const text = editorAct("save").parentElement.querySelector(".notice").textContent;
            expect(text === "Hacker News saved. Effective next run.", `notice: ${text}`);
            expect(editorAct("save").disabled, "Save source is still enabled");
        """,
        sabotage="""editor().remove();""",
    ),
]

PRELUDE = """
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const visible = (el) => !!el && el.offsetParent !== null;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const expect = (condition, detail) => { if (!condition) throw new Error(detail); };
const waitFor = async (probe, what, ms = 5000) => {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    try { const value = probe(); if (value) return value; } catch {}
    await sleep(25);
  }
  throw new Error(`timed out waiting for ${what}`);
};
const setValue = (el, value) => {
  el.value = value;
  el.dispatchEvent(new Event("input", {bubbles: true}));
  el.dispatchEvent(new Event("change", {bubbles: true}));
};
const panels = () => $$(".tab").filter(visible).map((panel) => panel.dataset.tab);
const currentTabs = () =>
  $$(".settings-nav a[aria-current]").map((link) => link.getAttribute("href"));
const same = (actual, wanted) => JSON.stringify(actual) === JSON.stringify(wanted);
const choice = (name) => $(`input[name=provider][value="${name}"]`).closest("label.choice");
const providersLoaded = () => waitFor(() => $$("input[name=provider]").length, "providers");
const saveOf = (tab) => $(`form.tab[data-tab="${tab}"] button[type="submit"]`);
const navOf = (tab) => $(`.settings-nav a[data-tab="${tab}"]`);
const settingsLoaded = () => waitFor(() => $("#max-featured").value, "settings");
const rowNames = () => $$("tr.src-row").map((row) => $("td", row).textContent);
const sourcesLoaded = () => waitFor(() => $$("tr.src-row").length, "source rows");
const rowOf = (name) => $(`tr.src-row[data-name="${CSS.escape(name)}"]`);
const editor = () => $("tr.editor");
const editorAct = (act) => $(`[data-act="${act}"]`, editor());
const openRow = async (name) => {
  await sourcesLoaded();
  rowOf(name).click();
  return waitFor(editor, "the editor");
};
const shownFields = () =>
  $$("[data-for]", editor()).filter(visible).map((field) => $("input", field).id);
const noticeOf = (tab) => $(".actions-line .notice", $(`form.tab[data-tab="${tab}"]`));
const ctx = {};
"""


def check_expression(check: Check, sabotaged: bool) -> str:
    """The page-side program: act, maybe sabotage, then assert."""
    sabotage = check.sabotage if sabotaged else ""
    return f"""(async () => {{
{PRELUDE}
try {{ {{ {check.act} }} }} catch (error) {{
  return JSON.stringify({{ok: false, detail: `act: ${{error.message || error}}`}});
}}
try {{ {{ {sabotage} }} }} catch (error) {{
  return JSON.stringify({{ok: false, sabotageError: String(error.message || error)}});
}}
try {{ {{ {check.script} }} }} catch (error) {{
  return JSON.stringify({{ok: false, detail: String(error.message || error)}});
}}
return JSON.stringify({{ok: true}});
}})()"""


DRIVER = """
const socket = new WebSocket(process.env.CDP_WS);
const pending = new Map();
let nextId = 0;
socket.addEventListener('message', (event) => {
  const message = JSON.parse(event.data);
  const settle = pending.get(message.id);
  if (settle) { pending.delete(message.id); settle(message); }
});
const call = (method, params = {}) => new Promise((resolve, reject) => {
  const id = ++nextId;
  pending.set(id, (message) => message.error
    ? reject(new Error(`${method}: ${message.error.message}`))
    : resolve(message.result));
  socket.send(JSON.stringify({ id, method, params }));
});
const evaluate = async (expression) => {
  const answer = await call('Runtime.evaluate',
    { expression, awaitPromise: true, returnByValue: true });
  if (answer.exceptionDetails) throw new Error(JSON.stringify(answer.exceptionDetails));
  return answer.result.value;
};
// A navigation that has not committed yet still reports the old document as
// complete, so each wait also names what the new document must be.
const settled = async (condition) => {
  for (let attempt = 0; attempt < 400; attempt++) {
    try {
      if (await evaluate(`(${condition}) && document.readyState === 'complete'`)) return;
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  throw new Error(`page never settled: ${condition}`);
};
await new Promise((resolve, reject) => {
  socket.addEventListener('open', resolve);
  socket.addEventListener('error', reject);
});
const job = JSON.parse(process.env.PROBE_JOB);
await call('Page.enable');
await call('Network.enable');
// Fonts come from Google; the checks read layout, not type, and wait on nothing.
await call('Network.setBlockedURLs', { urls: ['*fonts.googleapis.com*', '*fonts.gstatic.com*'] });
// --window-size has a ~500px floor; the emulation override reaches a phone.
await call('Emulation.setDeviceMetricsOverride',
  { width: job.width, height: job.height, deviceScaleFactor: 1, mobile: false });
const results = [];
try {
  for (const url of job.urls) {
    await call('Page.navigate', { url: 'about:blank' });
    await settled(`location.href === 'about:blank'`);
    const added = [];
    for (const source of job.preloads) {
      added.push((await call('Page.addScriptToEvaluateOnNewDocument', { source })).identifier);
    }
    await call('Page.navigate', { url });
    await settled(`location.origin === ${JSON.stringify(new URL(url).origin)}`);
    if (job.reload) {
      await evaluate('window.__probeBeforeReload = true');
      await call('Page.reload');
      await settled('window.__probeBeforeReload === undefined');
    }
    results.push(JSON.parse(await evaluate(job.expression)));
    for (const identifier of added) {
      await call('Page.removeScriptToEvaluateOnNewDocument', { identifier });
    }
    if (!results.at(-1).ok) break;
  }
} finally {
  await call('Emulation.clearDeviceMetricsOverride');
}
process.stdout.write(JSON.stringify(results));
socket.close();
"""


@dataclass(frozen=True)
class Outcome:
    ok: bool
    detail: str = ""
    sabotage_error: str = ""


async def _serve(app: web.Application) -> tuple[web.AppRunner, str]:
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    host, port = runner.addresses[0][:2]
    return runner, f"http://{host}:{port}"


async def _drive(socket_url: str, job: dict) -> list[dict]:
    # Spawned from the loop that serves the fixture: a blocking subprocess call
    # would freeze the server the page is talking to.
    process = await asyncio.create_subprocess_exec(
        "node",
        "--input-type=module",
        "-e",
        DRIVER,
        env={**os.environ, "CDP_WS": socket_url, "PROBE_JOB": json.dumps(job)},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), CHECK_TIMEOUT_S)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    if process.returncode != 0:
        raise RuntimeError(f"driver failed: {stderr.decode().strip()}")
    return json.loads(stdout)


async def run_check(check: Check, socket_url: str, sabotaged: bool) -> Outcome:
    """Serve a fresh fixture, drive the page through the check, then read the receipt."""
    fixture = build_fixture(check.fixture)
    if check.setup:
        check.setup(fixture)
    runner, base = await _serve(fixture.app)
    try:
        preloads = [check.preload] if check.preload else []
        if sabotaged and check.sabotage_preload:
            preloads.append(check.sabotage_preload)
        job = {
            "urls": [base + path for path in check.paths],
            "width": check.width,
            "height": HEIGHT,
            "preloads": preloads,
            "reload": check.reload,
            "expression": check_expression(check, sabotaged),
        }
        try:
            results = await _drive(socket_url, job)
        except TimeoutError:
            return Outcome(False, "timeout")
        for result in results:
            if error := result.get("sabotageError"):
                return Outcome(False, f"sabotage failed to apply: {error}", error)
            if not result["ok"]:
                return Outcome(False, result["detail"])
        if check.receipt and (problem := check.receipt(fixture)):
            return Outcome(False, f"receipt: {problem}")
        return Outcome(True)
    finally:
        await runner.cleanup()


async def resolved_readiness(base: str) -> dict[str, bool]:
    async with aiohttp.ClientSession() as session, session.get(f"{base}/api/settings") as res:
        data = await res.json()
    return {provider["name"]: provider["configured"] for provider in data["providers"]}


async def _assert_readiness() -> None:
    runner, base = await _serve(build_fixture("writable").app)
    try:
        resolved = await resolved_readiness(base)
    finally:
        await runner.cleanup()
    if resolved != EXPECTED_READINESS:
        wrong = [
            f"{name} resolved configured={resolved.get(name)}"
            for name in sorted(set(resolved) | set(EXPECTED_READINESS))
            if resolved.get(name) != EXPECTED_READINESS.get(name)
        ]
        print(f"provider readiness is not the fixture's: {'; '.join(wrong)}", file=sys.stderr)
        raise SystemExit(2)


def _require_node() -> None:
    try:
        version = subprocess.run(
            ["node", "--version"], capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        version = ""
    match = re.match(r"v(\d+)", version.strip())
    if not match or int(match.group(1)) < MINIMUM_NODE:
        print("settings_probe needs Node 22+ (global WebSocket)", file=sys.stderr)
        raise SystemExit(2)


@contextlib.contextmanager
def _chromium(browser: str) -> Iterator[str]:
    with tempfile.TemporaryDirectory(prefix="settings-probe-chromium-") as profile:
        profile_dir = Path(profile)
        process = subprocess.Popen(
            [
                browser,
                "--headless",
                f"--user-data-dir={profile_dir}",
                "--remote-debugging-port=0",
                "--disable-remote-fonts",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-gpu",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 60
            yield _page_socket(_devtools_port(profile_dir, deadline), deadline)
        finally:
            process.terminate()
            process.wait(timeout=30)


async def _run(checks: list[Check], socket_url: str, self_test: bool) -> bool:
    await _assert_readiness()
    all_good = True
    for check in checks:
        clean = await run_check(check, socket_url, sabotaged=False)
        print(f"PASS {check.id}" if clean.ok else f"FAIL {check.id}: {clean.detail}", flush=True)
        all_good &= clean.ok
        if self_test:
            broken = await run_check(check, socket_url, sabotaged=True)
            caught = not broken.ok and not broken.sabotage_error
            detail = "" if caught else f": {broken.detail or 'passed while sabotaged'}"
            print(f"{'CAUGHT' if caught else 'MISSED'} {check.id}{detail}", flush=True)
            all_good &= caught
    return all_good


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", default=None, help="Chromium binary (else $CYRIS_CHROMIUM)")
    parser.add_argument("--only", action="append", default=[], metavar="ID", help="run one check")
    parser.add_argument(
        "--self-test", action="store_true", help="also run every check sabotaged; each must fail"
    )
    args = parser.parse_args()

    known = {check.id for check in CHECKS}
    if unknown := sorted(set(args.only) - known):
        parser.error(f"unknown check: {', '.join(unknown)}")
    checks = [check for check in CHECKS if not args.only or check.id in args.only]

    browser = find_browser(args.browser)
    _require_node()
    with probe_environment(), _chromium(browser) as socket_url:
        good = asyncio.run(_run(checks, socket_url, args.self_test))
    raise SystemExit(0 if good else 1)


if __name__ == "__main__":
    main()
