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

The browser half, which knows no page, is `cdp_probe.py`, shared with the raw
page's probe.

This script must never be collected by pytest: it needs Chromium and Node,
which makes it a reviewer-run gate rather than a test. Importing it is safe,
and `tests/test_css_receipt.py` does so to check its fixtures and registry.
"""

import argparse
import asyncio
import contextlib
import json
import os
import sys
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from types import SimpleNamespace
from unittest import mock

import aiohttp
from aiohttp import web
from cdp_probe import Check, base_prelude, chromium, require_node, run_all, serve
from css_computed import find_browser

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
        "values": {
            "llm_provider.provider": "gemini",
            "llm_provider.model": "",
            "general.digest_schedule": ["08:00", "20:00"],
            "digest.max_featured": 5,
            "notify.discord_webhook_url": STORED_WEBHOOK,
        },
        "sources": _seed_sources(),
        "sources_origin": "sources.yaml",
    }
    if kind == "readonly":
        server = TriageServer(**common)
        return Fixture(server._app, None, server._sources)
    if kind == "writable":
        settings = FakeSettings()
        store = FakeSourceStore(_seed_sources())
        server = TriageServer(settings=settings, source_store=store, **common)
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


# Installed before the page loads: the settings request never answers.
HOLD_SETTINGS = """
const realFetch = window.fetch;
window.fetch = (input, init) =>
  String(input).endsWith("/api/settings") && !(init && init.method)
    ? new Promise(() => {})
    : realFetch(input, init);
"""


# Installed before the page loads: the settings answer reports no provider key.
NO_KEYS = """
const realFetch = window.fetch;
window.fetch = async (input, init) => {
  const res = await realFetch(input, init);
  if (!String(input).endsWith("/api/settings") || (init && init.method)) return res;
  const data = await res.json();
  data.providers.forEach((provider) => { provider.configured = false; });
  return new Response(JSON.stringify(data), {status: res.status, headers: res.headers});
};
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


def reject_get(path: str) -> str:
    """A preload under which the page's GET of `path` fails as a network error would."""
    return f"""
const realFetch = window.fetch;
window.fetch = (input, init) =>
  String(input).endsWith({json.dumps(path)}) && !(init && init.method)
    ? Promise.reject(new TypeError("boom"))
    : realFetch(input, init);
"""


def answer_post(path: str, answer: str) -> str:
    """A preload under which the page's POST to `path` gets `answer`, a JS promise, instead."""
    return f"""
const realFetch = window.fetch;
window.fetch = (input, init) =>
  String(input).endsWith({json.dumps(path)}) && init && init.method === "POST"
    ? {answer}
    : realFetch(input, init);
"""


def also_post(when: str, path: str, body: dict) -> str:
    """A preload that sends an extra POST to `path` whenever the page POSTs to `when`."""
    return f"""
const realFetch = window.fetch;
window.fetch = async (input, init) => {{
  if (String(input).endsWith({json.dumps(when)}) && init && init.method === "POST") {{
    await realFetch({json.dumps(path)}, {{method: "POST",
      headers: {{"Content-Type": "application/json"}}, body: {json.dumps(json.dumps(body))}}});
  }}
  return realFetch(input, init);
}};
"""


def hold_post(path: str) -> str:
    """A preload that counts the page's POSTs to `path` and holds each until `__release()`."""
    return answer_post(
        path,
        """(window.__posts = (window.__posts || 0) + 1, new Promise((resolve) => {
          window.__release = () => resolve(realFetch(input, init));
        }))""",
    )


EDITED_WEBHOOK = "https://discord.com/api/webhooks/7/EDITEDTOKEN"

# Per settings form: the POST its Save sends, a change that makes it dirty, and
# a second change typed while that save is in flight.
IN_FLIGHT = {
    "model": (
        "/api/settings",
        """$('input[value="anthropic"]').click();""",
        """setValue($("#model-input"), "edited-model");""",
    ),
    "digest": (
        "/api/settings/values",
        """setValue($("#max-featured"), "7");""",
        """setValue($("#max-featured"), "8");""",
    ),
    "notifications": (
        "/api/settings/notify",
        f"""setValue($("#discord-webhook"), {json.dumps(NEW_WEBHOOK)});""",
        f"""setValue($("#discord-webhook"), {json.dumps(EDITED_WEBHOOK)});""",
    ),
}


def _save_edit_during(tab: str) -> str:
    """Act: change the form, press Save, then edit and press Save again while it is held."""
    _, change, edit = IN_FLIGHT[tab]
    return f"""
await settingsLoaded();
{change}
saveOf("{tab}").click();
await waitFor(() => window.__release, "the held save");
{edit}
saveOf("{tab}").click();
"""


def _calls(wanted: list[dict]) -> Callable[[Fixture], str | None]:
    """A receipt: the settings store received exactly these writes, in order."""

    def receipt(fixture: Fixture) -> str | None:
        calls = fixture.settings.calls
        return None if calls == wanted else f"settings writes: {calls}"

    return receipt


ARM_RETIRE = """
await openRow("Hacker News");
editorAct("retire").click();
"""

# A browser dialog would block the page, so calling one is recorded and refused.
REFUSE_CONFIRM = """
window.confirm = () => { window.__confirmCalled = true; throw new Error("confirm called"); };
"""

# Swallows a press on an armed Retire, so the second press does nothing.
IGNORE_ARMED_RETIRE = """
document.addEventListener("click", (event) => {
  if (event.target.closest('[data-act="retire"].armed')) event.stopImmediatePropagation();
}, true);
"""


def _keep_only(name: str) -> Callable[[Fixture], None]:
    """A setup: the source table holds `name` alone."""

    def setup(fixture: Fixture) -> None:
        for other in [n for n in fixture.sources if n != name]:
            fixture.sources.pop(other)

    return setup


# Installed before the page loads: every DELETE of a source is answered as retired.
ANSWER_DELETE_OK = """
const realFetch = window.fetch;
const retired = JSON.stringify({ok: true, name: "x", note: "Effective next run."});
window.fetch = (input, init) =>
  String(input).includes("/api/sources/") && init && init.method === "DELETE"
    ? Promise.resolve(new Response(retired,
        {status: 200, headers: {"Content-Type": "application/json"}}))
    : realFetch(input, init);
"""


def _still_listed(name: str) -> Callable[[Fixture], str | None]:
    return lambda fixture: None if name in fixture.sources else f"{name} was retired"


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
        id="site-bar-links-align",
        fixture="readonly",
        path="/settings",
        script="""
            const bottoms = $$(".site-nav a").map((a) => a.getBoundingClientRect().bottom);
            expect(bottoms.length === 2 && bottoms[0] === bottoms[1], `bottoms: ${bottoms}`);
        """,
        # The shape the bar had before: the gate on a wrapper, the link inside it.
        sabotage="""
            const link = $('.site-nav a[href="/settings"]');
            const wrapper = document.createElement("span");
            wrapper.className = "label";
            link.classList.remove("label");
            link.replaceWith(wrapper);
            wrapper.append(link);
        """,
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
        id="model-no-keys",
        fixture="writable",
        path="/settings#model",
        preload=NO_KEYS,
        act="""
            await settingsLoaded();
            setValue($("#model-input"), "some-model");
        """,
        script="""
            const available = $$("label.choice:not(.unavailable)").map((row) => row.textContent);
            expect(available.length === 0, `available: ${available}`);
            const checked = $$("input[name=provider]:checked").map((input) => input.value);
            expect(checked.length === 0, `checked: ${checked}`);
            const hint = $("#model-hint").textContent;
            expect(hint === "Pick a provider whose key is present.", `hint: ${hint}`);
            expect(saveOf("model").disabled, "Save is enabled with no provider to save");
        """,
        sabotage="""saveOf("model").disabled = false;""",
        receipt=_calls([]),
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
            expect(notice.textContent.includes("Effective next run."), notice.textContent);
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
        id="notice-unreachable",
        fixture="writable",
        path="/settings#notifications",
        preload=answer_post("/api/settings/notify", 'Promise.reject(new TypeError("offline"))'),
        act=f"""
            await settingsLoaded();
            setValue($("#discord-webhook"), {json.dumps(NEW_WEBHOOK)});
            saveOf("notifications").click();
            await waitFor(() => visible($("#notify-result")), "the notice");
        """,
        script="""
            const notice = $("#notify-result"), text = notice.textContent;
            expect(notice.classList.contains("err"), `not an error: ${text}`);
            const wanted = "Could not reach cyris (offline). Check the connection and try again.";
            expect(text === wanted, `notice: ${text}`);
            expect(!saveOf("notifications").disabled, "the Save was disabled");
        """,
        sabotage="""$("#notify-result").textContent = "TypeError: offline";""",
        receipt=_calls([]),
    ),
    Check(
        id="notice-unexplained-refusal",
        fixture="writable",
        path="/settings#sources",
        preload=answer_post(
            "/api/sources", 'Promise.resolve(new Response("<h1>Bad gateway</h1>", {status: 502}))'
        ),
        act="""
            await openRow("Hacker News");
            setValue($("#e-tags", editor()), "news");
            editorAct("save").click();
            await waitFor(() => visible($(".notice", editor())), "the notice");
        """,
        script="""
            const notice = editorAct("save").parentElement.querySelector(".notice");
            const text = notice.textContent;
            expect(notice.classList.contains("err"), `not an error: ${text}`);
            const wanted = "cyris answered 502 without saying why. "
              + "Try again; if it keeps failing, check the server log.";
            expect(text === wanted, `notice: ${text}`);
            expect(!editorAct("save").disabled, "Save source was disabled");
        """,
        sabotage="""
            editorAct("save").parentElement.querySelector(".notice").textContent = "HTTP 502";
        """,
        receipt=_stored("Hacker News", tags=["news", "tech"]),
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
    Check(
        id="source-notice-outside-filter",
        fixture="writable",
        path="/settings#sources",
        act="""
            await sourcesLoaded();
            $('[data-filter="rss"]').click();
            await openRow("Hacker News");
            $('[data-type="newsletter"]', editor()).click();
            setValue($("#e-email", editor()), "from:hn@example.com");
            editorAct("save").click();
            await waitFor(() => editor() && visible($(".notice", editor())), "the save");
        """,
        script="""
            expect(rowOf("Hacker News").nextElementSibling === editor(), "not under its row");
            const text = editorAct("save").parentElement.querySelector(".notice").textContent;
            expect(text === "Hacker News saved. Effective next run.", `notice: ${text}`);
            const all = $('[data-filter="all"]').getAttribute("aria-pressed");
            expect(all === "true", "the filter still hides the saved source");
        """,
        # The filter the save left behind: pressing it closes the editor.
        sabotage="""$('[data-filter="rss"]').click();""",
        receipt=_stored("Hacker News", type="newsletter", email_match="from:hn@example.com"),
    ),
    Check(
        id="settings-load-failure",
        fixture="writable",
        path="/settings",
        preload=reject_get("/api/settings"),
        act="""await waitFor(() => $$("form.tab .notice.err").length === 3, "three notices");""",
        script="""
            const tabs = ["model", "digest", "notifications"];
            const wrong = tabs.filter((t) => !noticeOf(t).classList.contains("err")
              || !noticeOf(t).textContent.startsWith("Could not load settings: TypeError: boom"));
            expect(wrong.length === 0, `wrong notices: ${wrong}`);
            const live = tabs.filter((t) => !saveOf(t).disabled);
            expect(live.length === 0, `enabled: ${live}`);
        """,
        sabotage="""saveOf("digest").disabled = false;""",
    ),
    Check(
        id="sources-load-failure",
        fixture="writable",
        path="/settings#sources",
        preload=reject_get("/api/sources"),
        act="""await waitFor(() => visible($("#sources-notice")), "the notice");""",
        script="""
            const notice = $("#sources-notice");
            expect(notice.classList.contains("err"), "not an error");
            expect(notice.textContent.startsWith("Could not load sources:"), notice.textContent);
            expect($("#add-source").disabled, "Add source is enabled");
            expect($$("tr.src-row").length === 0, "rows were listed");
        """,
        sabotage="""$("#add-source").disabled = false;""",
    ),
    Check(
        id="digest-posts-featured-only",
        fixture="writable",
        path="/settings#digest",
        act=SAVE_FEATURED_7,
        script="",
        sabotage_preload=also_post(
            "/api/settings/values", "/api/settings/schedule", {"times": ["08:00", "20:00"]}
        ),
        receipt=_calls([{"digest.max_featured": 7}]),
    ),
    Check(
        id="digest-posts-hours-only",
        fixture="writable",
        path="/settings#digest",
        act="""
            await settingsLoaded();
            setValue($("#morning"), "9");
            saveOf("digest").click();
            await waitFor(() => visible(noticeOf("digest")), "the save");
        """,
        script="",
        sabotage_preload=also_post(
            "/api/settings/schedule", "/api/settings/values", {"values": {"digest.max_featured": 5}}
        ),
        receipt=_calls([{"general.digest_schedule": ["09:00", "20:00"]}]),
    ),
    Check(
        id="digest-partial-failure",
        fixture="writable",
        path="/settings#digest",
        act="""
            await settingsLoaded();
            setValue($("#morning"), "25");
            setValue($("#max-featured"), "7");
            saveOf("digest").click();
            await waitFor(() => visible(noticeOf("digest")), "the save");
        """,
        script="""
            const notice = noticeOf("digest"), lines = notice.textContent.split("\\n");
            expect(notice.classList.contains("err"), `not an error: ${notice.textContent}`);
            expect(lines.length === 2 && lines[0].startsWith("Digest hours not saved: ")
              && lines[0].length > "Digest hours not saved: ".length, `lines: ${lines}`);
            expect(lines[1].startsWith("Featured sections: 7."), `lines: ${lines}`);
            expect(!saveOf("digest").disabled, "the digest Save was disabled");
            expect($("#morning").value === "25", `morning: ${$("#morning").value}`);
            setValue($("#max-featured"), "5");
            expect(navOf("digest").classList.contains("dirty"), "the failed hours look saved");
        """,
        sabotage="""noticeOf("digest").classList.remove("err");""",
        receipt=_calls([{"digest.max_featured": 7}]),
    ),
    *(
        Check(
            id=f"save-held-in-flight-{tab}",
            fixture="writable",
            path=f"/settings#{tab}",
            preload=hold_post(path),
            act=_save_edit_during(tab) + "await sleep(100);",
            script=f"""
                expect(saveOf("{tab}").disabled, "Save came back while the save was in flight");
                expect(window.__posts === 1, `POSTs: ${{window.__posts}}`);
                const notice = noticeOf("model");
                expect("{tab}" !== "model" || (visible(notice)
                  && notice.textContent === "Checking with the provider…"),
                  `the model notice: ${{visible(notice) && notice.textContent}}`);
            """,
            sabotage=f"""saveOf("{tab}").disabled = false;""",
        )
        for tab, (path, _, _) in IN_FLIGHT.items()
    ),
    *(
        Check(
            id=f"edit-during-save-stays-dirty-{tab}",
            fixture="writable",
            path=f"/settings#{tab}",
            preload=hold_post(IN_FLIGHT[tab][0]),
            act=_save_edit_during(tab)
            + f"""
                window.__release();
                await waitFor(() => visible(noticeOf("{tab}"))
                  && !noticeOf("{tab}").textContent.startsWith("Checking"), "the answer");
            """,
            script=f"""
                const value = {json.dumps(field)}, wanted = {json.dumps(typed)};
                expect($(value).value === wanted, `field: ${{$(value).value}}`);
                expect(!saveOf("{tab}").disabled, "the edit made during the save looks saved");
                expect(navOf("{tab}").classList.contains("dirty"), "no dirty mark");
            """,
            sabotage=f"""markClean(saveOf("{tab}").form);""",
            receipt=_last_call(stored),
        )
        for tab, field, typed, stored in (
            (
                "model",
                "#model-input",
                "edited-model",
                {"llm_provider.provider": "anthropic", "llm_provider.model": ""},
            ),
            (
                "notifications",
                "#discord-webhook",
                EDITED_WEBHOOK,
                {"notify.discord_webhook_url": NEW_WEBHOOK},
            ),
        )
    ),
    Check(
        id="editor-save-held-in-flight",
        fixture="writable",
        path="/settings#sources",
        preload=hold_post("/api/sources"),
        act="""
            await openRow("Hacker News");
            setValue($("#e-tags", editor()), "news");
            editorAct("save").click();
            await waitFor(() => window.__release, "the held save");
            setValue($("#e-tags", editor()), "news, edited");
            editorAct("save").click();
            await sleep(100);
        """,
        script="""
            expect(editorAct("save").disabled, "Save source came back during the save");
            expect(window.__posts === 1, `POSTs: ${window.__posts}`);
        """,
        sabotage="""editorAct("save").disabled = false;""",
    ),
    Check(
        id="editor-locked-while-saving",
        fixture="writable",
        path="/settings#sources",
        preload=hold_post("/api/sources"),
        act="""
            await openRow("Hacker News");
            setValue($("#e-tags", editor()), "news");
            editorAct("save").click();
            await waitFor(() => window.__release, "the held save");
        """,
        # Typing, not setValue: an edit the reload would drop must not get in.
        script="""
            const tags = $("#e-tags", editor());
            tags.focus();
            document.execCommand("insertText", false, ", typed");
            expect(tags.value === "news", `typed during the save: ${tags.value}`);
        """,
        sabotage="""editor().inert = false;""",
    ),
    Check(
        id="retire-arms",
        fixture="writable",
        path="/settings#sources",
        act=ARM_RETIRE,
        script="""
            const retire = editorAct("retire");
            expect(retire.textContent === "Confirm retire", `text: ${retire.textContent}`);
            expect(retire.classList.contains("armed"), "not armed");
        """,
        sabotage="""editorAct("retire").classList.remove("armed");""",
        receipt=_still_listed("Hacker News"),
    ),
    Check(
        id="retire-reverts",
        fixture="writable",
        path="/settings#sources",
        act=ARM_RETIRE + "await sleep(3300);",
        script="""
            const retire = editorAct("retire");
            expect(retire.textContent === "Retire", `text: ${retire.textContent}`);
            expect(!retire.classList.contains("armed"), "still armed");
        """,
        sabotage="""editorAct("retire").classList.add("armed");""",
        receipt=_still_listed("Hacker News"),
    ),
    Check(
        id="retire-deletes",
        fixture="writable",
        path="/settings#sources",
        preload=REFUSE_CONFIRM,
        act="""
            await openRow("曼報");
            editorAct("retire").click();
            await sleep(200);
            editorAct("retire").click();
            await waitFor(() => visible($("#sources-notice")), "the toolbar notice");
        """,
        script="""
            expect(!window.__confirmCalled, "window.confirm was called");
            expect(!rowNames().includes("曼報"), `rows: ${rowNames()}`);
            const text = $("#sources-notice").textContent;
            expect(text === "曼報 retired. Effective next run.", `notice: ${text}`);
        """,
        sabotage_preload=IGNORE_ARMED_RETIRE,
        receipt=lambda fixture: "曼報 is still listed" if "曼報" in fixture.sources else None,
    ),
    Check(
        id="retire-last-refused",
        fixture="writable",
        path="/settings#sources",
        setup=_keep_only("Hacker News"),
        act="""
            await openRow("Hacker News");
            editorAct("retire").click();
            await sleep(200);
            editorAct("retire").click();
            const notice = () => editorAct("retire").parentElement.querySelector(".notice");
            await waitFor(() => visible(notice()) || visible($("#sources-notice")), "the retire");
        """,
        script="""
            const notice = editorAct("retire").parentElement.querySelector(".notice");
            expect(notice.classList.contains("err"), `not an error: ${notice.className}`);
            expect(notice.textContent.includes("last source"), `notice: ${notice.textContent}`);
            expect(rowNames().includes("Hacker News"), `rows: ${rowNames()}`);
        """,
        sabotage_preload=ANSWER_DELETE_OK,
        receipt=_still_listed("Hacker News"),
    ),
    Check(
        id="readonly-settings",
        fixture="readonly",
        path="/settings#model",
        act="""
            await settingsLoaded();
            ctx.notices = {};
            for (const tab of ["model", "digest", "notifications"]) {
              location.hash = tab;
              await waitFor(() => same(panels(), [tab]), tab);
              const notice = noticeOf(tab);
              ctx.notices[tab] = visible(notice) && notice.classList.contains("err")
                && notice.textContent.includes("Edit cyris.toml instead.");
            }
            setValue($("#max-featured"), "9");
        """,
        script="""
            const unexplained = Object.keys(ctx.notices).filter((tab) => !ctx.notices[tab]);
            expect(unexplained.length === 0, `no read-only notice: ${unexplained}`);
            const live = ["model", "digest", "notifications"].filter((t) => !saveOf(t).disabled);
            expect(live.length === 0, `enabled: ${live}`);
            expect(!$(".settings-nav a.dirty"), "a category is marked unsaved");
        """,
        sabotage="""saveOf("digest").disabled = false;""",
    ),
    Check(
        id="readonly-sources",
        fixture="readonly",
        path="/settings#sources",
        act="""
            await openRow("Hacker News");
            setValue($("#e-tags", editor()), "x");
        """,
        script="""
            const reason = "No writable source table here — this deployment reads sources.yaml.";
            expect(!$(".settings-nav a.dirty"), "Sources is marked unsaved");
            expect($("#add-source").disabled, "Add source is enabled");
            const toolbar = $("#sources-notice");
            expect(visible(toolbar) && toolbar.textContent === reason, toolbar.textContent);
            expect(editorAct("save").disabled, "Save source is enabled");
            expect(editorAct("retire").disabled, "Retire is enabled");
            const notice = editorAct("save").parentElement.querySelector(".notice");
            expect(visible(notice) && notice.textContent === reason, notice.textContent);
        """,
        sabotage="""editorAct("retire").disabled = false;""",
    ),
    Check(
        id="fits-400",
        fixture="writable",
        path="/settings",
        width=400,
        act="""
            await settingsLoaded();
            const measure = (where) => {
              const nav = $(".settings-nav"), style = getComputedStyle(nav);
              const problems = [overflow(where)];
              if (style.display !== "flex") problems.push(`${where}: nav is ${style.display}`);
              if (nav.getBoundingClientRect().bottom > visiblePanel().getBoundingClientRect().top)
                problems.push(`${where}: the nav is not above the panel`);
              return problems.filter(Boolean);
            };
            ctx.problems = await eachCategory(measure);
            await openRow("Hacker News");
            ctx.problems.push(...measure("the open editor"));
        """,
        script="""
            const problems = [...ctx.problems, overflow("now")].filter(Boolean);
            expect(problems.length === 0, problems.join("; "));
        """,
        sabotage="""
            const wide = document.createElement("div");
            wide.textContent = "wide";
            wide.className = "probe-wide";
            wide.style.width = "2000px";
            visiblePanel().append(wide);
        """,
    ),
    Check(
        id="fits-1440",
        fixture="writable",
        path="/settings",
        act="""
            await settingsLoaded();
            ctx.problems = await eachCategory(layout1440);
        """,
        script="""
            const problems = [...ctx.problems, ...layout1440("now")];
            expect(problems.length === 0, problems.join("; "));
        """,
        sabotage="""$(".settings-nav").style.display = "none";""",
    ),
]

# Only these two may scroll sideways; anything else past the viewport is overflow.
PRELUDE = (
    base_prelude((".table-wrap", ".settings-nav"))
    + """
const setValue = (el, value) => {
  el.value = value;
  el.dispatchEvent(new Event("input", {bubbles: true}));
  el.dispatchEvent(new Event("change", {bubbles: true}));
};
const panels = () => $$(".tab").filter(visible).map((panel) => panel.dataset.tab);
const currentTabs = () =>
  $$(".settings-nav a[aria-current]").map((link) => link.getAttribute("href"));
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
const eachCategory = async (measure) => {
  const problems = [];
  for (const tab of ["model", "digest", "notifications", "sources"]) {
    location.hash = tab;
    await waitFor(() => same(panels(), [tab]), tab);
    problems.push(...measure(tab));
  }
  return problems;
};
const visiblePanel = () => $$(".tab").find(visible);
const layout1440 = (where) => {
  const nav = $(".settings-nav").getBoundingClientRect();
  const panel = visiblePanel().getBoundingClientRect();
  const page = $(".page"), style = getComputedStyle(page);
  const content = page.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
  const problems = [overflow(where)];
  if (nav.width !== 220) problems.push(`${where}: nav is ${nav.width}px wide`);
  if (!(nav.right < panel.left)) problems.push(`${where}: nav is not left of the panel`);
  if (content > 1240) problems.push(`${where}: page content is ${content}px`);
  return problems.filter(Boolean);
};
"""
)


async def resolved_readiness(base: str) -> dict[str, bool]:
    async with aiohttp.ClientSession() as session, session.get(f"{base}/api/settings") as res:
        data = await res.json()
    return {provider["name"]: provider["configured"] for provider in data["providers"]}


async def _assert_readiness() -> None:
    runner, base = await serve(build_fixture("writable").app)
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
    require_node()
    with probe_environment(), chromium(browser) as socket_url:
        good = asyncio.run(
            run_all(checks, socket_url, args.self_test, build_fixture, PRELUDE, _assert_readiness)
        )
    raise SystemExit(0 if good else 1)


if __name__ == "__main__":
    main()
