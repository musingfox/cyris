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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest import mock

import aiohttp
from aiohttp import web
from cdp_probe import (
    Check,
    at_largest_type_scale,
    base_prelude,
    chromium,
    largest_twin,
    require_node,
    restyle,
    run_all,
    serve,
)
from css_computed import find_browser

from cyris.adapters.notify import mask_discord_webhook_url
from cyris.adapters.store.feed_health import FeedHealth
from cyris.config import GRADE_D_KEYS, LLMProviderConfig
from cyris.diagnostics.doctor import Check as DoctorCheck
from cyris.domain.models import SourceConfig, Tier, TrackedTopic
from cyris.entrypoints.triage_server import TriageServer

# Named rather than random so a leak test can look for exactly these strings.
SENTINEL_KEY = "cyris-probe-sentinel-key"
STORED_WEBHOOK = "https://discord.com/api/webhooks/123/abcTOKEN"
OFFLINE_DETAIL = "cyris-probe: answered offline"

# What /api/settings must resolve inside the probe environment. Asserted from the
# server's answer, not from the variables this script set, because a key can
# also arrive from somewhere this script does not control.
EXPECTED_READINESS = {
    "anthropic": True,
    "gemini": True,
    "openai": False,
    "workers_ai": False,
    "ai_gateway": False,
    "none": True,
}


class FakeSettings:
    """Stands in for `D1Settings` and records every write the page makes."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def set(self, values: dict) -> None:
        self.calls.append(dict(values))


class FakeFeedHealth:
    """Stands in for `D1FeedHealth`: Hacker News keeps failing, Simon Willison is fine."""

    def read(self) -> dict[str, FeedHealth]:
        recent = datetime.now(UTC).isoformat()
        return {
            "Hacker News": FeedHealth("Hacker News", 3, "HTTP 503", recent, None, recent),
            "Simon Willison": FeedHealth("Simon Willison", 0, None, None, recent, recent),
        }


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


class FakeTopicStore:
    """Stands in for the D1 `tracked_topics` table; `topics` is the receipt."""

    def __init__(self, topics: dict[str, TrackedTopic]) -> None:
        self.topics = topics

    def list_topics(self) -> list[TrackedTopic]:
        return [self.topics[name] for name in sorted(self.topics)]

    def upsert(self, topic: TrackedTopic) -> None:
        self.topics[topic.name] = topic

    def delete(self, name: str) -> int:
        return 1 if self.topics.pop(name, None) else 0


# The probe values embed with workers_ai's default model, @cf/baai/bge-m3.
PROBE_EMBEDDING_MODEL = "@cf/baai/bge-m3"


def _seed_topics() -> dict[str, TrackedTopic]:
    listed = [
        TrackedTopic(
            name="Anthropic",
            description="The company that makes Claude",
            threshold=0.55,
            model=PROBE_EMBEDDING_MODEL,
        ),
        # Set for a model the probe deployment does not embed with: the run skips it.
        TrackedTopic(
            name="Chips",
            description="Semiconductor makers and export rules",
            threshold=0.7,
            model="gemini-embedding-001",
        ),
    ]
    return {topic.name: topic for topic in listed}


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


_PROBE_VALUES = {
    "general.digest_schedule": ["08:00", "20:00"],
    "general.timezone": "Asia/Taipei",
    "general.digest_window_hours": 24,
    "llm_provider.provider": "gemini",
    "llm_provider.model": "",
    "notify.discord_webhook_url": STORED_WEBHOOK,
    "notify.email_to": "",
    "notify.email_from": "",
    "digest.max_articles_per_digest": 200,
    "digest.max_articles_per_digest_output": 15,
    "digest.max_featured": 5,
    "digest.scoring_snippet_length": 1000,
    "digest.summarize_snippet_length": 1000,
    "digest.filter_snippet_length": 500,
    "digest.output_language": "zh-Hant",
    "digest.style_prompt": "x",
    "digest.type_scale": 1,
    "digest.rank_by_preference": False,
    "routing.score_threshold": 70,
    "routing.summarize_score_threshold": 70,
    "vote_similarity.enabled": False,
    "vote_similarity.provider": "workers_ai",
    "vote_similarity.model": "",
    "vote_similarity.max_seeds": 200,
}
# Every runtime setting set, so no check meets a missing-value marker it did not ask
# for; a key the registry adds without a value here fails at import.
PROBE_VALUES = {key: _PROBE_VALUES[key] for key in GRADE_D_KEYS}


@dataclass
class Fixture:
    """One server and the in-memory stores a check reads its receipt from.

    `values` is the server's own snapshot of the settings home, so a check's
    setup can leave a key unset.
    """

    app: web.Application
    settings: FakeSettings | None
    sources: dict[str, SourceConfig]
    values: dict
    topics: dict[str, TrackedTopic] = field(default_factory=dict)


def build_fixture(kind: str) -> Fixture:
    """A `readonly` deployment (no settings store, no source table) or a `writable` one.

    `writable-largest` is `writable` with the page served at the largest type size;
    `writable-health` is `writable` with one feed failing; `writable-topics` is
    `writable` with two tracked topics, one set for another embedding model.
    Every other writable fixture tracks no topic, so `rowNames` reads sources alone.
    """
    common = {"values": PROBE_VALUES, "sources": _seed_sources()}
    if kind == "readonly":
        server = TriageServer(**common)
        return Fixture(server._app, None, server._sources, server._values)
    if kind in ("writable", "writable-largest", "writable-health", "writable-topics"):
        settings = FakeSettings()
        store = FakeSourceStore(_seed_sources())
        topics = FakeTopicStore(_seed_topics() if kind == "writable-topics" else {})
        # Health only where a check asks for it: its line sits in the name cell
        # that `rowNames` reads.
        health = FakeFeedHealth() if kind == "writable-health" else None
        server = TriageServer(
            settings=settings, source_store=store, feed_health=health, topic_store=topics, **common
        )
        if kind == "writable-largest":
            server._settings_page = at_largest_type_scale(server._settings_page)
        return Fixture(server._app, settings, store.sources, server._values, topics.topics)
    raise ValueError(f"unknown fixture {kind!r}")


@contextlib.contextmanager
def probe_environment() -> Iterator[SimpleNamespace]:
    """Pin the provider keys, and answer the LLM, embedder and Discord probes offline.

    Runs from an empty directory so no `.env` there can bind a key. Yields the
    patched probes so a caller can see they were the ones answering.
    """
    absent = {
        LLMProviderConfig(provider="openai", model="").api_key_env_var,
        LLMProviderConfig(provider="workers_ai", model="").api_key_env_var,
        # workers_ai falls back to these, so removing its own variable is not enough.
        "CLOUDFLARE_EMBEDDING_API_TOKEN",
        "CLOUDFLARE_ACCOUNT_ID",
    }
    offline = DoctorCheck(name="probe", status="ok", detail=OFFLINE_DETAIL)
    llm = mock.AsyncMock(return_value=offline)
    embedder = mock.AsyncMock(return_value=offline)
    discord = mock.AsyncMock(return_value=offline)
    present = {"GEMINI_API_KEY": SENTINEL_KEY, "ANTHROPIC_API_KEY": SENTINEL_KEY}
    with (
        tempfile.TemporaryDirectory(prefix="settings-probe-") as home,
        contextlib.chdir(home),
        mock.patch.dict(os.environ, present),
        mock.patch("cyris.diagnostics.doctor.probe_llm", llm),
        mock.patch("cyris.entrypoints.triage_server.probe_embedder", embedder),
        mock.patch("cyris.entrypoints.triage_server.probe_discord", discord),
    ):
        for name in absent:
            os.environ.pop(name, None)
        yield SimpleNamespace(llm=llm, embedder=embedder, discord=discord)


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


def rewrite_post(path: str, mutation: str) -> str:
    """A preload that changes the body of the page's POST to `path` before it leaves."""
    return f"""
const realFetch = window.fetch;
window.fetch = (input, init) => {{
  if (String(input).endsWith({json.dumps(path)}) && init && init.method === "POST") {{
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


def _topic_stored(name: str, **wanted) -> Callable[[Fixture], str | None]:
    """A receipt: the topic store holds `name` with exactly these field values."""

    def receipt(fixture: Fixture) -> str | None:
        topic = fixture.topics.get(name)
        if topic is None:
            return f"{name} is not stored"
        wrong = {k: getattr(topic, k) for k, v in wanted.items() if getattr(topic, k) != v}
        return f"{name} stored {wrong}" if wrong else None

    return receipt


def _topic_gone(name: str) -> Callable[[Fixture], str | None]:
    """A receipt: the topic store no longer holds `name`."""
    return lambda fixture: f"{name} is still stored" if name in fixture.topics else None


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
await waitFor(() => settled(noticeOf("digest")) && saveOf("digest").disabled, "the save");
"""

# Pick a type size in the Digest form and save it; `value` is the option's.
SAVE_TYPE_SCALE = """
await settingsLoaded();
setValue($("#type-scale"), "{value}");
saveOf("digest").click();
await waitFor(() => settled(noticeOf("digest")) && saveOf("digest").disabled, "the save");
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


def answer_get(path: str, answer: str) -> str:
    """A preload under which the page's GET of `path` gets `answer`, a JS promise, instead."""
    return f"""
const realFetch = window.fetch;
window.fetch = (input, init) =>
  String(input).endsWith({json.dumps(path)}) && [undefined, "GET"].includes(init && init.method)
    ? {answer}
    : realFetch(input, init);
"""


# Installed before the page loads: the sources request never answers.
HOLD_SOURCES = answer_get("/api/sources", "new Promise(() => {})")


# What the app Worker answers a request whose session cookie has expired, and
# what the page must then tell the reader.
SESSION_EXPIRED = (
    'Promise.resolve(new Response(JSON.stringify({error: "unauthorized"}), '
    '{status: 401, headers: {"Content-Type": "application/json"}}))'
)
SIGN_IN_AGAIN = "Your session has expired. Reload the page to sign in again."

# What the app Worker's /api/vote answers a signed-in reader; this server has no such route.
SIGNED_IN = (
    "Promise.resolve(new Response(JSON.stringify({authorized: true}), "
    '{headers: {"Content-Type": "application/json"}}))'
)


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


def hold_delete() -> str:
    """A preload that holds the page's DELETE of a source until `__release()`."""
    return """
const realFetch = window.fetch;
window.fetch = (input, init) =>
  String(input).includes("/api/sources/") && init && init.method === "DELETE"
    ? new Promise((resolve) => { window.__release = () => resolve(realFetch(input, init)); })
    : realFetch(input, init);
"""


def refuse_post(path: str, error: str, field: str) -> str:
    """A preload under which the page's POST to `path` is refused for `field`."""
    body = json.dumps(json.dumps({"ok": False, "error": error, "field": field}))
    return answer_post(
        path,
        f"""Promise.resolve(new Response({body},
          {{status: 400, headers: {{"Content-Type": "application/json"}}}}))""",
    )


SAVE_BAD_TIMEZONE = """
await settingsLoaded();
setValue($("#timezone"), "Mars/Base");
saveOf("digest").click();
await waitFor(() => settled(noticeOf("digest")), "the refusal");
"""
TIMEZONE_REFUSED = "Timezone: 'Mars/Base' is not a timezone this server knows"

MODEL_REFUSED = "typo refused: 404\nCheck the model name, then save again."
EMBEDDER_REFUSED = "CLOUDFLARE_EMBEDDING_API_TOKEN is not set\nSet it, or save with it off."

# The color --warn resolves to, for comparing a border against.
WARN = """
const warnOf = () => {
  const swatch = document.createElement("span");
  swatch.style.color = "var(--warn)";
  document.body.append(swatch);
  const color = getComputedStyle(swatch).color;
  swatch.remove();
  return color;
};
"""

# Per save: where it starts, the POST it sends, the change that sends it, the
# notice it reports in, and what that notice says while the POST is out.
BUSY = {
    "digest": (
        "/settings#digest",
        "/api/settings/values",
        """setValue($("#max-featured"), "7");""",
        "digest",
        "Saving…",
    ),
    "pipeline": (
        "/settings#pipeline",
        "/api/settings/values",
        """setValue($("#window-hours"), "12");""",
        "pipeline",
        "Saving…",
    ),
    "model-vote-off": (
        "/settings#model",
        "/api/settings/vote-similarity",
        """setValue($("#vote-seeds"), "50");""",
        "model",
        "Saving…",
    ),
    "discord": (
        "/settings#notifications",
        "/api/settings/notify",
        f"""setValue($("#discord-webhook"), {json.dumps(NEW_WEBHOOK)});""",
        "notifications",
        "Checking with Discord…",
    ),
    "email": (
        "/settings#notifications",
        "/api/settings/email",
        """setValue($("#email-to"), "me@example.org");
        setValue($("#email-from"), "d@example.org");""",
        "notifications",
        "Sending a test message…",
    ),
}

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

# A sabotage: focus() stops working, so nothing the page does can move focus.
NO_FOCUS = """HTMLElement.prototype.focus = function () {};"""

# A sabotage: the page hears no key, as if it listened for none.
IGNORE_KEYS = """
document.addEventListener("keydown", (event) => event.stopImmediatePropagation(), true);
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


# Installed before the page loads: every POST body the page sends, in order. A
# block, so a sabotage preload can declare its own `realFetch` beside it.
RECORD_POSTS = """
{
  const realFetch = window.fetch;
  window.__sent = [];
  window.fetch = (input, init) => {
    if (init && init.method === "POST") window.__sent.push([String(input), JSON.parse(init.body)]);
    return realFetch(input, init);
  };
}
"""


def _still_listed(name: str) -> Callable[[Fixture], str | None]:
    return lambda fixture: None if name in fixture.sources else f"{name} was retired"


def _unset(*keys: str) -> Callable[[Fixture], None]:
    """A setup: the settings home holds every key but `keys`; none at all when empty."""

    def setup(fixture: Fixture) -> None:
        for key in keys or list(fixture.values):
            fixture.values.pop(key)

    return setup


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
        preload=answer_get("/api/vote", SIGNED_IN),
        act="""await waitFor(() => $$(".site-nav a").filter(visible).length === 2, "Archive");""",
        script="""
            const shown = $$(".site-nav a").filter(visible);
            const bottoms = shown.map((a) => a.getBoundingClientRect().bottom);
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
        id="unavailable-state-opaque",
        fixture="writable",
        path="/settings#model",
        act="await providersLoaded();",
        script="""
            let opacity = 1;
            for (let el = $(".label", choice("openai")); el; el = el.parentElement) {
                opacity *= Number(getComputedStyle(el).opacity);
            }
            expect(opacity === 1, `the missing-key state shows at opacity ${opacity}`);
        """,
        sabotage="""choice("openai").style.opacity = "0.5";""",
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
            const rows = $$("#providers label.choice:not(.unavailable)");
            const available = rows.map((row) => row.textContent);
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
            const tabs = ["model", "digest", "pipeline", "notifications"];
            const live = tabs.filter((t) => !saveOf(t).disabled);
            expect(live.length === 0, `enabled before settings loaded: ${live}`);
        """,
        sabotage="""saveOf("digest").disabled = false;""",
    ),
    Check(
        id="settings-loading-shown",
        fixture="writable",
        path="/settings",
        preload=HOLD_SETTINGS,
        act="await sleep(300);",
        script="""
            const shown = $$("form.tab [data-loading]").filter(visible).map((el) => el.textContent);
            expect(same(shown, ["Loading settings…"]), `shown: ${shown}`);
            const silent = $$("form.tab").filter((form) => !$("[data-loading]", form))
              .map((form) => form.dataset.tab);
            expect(silent.length === 0, `no loading line: ${silent}`);
        """,
        sabotage="""$$("form.tab [data-loading]").forEach((el) => el.remove());""",
    ),
    Check(
        id="settings-loading-clears",
        fixture="writable",
        path="/settings",
        act="await settingsLoaded();",
        script="""
            const left = $$("form.tab [data-loading]").map((el) => el.closest("form").dataset.tab);
            expect(left.length === 0, `still loading: ${left}`);
        """,
        sabotage="""
            const line = document.createElement("p");
            line.setAttribute("data-loading", "");
            $("#model-form .tab-head").append(line);
        """,
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
            const heads = $$('[data-tab="sources"] table.src th').map((th) => th.textContent);
            const wanted = ["Name", "Type", "Tier", "Feed or sender", "Tags"];
            expect(same(heads, wanted), `headers: ${heads}`);
            expect(rowNames().length === 4, `rows: ${rowNames()}`);
            expect(rowNames().includes("<b>x</b>"), `names: ${rowNames()}`);
            expect(!$("table.src b"), "a source name was parsed as markup");
        """,
        sabotage="""$("tr.src-row td").innerHTML = "<b>x</b>";""",
    ),
    Check(
        id="sources-feed-health",
        fixture="writable-health",
        path="/settings#sources",
        act="await sourcesLoaded();",
        script="""
            const line = $(".feed-problem", rowOf("Hacker News"));
            const text = line && line.textContent;
            expect(text === "3 failures in a row · HTTP 503", `line: ${text}`);
            const probe = document.createElement("span");
            probe.style.color = "var(--warn)";
            document.body.append(probe);
            const warn = getComputedStyle(probe).color;
            probe.remove();
            const colour = getComputedStyle(line).color;
            expect(colour === warn, `colour: ${colour}, --warn: ${warn}`);
            expect(!$(".feed-problem", rowOf("Simon Willison")), "a healthy feed shows a line");
            expect(!$(".feed-problem", rowOf("曼報")), "a newsletter shows a line");
        """,
        sabotage="""$(".feed-problem").remove();""",
    ),
    Check(
        id="sources-tier-plain-pill",
        fixture="writable",
        path="/settings#sources",
        act="await sourcesLoaded();",
        script="""
            const pills = $$("table.src .pill");
            const tiers = pills.map((pill) => pill.textContent);
            expect(tiers.includes("summarize"), `tiers: ${tiers}`);
            const variants = pills.filter((pill) => pill.className !== "pill");
            expect(!variants.length, `a tier took a variant: ${variants.map((p) => p.className)}`);
        """,
        sabotage="""$("table.src .pill").classList.add("score");""",
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
        id="sources-empty-all",
        fixture="writable",
        path="/settings#sources",
        setup=lambda fixture: fixture.sources.clear(),
        act="""
            await sourcesListed();
        """,
        script="""
            const rows = $$("#src-body tr").map((row) => row.textContent.trim());
            const copy = "No sources yet. The next run stops until one is added.";
            expect(same(rows, [copy]), `rows: ${rows}`);
        """,
        sabotage="""$("#src-body td").textContent = "No sources configured.";""",
    ),
    Check(
        id="sources-loading-shown",
        fixture="writable",
        path="/settings#sources",
        preload=HOLD_SOURCES,
        act="await sleep(300);",
        script="""
            const rows = $$("#src-body tr").filter(visible).map((row) => row.textContent.trim());
            expect(same(rows, ["Loading sources…"]), `rows: ${rows}`);
        """,
        sabotage="""$("#src-body").replaceChildren();""",
    ),
    Check(
        id="sources-empty-warn-dot",
        fixture="writable",
        path="/settings#sources",
        setup=lambda fixture: fixture.sources.clear(),
        act="await sourcesListed();",
        script="""
            expect(navOf("sources").classList.contains("missing"), "Sources has no missing mark");
            const dot = getComputedStyle($(".missing-dot", navOf("sources"))).visibility;
            expect(dot === "visible", `the dot is ${dot}`);
        """,
        sabotage="""navOf("sources").classList.remove("missing");""",
    ),
    Check(
        id="sources-warn-dot-clears-on-save",
        fixture="writable",
        path="/settings#sources",
        setup=lambda fixture: fixture.sources.clear(),
        act="""
            await sourcesListed();
            ctx.marked = navOf("sources").classList.contains("missing");
            $("#add-source").click();
            await waitFor(editor, "the editor");
            setValue($("#e-name", editor()), "First Feed");
            setValue($("#e-url", editor()), "https://example.test/first.xml");
            editorAct("save").click();
            await waitFor(() => rowOf("First Feed"), "the saved row");
        """,
        script="""
            expect(ctx.marked, "Sources was not marked before the save");
            expect(!navOf("sources").classList.contains("missing"), "Sources is still marked");
        """,
        sabotage="""navOf("sources").classList.add("missing");""",
        receipt=_stored("First Feed", url="https://example.test/first.xml"),
    ),
    Check(
        id="sources-warn-dot-absent-with-sources",
        fixture="writable",
        path="/settings#sources",
        act="await sourcesLoaded();",
        script="""
            expect(!navOf("sources").classList.contains("missing"), "Sources is marked");
        """,
        sabotage="""navOf("sources").classList.add("missing");""",
    ),
    Check(
        id="result-notices-are-live",
        fixture="writable",
        path="/settings#sources",
        act="""await openRow("Hacker News");""",
        script="""
            const ids = ["model-result", "digest-result", "pipeline-result", "notify-result",
              "sources-notice"];
            const notices = [...ids.map((id) => $(`#${id}`)), ...$$(".notice", editor())];
            const silent = notices.filter((n) => n.getAttribute("role") !== "status")
              .map((n) => n.id || "an editor notice");
            expect(notices.length === ids.length + 2, `notices: ${notices.length}`);
            expect(silent.length === 0, `not live: ${silent}`);
        """,
        sabotage="""$("#digest-result").removeAttribute("role");""",
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
    # A row opens from the keyboard, and focus follows into the editor's first field.
    *(
        Check(
            id=f"editor-opens-on-{name}",
            fixture="writable",
            path="/settings#sources",
            focused=True,
            act="""
                await sourcesLoaded();
                rowOf("Hacker News").focus();
            """,
            gestures=({"key": key},),
            script="""
                await waitFor(editor, "the editor");
                expect(rowOf("Hacker News").nextElementSibling === editor(), "not under its row");
                const now = document.activeElement;
                const at = now && (now.id || now.tagName);
                expect(now === $("#e-name", editor()), `focus is on ${at}`);
            """,
            sabotage=sabotage,
        )
        for name, key, sabotage in (
            ("enter", "Enter", IGNORE_KEYS),
            ("space", " ", NO_FOCUS),
        )
    ),
    # Closing the editor from the keyboard puts focus back where it was opened from.
    *(
        Check(
            id=check_id,
            fixture="writable",
            path="/settings#sources",
            focused=True,
            act=act,
            gestures=({"key": "Enter"},),
            script=f"""
                await waitFor(() => !editor(), "the editor to close");
                const now = document.activeElement;
                const at = now && (now.dataset.name || now.id || now.tagName);
                expect(now === {wanted}, `focus is on ${{at}}`);
            """,
            sabotage=NO_FOCUS,
        )
        for check_id, act, wanted in (
            (
                "editor-cancel-focuses-row",
                """
                await openRow("Hacker News");
                editorAct("cancel").focus();
                """,
                'rowOf("Hacker News")',
            ),
            (
                "editor-row-close-focuses-row",
                """
                await openRow("Hacker News");
                rowOf("Hacker News").focus();
                """,
                'rowOf("Hacker News")',
            ),
            (
                "editor-new-cancel-focuses-add",
                """
                await sourcesLoaded();
                $("#add-source").click();
                await waitFor(editor, "the editor");
                editorAct("cancel").focus();
                """,
                '$("#add-source")',
            ),
        )
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
            await waitFor(() => editor() && settled($(".notice", editor())), "the save");
        """,
        script="",
        sabotage_preload=rewrite_post(
            "/api/sources", 'body.url = "https://hnrss.org/frontpage?points=200";'
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
            await waitFor(() => editor() && settled($(".notice", editor())), "the save");
        """,
        script="",
        sabotage_preload=rewrite_post("/api/sources", 'body.tags = ["ai", "", "tools"];'),
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
            expect(notice.textContent.includes("Features cards: 7."), notice.textContent);
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
            await waitFor(() => settled(noticeOf("digest")), "the notice");
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
        id="hours-empty-refused-beside-field",
        fixture="writable",
        path="/settings#digest",
        act="""
            await settingsLoaded();
            setValue($("#evening"), "");
            saveOf("digest").click();
            await waitFor(() => settled(noticeOf("digest")), "the notice");
        """,
        script="""
            const field = $("#hours-error"), text = field.textContent;
            expect(visible(field) && text === "Evening needs a whole hour from 0 to 23.",
              `beside the hours: ${visible(field) && text}`);
            expect($("#evening").classList.contains("invalid"), "Evening is not marked");
            expect(!$("#morning").classList.contains("invalid"), "Morning is marked");
            const notice = noticeOf("digest");
            expect(notice.classList.contains("err")
              && notice.textContent.startsWith("Digest hours not saved: Evening needs"),
              `notice: ${notice.textContent}`);
            expect(navOf("digest").classList.contains("dirty"), "Digest lost its dirty mark");
        """,
        sabotage="""$("#hours-error").hidden = true;""",
        receipt=_calls([]),
    ),
    Check(
        id="hours-empty-refused-on-first-boot",
        fixture="writable",
        path="/settings#digest",
        setup=_unset("general.digest_schedule"),
        act="""
            await settingsLoaded();
            setValue($("#evening"), "20");
            saveOf("digest").click();
            await waitFor(() => settled(noticeOf("digest")), "the notice");
        """,
        script="""
            const field = $("#hours-error"), text = field.textContent;
            expect(visible(field) && text === "Morning needs a whole hour from 0 to 23.",
              `beside the hours: ${visible(field) && text}`);
            expect($("#morning").classList.contains("invalid"), "Morning is not marked");
        """,
        sabotage="""$("#morning").classList.remove("invalid");""",
        receipt=_calls([]),
    ),
    Check(
        id="hours-error-clears-on-edit",
        fixture="writable",
        path="/settings#digest",
        act="""
            await settingsLoaded();
            setValue($("#evening"), "");
            saveOf("digest").click();
            await waitFor(() => visible($("#hours-error")), "the field notice");
            setValue($("#evening"), "21");
        """,
        script="""
            expect(!visible($("#hours-error")), "the field notice is still shown");
            expect(!$("#evening").classList.contains("invalid"), "Evening is still marked");
        """,
        sabotage="""$("#hours-error").hidden = false;""",
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
            await waitFor(() => settled($("#notify-result")), "the notice");
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
        id="notify-email-part-alone",
        fixture="writable",
        path="/settings#notifications",
        preload=answer_post(
            "/api/settings/email",
            "Promise.resolve(Response.json({ok: true, "
            '"notify.email_to": "me@example.org", "notify.email_from": "d@example.org", '
            'detail: "A test message was delivered to me@example.org.", '
            'note: "Saved. The next digest run mails this address."}))',
        ),
        act="""
            await settingsLoaded();
            setValue($("#email-to"), "me@example.org");
            setValue($("#email-from"), "d@example.org");
            saveOf("notifications").click();
            await waitFor(() => settled($("#notify-result"))
              && !$("#notify-result").textContent.startsWith("Sending"), "the notice");
        """,
        script="""
            const notice = $("#notify-result"), text = notice.textContent;
            expect(!notice.classList.contains("err"), `an error: ${text}`);
            expect(text.includes("delivered to me@example.org"), `notice: ${text}`);
            expect(saveOf("notifications").disabled, "the Save is still live");
        """,
        sabotage="""$("#notify-result").classList.add("err");""",
        # Only the mail part changed, so the webhook route is never asked to store.
        receipt=_calls([]),
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
            await waitFor(() => settled($("#notify-result")), "the notice");
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
            await waitFor(() => settled($(".notice", editor())), "the notice");
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
            await waitFor(() => editor() && settled($(".notice", editor())), "the save");
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
            await waitFor(() => editor() && settled($(".notice", editor())), "the save");
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
        act="""
            await waitFor(() => $$("form.tab .actions-line .notice.err").length === 4, "4 notices");
        """,
        script="""
            const tabs = ["model", "digest", "pipeline", "notifications"];
            const wrong = tabs.filter((t) => !noticeOf(t).classList.contains("err")
              || !noticeOf(t).textContent.startsWith("Could not load settings: TypeError: boom"));
            expect(wrong.length === 0, `wrong notices: ${wrong}`);
            const live = tabs.filter((t) => !saveOf(t).disabled);
            expect(live.length === 0, `enabled: ${live}`);
            expect(!$("form.tab [data-loading]"), "a loading line is still shown");
        """,
        sabotage="""saveOf("digest").disabled = false;""",
    ),
    Check(
        id="settings-session-expired",
        fixture="writable",
        path="/settings",
        preload=answer_get("/api/settings", SESSION_EXPIRED),
        act="""
            await waitFor(() => $$("form.tab .actions-line .notice.err").length === 4, "4 notices");
        """,
        script=f"""
            const tabs = ["model", "digest", "pipeline", "notifications"];
            const wrong = tabs.filter((t) => !noticeOf(t).classList.contains("err")
              || noticeOf(t).textContent !== {json.dumps(SIGN_IN_AGAIN)});
            expect(wrong.length === 0, `wrong notices: ${{wrong}}`);
            const live = tabs.filter((t) => !saveOf(t).disabled);
            expect(live.length === 0, `enabled: ${{live}}`);
        """,
        sabotage="""noticeOf("digest").textContent = "Could not load settings: TypeError: x";""",
    ),
    Check(
        id="sources-session-expired",
        fixture="writable",
        path="/settings#sources",
        preload=answer_get("/api/sources", SESSION_EXPIRED),
        act="""await waitFor(() => visible($("#sources-notice")), "the notice");""",
        script=f"""
            const notice = $("#sources-notice");
            expect(notice.classList.contains("err"), "not an error");
            expect(notice.textContent === {json.dumps(SIGN_IN_AGAIN)}, notice.textContent);
            expect($("#add-source").disabled, "Add source is enabled");
        """,
        sabotage="""$("#sources-notice").textContent = "Could not load sources: TypeError: x";""",
    ),
    Check(
        id="save-session-expired",
        fixture="writable",
        path="/settings#sources",
        preload=answer_post("/api/sources", SESSION_EXPIRED),
        act="""
            await openRow("Hacker News");
            setValue($("#e-tags", editor()), "news");
            editorAct("save").click();
            await waitFor(() => settled($(".notice", editor())), "the notice");
        """,
        script=f"""
            const notice = editorAct("save").parentElement.querySelector(".notice");
            const text = notice.textContent;
            expect(notice.classList.contains("err"), `not an error: ${{text}}`);
            expect(text === {json.dumps(SIGN_IN_AGAIN)}, `notice: ${{text}}`);
        """,
        sabotage="""
            editorAct("save").parentElement.querySelector(".notice").textContent = "unauthorized";
        """,
        receipt=_stored("Hacker News", tags=["news", "tech"]),
    ),
    Check(
        id="settings-hides-the-archive-off-the-worker",
        fixture="readonly",
        path="/settings",
        act="await settingsLoaded();",
        script="""
            const archive = $$(".site-nav a").find((a) => a.textContent === "Archive");
            expect(archive && !visible(archive), "Archive shows with no archive to link");
            expect(!$(".brand").hasAttribute("href"), `brand links ${$(".brand").href}`);
            expect(visible($(".brand")), "the brand is gone");
        """,
        sabotage="""$$(".site-nav a").forEach((a) => { a.hidden = false; });""",
    ),
    Check(
        id="settings-links-the-archive-on-the-worker",
        fixture="readonly",
        path="/settings",
        preload=answer_get("/api/vote", SIGNED_IN),
        act="""
            const archive = () => $$(".site-nav a").find((a) => a.textContent === "Archive");
            await waitFor(() => visible(archive()), "Archive");
        """,
        script="""
            const href = $(".brand").getAttribute("href");
            expect(href === "index.html", `brand links ${href}`);
            expect(!visible($(".signin-link")), "Sign in shows");
        """,
        sabotage="""$(".brand").removeAttribute("href");""",
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
            expect(!$("#src-body [data-loading]"), "the loading row is still shown");
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
            await waitFor(() => settled(noticeOf("digest")), "the save");
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
            await waitFor(() => settled(noticeOf("digest")), "the save");
        """,
        script="""
            const notice = noticeOf("digest"), lines = notice.textContent.split("\\n");
            expect(notice.classList.contains("err"), `not an error: ${notice.textContent}`);
            expect(lines.length === 2 && lines[0].startsWith("Digest hours not saved: ")
              && lines[0].length > "Digest hours not saved: ".length, `lines: ${lines}`);
            expect(lines[1].startsWith("Features cards: 7."), `lines: ${lines}`);
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
                await waitFor(() => settled(noticeOf("{tab}"))
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
            await waitFor(() => settled(notice()) || visible($("#sources-notice")), "the retire");
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
    # A pressed button is disabled while its write is out, which drops focus; a
    # keyboard reader gets it back where the result left them.
    Check(
        id="source-save-failure-focuses-save",
        fixture="writable",
        path="/settings#sources",
        focused=True,
        act="""
            await openRow("Hacker News");
            setValue($("#e-url", editor()), "");
            editorAct("save").focus();
        """,
        gestures=({"key": "Enter"},),
        script="""
            const notice = editorAct("save").parentElement.querySelector(".notice");
            await waitFor(() => settled(notice), "the refusal");
            const now = document.activeElement;
            const at = now && (now.dataset.act || now.id || now.tagName);
            expect(now === editorAct("save"), `focus is on ${at}`);
        """,
        sabotage=NO_FOCUS,
        receipt=_stored("Hacker News", url="https://hnrss.org/frontpage?points=200"),
    ),
    Check(
        id="retire-failure-focuses-retire",
        fixture="writable",
        path="/settings#sources",
        setup=_keep_only("Hacker News"),
        focused=True,
        act="""
            await openRow("Hacker News");
            editorAct("retire").focus();
        """,
        gestures=({"key": "Enter"}, {"key": "Enter"}),
        script="""
            const notice = editorAct("retire").parentElement.querySelector(".notice");
            await waitFor(() => settled(notice), "the refusal");
            const now = document.activeElement;
            const at = now && (now.dataset.act || now.id || now.tagName);
            expect(now === editorAct("retire"), `focus is on ${at}`);
        """,
        sabotage=NO_FOCUS,
        receipt=_still_listed("Hacker News"),
    ),
    Check(
        id="retire-focuses-add",
        fixture="writable",
        path="/settings#sources",
        focused=True,
        act="""
            await openRow("曼報");
            editorAct("retire").focus();
        """,
        gestures=({"key": "Enter"}, {"key": "Enter"}),
        script="""
            await waitFor(() => visible($("#sources-notice")), "the toolbar notice");
            const now = document.activeElement;
            expect(now === $("#add-source"), `focus is on ${now && (now.id || now.tagName)}`);
        """,
        sabotage=NO_FOCUS,
        receipt=lambda fixture: "曼報 is still listed" if "曼報" in fixture.sources else None,
    ),
    Check(
        id="missing-marked",
        fixture="writable",
        path="/settings#digest",
        setup=_unset(),
        act="""
            await providersLoaded();
            // The border colour transitions; read it once that has settled.
            await sleep(400);
            const swatch = document.createElement("i");
            swatch.style.color = "var(--warn)";
            document.body.append(swatch);
            ctx.warn = getComputedStyle(swatch).color;
            swatch.remove();
        """,
        script="""
            const field = $("#timezone");
            expect(field.getAttribute("aria-invalid") === "true", "the field is not marked");
            expect(field.value === "" && field.placeholder === "Not set",
              `field: ${field.value} / ${field.placeholder}`);
            const border = getComputedStyle(field).borderTopColor;
            expect(border === ctx.warn, `border: ${border}, warn: ${ctx.warn}`);
            const notice = $("#digest-missing");
            expect(visible(notice) && notice.classList.contains("err"), "no category notice");
            expect(notice.textContent.startsWith("Not set yet: ")
              && notice.textContent.includes("Timezone"), notice.textContent);
            expect(navOf("digest").classList.contains("missing"), "Digest has no missing mark");
            expect($('.settings-nav a[data-tab="pipeline"].missing'), "Pipeline has no mark");
            const dot = getComputedStyle($(".missing-dot", navOf("digest"))).visibility;
            expect(dot === "visible", `the dot is ${dot}`);
            const checked = $$("input[name=provider]:checked").map((input) => input.value);
            expect(checked.length === 0, `checked: ${checked}`);
        """,
        sabotage="""$("#timezone").removeAttribute("aria-invalid");""",
    ),
    Check(
        id="missing-clears-on-save",
        fixture="writable",
        path="/settings#digest",
        setup=_unset("general.timezone"),
        act="""
            await providersLoaded();
            ctx.marked = navOf("digest").classList.contains("missing");
            setValue($("#timezone"), "Europe/Berlin");
            saveOf("digest").click();
            await waitFor(() => settled(noticeOf("digest")) && saveOf("digest").disabled,
              "the save");
        """,
        script="""
            expect(ctx.marked, "Digest was not marked before the save");
            expect(!visible($("#digest-missing")), "the category notice is still shown");
            expect(!navOf("digest").classList.contains("missing"), "Digest is still marked");
            const field = $("#timezone");
            expect(!field.hasAttribute("aria-invalid"), "the field is still marked");
            expect(field.placeholder === "UTC", `placeholder: ${field.placeholder}`);
        """,
        sabotage="""navOf("digest").classList.add("missing");""",
        receipt=_calls([{"general.timezone": "Europe/Berlin"}]),
    ),
    Check(
        id="missing-readonly",
        fixture="readonly",
        path="/settings#digest",
        setup=_unset("digest.style_prompt"),
        act="""await waitFor(() => visible($("#digest-missing")), "the category notice");""",
        script="""
            const text = $("#digest-missing").textContent;
            expect(text.startsWith("Missing from cyris.toml: Style."), text);
        """,
        sabotage="""$("#digest-missing").textContent = "Not set yet: Style.";""",
    ),
    Check(
        id="missing-none-when-set",
        fixture="writable",
        path="/settings",
        act="await settingsLoaded();",
        script="""
            const marked = $$(".settings-nav a.missing").map((a) => a.dataset.tab);
            expect(marked.length === 0, `marked: ${marked}`);
            const shown = $$('[id$="-missing"]').filter(visible).map((n) => n.id);
            expect(shown.length === 0, `notices: ${shown}`);
        """,
        sabotage="""navOf("model").classList.add("missing");""",
    ),
    Check(
        id="missing-focus-visible",
        fixture="writable",
        path="/settings#digest",
        setup=_unset("general.timezone"),
        focused=True,
        act="""
            await providersLoaded();
            $("#timezone").focus();
            // The border colour transitions; read it once that has settled.
            await sleep(400);
            const swatch = document.createElement("i");
            swatch.style.color = "var(--accent)";
            document.body.append(swatch);
            ctx.accent = getComputedStyle(swatch).color;
            swatch.remove();
        """,
        script="""
            const field = $("#timezone");
            expect(document.activeElement === field, "the field is not focused");
            expect(field.getAttribute("aria-invalid") === "true", "the field is not marked");
            const border = getComputedStyle(field).borderTopColor;
            expect(border === ctx.accent, `border: ${border}, accent: ${ctx.accent}`);
        """,
        # The cascade this fixed: the missing-value border declared after focus.
        sabotage="""
            const style = document.createElement("style");
            style.textContent = '.input[aria-invalid="true"] { border-color: var(--warn); }';
            document.head.append(style);
            await sleep(400);
        """,
    ),
    Check(
        id="first-boot-notify-off",
        fixture="writable",
        path="/settings#notifications",
        setup=_unset(),
        act="""
            await providersLoaded();
            ctx.shown = visible($("#notify-off"));
            $("#notify-off").click();
            await sleep(200);
            $("#notify-off").click();
            await waitFor(() => settled($("#notify-result")), "the notice");
        """,
        script="""
            expect(ctx.shown, "Turn off is hidden while no webhook is stored");
            expect(!$("#discord-webhook").hasAttribute("aria-invalid"), "the webhook is marked");
            const text = $("#notifications-missing").textContent;
            expect(!text.includes("Discord webhook"), `notice: ${text}`);
            const state = $("#notify-state");
            expect(visible(state), "Notifications are off. is not shown");
        """,
        sabotage_preload=rewrite_post("/api/settings/notify", "body.off = false;"),
        receipt=_last_call({"notify.discord_webhook_url": ""}),
    ),
    Check(
        id="first-boot-notify-completes",
        fixture="writable",
        path="/settings#notifications",
        setup=_unset("notify.discord_webhook_url", "notify.email_to", "notify.email_from"),
        act="""
            await settingsLoaded();
            ctx.enabled = !saveOf("notifications").disabled;
            $("#notify-off").click();
            await sleep(200);
            $("#notify-off").click();
            await waitFor(() => settled($("#notify-result")), "the notice");
            saveOf("notifications").click();
            await waitFor(() => saveOf("notifications").disabled
              && !noticeOf("notifications").textContent.startsWith("Sending"), "the save");
        """,
        script="""
            expect(ctx.enabled, "Save was disabled with the email pair missing");
            expect(!visible($("#notifications-missing")), "the category notice is still shown");
            expect(!navOf("notifications").classList.contains("missing"),
              "Notifications is still marked");
            const marked = $$('#notify-form [aria-invalid="true"]').map((el) => el.id);
            expect(marked.length === 0, `marked: ${marked}`);
        """,
        sabotage_preload=rewrite_post("/api/settings/email", 'body.email_to = "x@example.test";'),
        receipt=_calls(
            [
                {"notify.discord_webhook_url": ""},
                {"notify.email_to": "", "notify.email_from": ""},
            ]
        ),
    ),
    Check(
        id="first-boot-style-empty",
        fixture="writable",
        path="/settings#digest",
        setup=_unset("digest.style_prompt"),
        act="""
            await settingsLoaded();
            ctx.enabled = !saveOf("digest").disabled;
            ctx.dirty = navOf("digest").classList.contains("dirty");
            saveOf("digest").click();
            await waitFor(() => settled(noticeOf("digest")) && saveOf("digest").disabled,
              "the save");
        """,
        script="""
            expect(ctx.enabled, "Save was disabled with Style missing");
            expect(!ctx.dirty, "Digest had a dirty mark before any edit");
            expect(!visible($("#digest-missing")), "the category notice is still shown");
            expect(!navOf("digest").classList.contains("missing"), "Digest is still marked");
            expect(!$("#style-prompt").hasAttribute("aria-invalid"), "Style is still marked");
        """,
        # The answer before this fix: nothing is known to take an empty value.
        sabotage_preload="""
const realFetch = window.fetch;
window.fetch = async (input, init) => {
  const res = await realFetch(input, init);
  if (!String(input).endsWith("/api/settings") || (init && init.method)) return res;
  const data = await res.json();
  data.may_be_empty = [];
  return new Response(JSON.stringify(data), {status: res.status, headers: res.headers});
};
""",
        receipt=_calls([{"digest.style_prompt": ""}]),
    ),
    Check(
        id="digest-posts-new-fields",
        fixture="writable",
        path="/settings#digest",
        act="""
            await settingsLoaded();
            setValue($("#timezone"), "Europe/Berlin");
            setValue($("#output-language"), "en");
            saveOf("digest").click();
            await waitFor(() => settled(noticeOf("digest")) && saveOf("digest").disabled,
              "the save");
        """,
        script="""
            const text = noticeOf("digest").textContent;
            expect(text === "Timezone: Europe/Berlin, Output language: en. Effective next run.",
              `notice: ${text}`);
        """,
        sabotage_preload=rewrite_post(
            "/api/settings/values", 'body.values["digest.max_featured"] = 5;'
        ),
        receipt=_calls([{"general.timezone": "Europe/Berlin", "digest.output_language": "en"}]),
    ),
    Check(
        id="digest-style-empty",
        fixture="writable",
        path="/settings#digest",
        act="""
            await settingsLoaded();
            ctx.loaded = $("#style-prompt").value;
            setValue($("#style-prompt"), "");
            saveOf("digest").click();
            await waitFor(() => settled(noticeOf("digest")) && saveOf("digest").disabled,
              "the save");
        """,
        script="""expect(ctx.loaded === "x", `the style loaded as ${ctx.loaded}`);""",
        sabotage_preload=rewrite_post(
            "/api/settings/values", 'body.values["digest.style_prompt"] = "x";'
        ),
        receipt=_calls([{"digest.style_prompt": ""}]),
    ),
    Check(
        id="digest-bad-timezone",
        fixture="writable",
        path="/settings#digest",
        act="""
            await settingsLoaded();
            setValue($("#timezone"), "Mars/Base");
            saveOf("digest").click();
            await waitFor(() => settled(noticeOf("digest")), "the notice");
        """,
        script="""
            const notice = $("#digest-result"), text = notice.textContent;
            expect(notice.classList.contains("err"), `not an error: ${text}`);
            const wanted = "Timezone not saved. Timezone: 'Mars/Base' is not a timezone this "
              + "server knows";
            expect(text === wanted, `notice: ${text}`);
            expect(navOf("digest").classList.contains("dirty"), "Digest lost its dirty mark");
            expect(!saveOf("digest").disabled, "the digest Save was disabled");
        """,
        sabotage="""$("#digest-result").classList.remove("err");""",
        receipt=_calls([]),
    ),
    Check(
        id="type-scale-saves",
        fixture="writable",
        path="/settings#digest",
        act=SAVE_TYPE_SCALE.format(value="1.125"),
        script="""
            const text = $("#digest-result").textContent;
            expect(text.includes("Type size: 1.125."), `notice: ${text}`);
        """,
        sabotage="""$("#digest-result").textContent = "";""",
        receipt=_calls([{"digest.type_scale": 1.125}]),
    ),
    Check(
        id="type-scale-unset",
        fixture="writable",
        path="/settings#digest",
        setup=_unset("digest.type_scale"),
        act="await settingsLoaded();",
        script="""
            const select = $("#type-scale");
            expect(select.value === "", `value: ${select.value}`);
            const shown = select.selectedOptions[0]?.textContent;
            expect(shown === "Not set", `shown: ${shown}`);
            expect(select.getAttribute("aria-invalid") === "true", "the field is not marked");
            const notice = $("#digest-missing").textContent;
            expect(notice.includes("Type size"), `notice: ${notice}`);
        """,
        sabotage="""$('#type-scale option[value=""]').remove();""",
    ),
    Check(
        id="type-scale-unset-clears-on-save",
        fixture="writable",
        path="/settings#digest",
        setup=_unset("digest.type_scale"),
        act=SAVE_TYPE_SCALE.format(value="1"),
        script="""
            const select = $("#type-scale");
            expect(!$('option[value=""]', select), "Not set is still an option");
            expect(!select.hasAttribute("aria-invalid"), "the field is still marked");
        """,
        sabotage="""$("#type-scale").prepend(new Option("Not set", "", false, false));""",
        receipt=_calls([{"digest.type_scale": 1}]),
    ),
    Check(
        id="type-scale-applies-on-save",
        fixture="writable",
        path="/settings#digest",
        act=SAVE_TYPE_SCALE.format(value="1.125"),
        script="""
            const scale = getComputedStyle(document.documentElement)
              .getPropertyValue("--type-scale").trim();
            expect(scale === "1.125", `--type-scale: ${scale}`);
        """,
        sabotage="""document.documentElement.style.removeProperty("--type-scale");""",
    ),
    Check(
        id="type-scale-kept-on-refusal",
        fixture="writable",
        path="/settings#digest",
        preload=answer_post(
            "/api/settings/values",
            """Promise.resolve(new Response(JSON.stringify({ok: false, error: "refused"}),
              {status: 400, headers: {"Content-Type": "application/json"}}))""",
        ),
        act="""
            await settingsLoaded();
            setValue($("#type-scale"), "1.125");
            saveOf("digest").click();
            await waitFor(() => settled(noticeOf("digest")), "the notice");
        """,
        script="""
            const scale = getComputedStyle(document.documentElement)
              .getPropertyValue("--type-scale").trim();
            expect(scale === "1", `--type-scale: ${scale}`);
            expect($("#digest-result").classList.contains("err"), "the notice is not an error");
        """,
        sabotage="""document.documentElement.style.setProperty("--type-scale", "1.125");""",
        receipt=_calls([]),
    ),
    Check(
        id="hash-pipeline",
        fixture="readonly",
        path="/settings#pipeline",
        script="""
            expect(same(panels(), ["pipeline"]), `visible panels: ${panels()}`);
            expect(same(currentTabs(), ["#pipeline"]), `current: ${currentTabs()}`);
        """,
        sabotage="""$('.tab[data-tab="model"]').hidden = false;""",
    ),
    Check(
        id="pipeline-posts-changed-only",
        fixture="writable",
        path="/settings#pipeline",
        act="""
            await settingsLoaded();
            ctx.loaded = $("#max-articles").value;
            setValue($("#max-articles"), "400");
            saveOf("pipeline").click();
            await waitFor(() => settled(noticeOf("pipeline")) && saveOf("pipeline").disabled,
              "the save");
        """,
        script="""
            expect(ctx.loaded === "200", `loaded: ${ctx.loaded}`);
            const notice = $("#pipeline-result"), text = notice.textContent;
            expect(!notice.classList.contains("err"), `an error: ${text}`);
            expect(text === "Articles per run: 400. Effective next run.", `notice: ${text}`);
            expect(!navOf("pipeline").classList.contains("dirty"), "Pipeline is still marked");
        """,
        sabotage_preload=rewrite_post(
            "/api/settings/values", 'body.values["general.digest_window_hours"] = 24;'
        ),
        receipt=_calls([{"digest.max_articles_per_digest": 400}]),
    ),
    Check(
        id="pipeline-refusal",
        fixture="writable",
        path="/settings#pipeline",
        act="""
            await settingsLoaded();
            setValue($("#featured-threshold"), "150");
            saveOf("pipeline").click();
            await waitFor(() => settled(noticeOf("pipeline")), "the notice");
        """,
        script="""
            const notice = $("#pipeline-result"), text = notice.textContent;
            expect(visible(notice) && notice.classList.contains("err"), `not an error: ${text}`);
            expect(notice.parentElement === saveOf("pipeline").parentElement, "not beside Save");
            expect(text === "Top story score not saved. Top story score: Input should be less "
              + "than or equal to 100", text);
            expect(navOf("pipeline").classList.contains("dirty"), "Pipeline lost its dirty mark");
        """,
        sabotage="""$("#pipeline-result").hidden = true;""",
        receipt=_calls([]),
    ),
    Check(
        id="model-posts-vote-only",
        fixture="writable",
        path="/settings#model",
        act="""
            await settingsLoaded();
            ctx.loaded = $("#vote-seeds").value;
            setValue($("#vote-seeds"), "50");
            saveOf("model").click();
            await waitFor(() => settled(noticeOf("model")) && saveOf("model").disabled, "the save");
        """,
        script="""
            expect(ctx.loaded === "200", `loaded: ${ctx.loaded}`);
            const notice = noticeOf("model"), text = notice.textContent;
            expect(!notice.classList.contains("err"), `an error: ${text}`);
            expect(text.startsWith("Not checked: vote similarity is off."), `notice: ${text}`);
        """,
        sabotage_preload=also_post(
            "/api/settings/vote-similarity", "/api/settings", {"provider": "gemini", "model": ""}
        ),
        receipt=_calls(
            [
                {
                    "vote_similarity.enabled": False,
                    "vote_similarity.provider": "workers_ai",
                    "vote_similarity.model": "",
                    "vote_similarity.max_seeds": 50,
                }
            ]
        ),
    ),
    Check(
        id="model-vote-checking",
        fixture="writable",
        path="/settings#model",
        preload=hold_post("/api/settings/vote-similarity"),
        act="""
            await settingsLoaded();
            setValue($("#vote-enabled"), "true");
            saveOf("model").click();
            await waitFor(() => window.__release, "the held save");
        """,
        script="""
            const notice = noticeOf("model");
            expect(visible(notice) && notice.textContent === "Checking the embedder…",
              `notice: ${visible(notice) && notice.textContent}`);
            expect(saveOf("model").disabled, "Save came back while the check was out");
        """,
        sabotage="""noticeOf("model").textContent = "Checking with the provider…";""",
    ),
    Check(
        id="embedding-readiness",
        fixture="writable",
        path="/settings#model",
        act="""await waitFor(() => $$("input[name=embedding-provider]").length, "embedders");""",
        script="""
            const radio = $('input[name=embedding-provider][value="workers_ai"]');
            const row = radio.closest("label.choice");
            const state = $(".label", row).textContent.trim();
            expect(state === "CLOUDFLARE_EMBEDDING_API_TOKEN missing", `workers_ai: ${state}`);
            expect(!radio.disabled && radio.checked, "workers_ai cannot be chosen or is unchosen");
            const input = $("#embedding-model-input");
            expect(input.placeholder === "Empty uses @cf/baai/bge-m3", input.placeholder);
        """,
        sabotage="""$('input[name=embedding-provider][value="workers_ai"]').disabled = true;""",
    ),
    Check(
        id="refusal-marks-the-field",
        fixture="writable",
        path="/settings#digest",
        act=SAVE_BAD_TIMEZONE,
        script="""
            expect($("#timezone").classList.contains("invalid"), "the field is not marked");
        """,
        sabotage="""$("#timezone").classList.remove("invalid");""",
        receipt=_calls([]),
    ),
    Check(
        id="refusal-explained-below-the-field",
        fixture="writable",
        path="/settings#digest",
        act=SAVE_BAD_TIMEZONE,
        script=f"""
            const below = $("#timezone").nextElementSibling;
            expect(visible(below) && below.classList.contains("err"), "no error below the field");
            expect(below.textContent === {json.dumps(TIMEZONE_REFUSED)}, below.textContent);
        """,
        sabotage="""$("#timezone").nextElementSibling.hidden = true;""",
        receipt=_calls([]),
    ),
    Check(
        id="refusal-clears-on-edit",
        fixture="writable",
        path="/settings#digest",
        act=SAVE_BAD_TIMEZONE + """setValue($("#timezone"), "Europe/Berlin");""",
        script="""
            const field = $("#timezone");
            expect(!field.classList.contains("invalid"), "the field is still marked");
            expect(!visible(field.nextElementSibling), "the field's notice is still shown");
            expect(!field.hasAttribute("aria-invalid"), "the field is still announced invalid");
            expect(!field.hasAttribute("aria-describedby"), "the field still points at a notice");
        """,
        sabotage="""$("#timezone").classList.add("invalid");""",
    ),
    Check(
        id="refusal-is-announced",
        fixture="writable",
        path="/settings#digest",
        act=SAVE_BAD_TIMEZONE,
        script=f"""
            const field = $("#timezone"), below = field.nextElementSibling;
            expect(field.getAttribute("aria-invalid") === "true", "the field is not announced");
            const described = document.getElementById(field.getAttribute("aria-describedby"));
            expect(described === below, "the field is not described by its notice");
            expect(described.textContent === {json.dumps(TIMEZONE_REFUSED)}, described.textContent);
        """,
        sabotage="""$("#timezone").removeAttribute("aria-describedby");""",
        receipt=_calls([]),
    ),
    Check(
        id="refusal-keeps-a-missing-mark",
        fixture="writable",
        path="/settings#digest",
        setup=_unset("general.timezone"),
        act=SAVE_BAD_TIMEZONE + """setValue($("#timezone"), "Europe/Berl");""",
        script="""
            const field = $("#timezone");
            expect(!field.classList.contains("invalid"), "the refusal is still marked");
            expect(field.getAttribute("aria-invalid") === "true", "the missing mark went with it");
        """,
        sabotage="""$("#timezone").removeAttribute("aria-invalid");""",
    ),
    Check(
        id="model-refusal-marks-the-model",
        fixture="writable",
        path="/settings#model",
        preload=refuse_post("/api/settings", MODEL_REFUSED, "llm_provider.model"),
        act="""
            await settingsLoaded();
            $('input[value="anthropic"]').click();
            saveOf("model").click();
            await waitFor(() => settled(noticeOf("model")), "the refusal");
        """,
        script=f"""
            const field = $("#model-input"), below = field.nextElementSibling;
            const wanted = {json.dumps(MODEL_REFUSED)};
            expect(field.classList.contains("invalid"), "the model is not marked");
            expect(visible(below) && below.textContent === wanted, `below: ${{below.textContent}}`);
            expect(noticeOf("model").textContent === wanted, noticeOf("model").textContent);
        """,
        sabotage="""$("#model-input").classList.remove("invalid");""",
    ),
    Check(
        id="refusal-marks-a-choice-list",
        fixture="writable",
        path="/settings#model",
        preload=refuse_post(
            "/api/settings/vote-similarity", EMBEDDER_REFUSED, "vote_similarity.provider"
        ),
        act="""
            await settingsLoaded();
            setValue($("#vote-enabled"), "true");
            saveOf("model").click();
            await waitFor(() => settled(noticeOf("model")), "the refusal");
        """,
        script=WARN
        + f"""
            const choices = $("#embedding-providers"), below = choices.nextElementSibling;
            const border = getComputedStyle(choices).borderTopColor;
            expect(border === warnOf(), `border: ${{border}}`);
            expect(visible(below) && below.textContent === {json.dumps(EMBEDDER_REFUSED)},
              `below: ${{below.textContent}}`);
        """,
        sabotage="""$("#embedding-providers").classList.remove("invalid");""",
    ),
    Check(
        id="source-refusal-marks-the-field",
        fixture="writable",
        path="/settings#sources",
        act="""
            await openRow("Hacker News");
            setValue($("#e-url", editor()), "");
            editorAct("save").click();
            await waitFor(() => settled(editorAct("save").parentElement.querySelector(".notice")),
              "the refusal");
        """,
        script="""
            const url = $("#e-url", editor()), below = url.nextElementSibling;
            expect(url.classList.contains("invalid"), "Feed URL is not marked");
            expect(visible(below) && below.textContent === "An RSS source needs a Feed URL.",
              `below: ${below.textContent}`);
        """,
        sabotage="""$("#e-url", editor()).classList.remove("invalid");""",
        receipt=_stored("Hacker News", url="https://hnrss.org/frontpage?points=200"),
    ),
    *(
        Check(
            id=f"busy-notice-{save}",
            fixture="writable",
            path=start,
            preload=hold_post(post),
            act=f"""
                await settingsLoaded();
                {change}
                saveOf("{tab}").click();
                await waitFor(() => window.__release, "the held save");
            """,
            script=f"""
                const notice = noticeOf("{tab}");
                expect(visible(notice) && notice.textContent === {json.dumps(busy)},
                  `notice: ${{visible(notice) && notice.textContent}}`);
                expect(notice.getAttribute("aria-busy") === "true", "the notice is not busy");
            """,
            sabotage=f"""noticeOf("{tab}").removeAttribute("aria-busy");""",
        )
        for save, (start, post, change, tab, busy) in BUSY.items()
    ),
    Check(
        id="busy-notice-turn-off",
        fixture="writable",
        path="/settings#notifications",
        preload=hold_post("/api/settings/notify"),
        act="""
            await settingsLoaded();
            $("#notify-off").click();
            await sleep(200);
            $("#notify-off").click();
            await waitFor(() => window.__release, "the held turn off");
        """,
        script="""
            const notice = $("#notify-result");
            expect(visible(notice) && notice.textContent === "Turning off…",
              `notice: ${visible(notice) && notice.textContent}`);
            expect(notice.getAttribute("aria-busy") === "true", "the notice is not busy");
        """,
        sabotage="""$("#notify-result").removeAttribute("aria-busy");""",
    ),
    Check(
        id="busy-notice-source-save",
        fixture="writable",
        path="/settings#sources",
        preload=hold_post("/api/sources"),
        act="""
            await openRow("Hacker News");
            setValue($("#e-tags", editor()), "news");
            editorAct("save").click();
            await waitFor(() => window.__release, "the held save");
        """,
        script="""
            const notice = editorAct("save").parentElement.querySelector(".notice");
            expect(visible(notice) && notice.textContent === "Saving…",
              `notice: ${visible(notice) && notice.textContent}`);
            expect(notice.getAttribute("aria-busy") === "true", "the notice is not busy");
        """,
        sabotage="""
            editorAct("save").parentElement.querySelector(".notice").removeAttribute("aria-busy");
        """,
    ),
    Check(
        id="editor-dims-while-saving",
        fixture="writable",
        path="/settings#sources",
        preload=hold_post("/api/sources"),
        act="""
            await openRow("Hacker News");
            setValue($("#e-tags", editor()), "news");
            editorAct("save").click();
            await waitFor(() => window.__release, "the held save");
        """,
        script="""
            const opacity = getComputedStyle($(".form", editor())).opacity;
            expect(opacity === "0.4", `the locked fields' opacity: ${opacity}`);
        """,
        sabotage="""editor().inert = false;""",
    ),
    Check(
        id="busy-notice-retire",
        fixture="writable",
        path="/settings#sources",
        preload=hold_delete(),
        act=ARM_RETIRE
        + """
            await sleep(200);
            editorAct("retire").click();
            await waitFor(() => window.__release, "the held retire");
        """,
        script="""
            const notice = editorAct("retire").parentElement.querySelector(".notice");
            expect(visible(notice) && notice.textContent === "Retiring…",
              `notice: ${visible(notice) && notice.textContent}`);
            expect(notice.getAttribute("aria-busy") === "true", "the notice is not busy");
        """,
        sabotage="""
            editorAct("retire").parentElement.querySelector(".notice").removeAttribute("aria-busy");
        """,
        receipt=_still_listed("Hacker News"),
    ),
    Check(
        id="model-vote-refusal",
        fixture="writable",
        path="/settings#model",
        act="""
            await settingsLoaded();
            setValue($("#vote-seeds"), "0");
            saveOf("model").click();
            await waitFor(() => settled(noticeOf("model")), "the notice");
        """,
        script="""
            const notice = $("#model-result"), text = notice.textContent;
            expect(visible(notice) && notice.classList.contains("err"), `not an error: ${text}`);
            expect(text === "Votes compared: Input should be greater than or equal to 1",
              `notice: ${text}`);
            expect(navOf("model").classList.contains("dirty"), "Model lost its dirty mark");
            expect(!saveOf("model").disabled, "the Model Save was disabled");
        """,
        sabotage="""$("#model-result").classList.remove("err");""",
        receipt=_calls([]),
    ),
    Check(
        id="model-none-posts",
        fixture="writable",
        path="/settings#model",
        preload=RECORD_POSTS,
        act="""
            await settingsLoaded();
            setValue($("#model-input"), "gemini-3.8-flash");
            $('input[name=provider][value="none"]').click();
            ctx.disabled = $("#model-input").disabled;
            saveOf("model").click();
            await waitFor(() => settled(noticeOf("model")) && saveOf("model").disabled
              && !noticeOf("model").textContent.startsWith("Checking"), "the save");
        """,
        script="""
            expect(ctx.disabled, "the model field stayed enabled under None");
            const label = $(".name", choice("none")).textContent;
            expect(label === "None — plain excerpts", `label: ${label}`);
            const text = noticeOf("model").textContent;
            expect(text.startsWith("No model is called: digests list plain excerpts."), text);
            // The server stores "" for None whatever it is sent, so read what was sent.
            const sent = window.__sent.filter(([url]) => url.endsWith("/api/settings"));
            expect(same(sent.map(([, body]) => body), [{provider: "none", model: ""}]),
              `sent: ${JSON.stringify(sent)}`);
        """,
        sabotage_preload=rewrite_post("/api/settings", 'body.model = "gemini-3.8-flash";'),
        receipt=_calls([{"llm_provider.provider": "none", "llm_provider.model": ""}]),
    ),
    Check(
        id="model-none-loaded",
        fixture="writable",
        path="/settings#model",
        setup=lambda fixture: fixture.values.update({"llm_provider.provider": "none"}),
        act="await providersLoaded();",
        script="""
            const checked = $$("input[name=provider]:checked").map((input) => input.value);
            expect(same(checked, ["none"]), `checked: ${checked}`);
            expect($("#model-input").disabled, "the model field is enabled");
        """,
        sabotage="""$("#model-input").disabled = false;""",
    ),
    Check(
        id="model-none-label",
        fixture="writable",
        path="/settings#model",
        act="await providersLoaded();",
        script="""
            const label = $(".label", choice("none")).textContent.trim();
            expect(label === "No key needed", `label: ${label}`);
        """,
        sabotage="""$(".label", choice("none")).textContent = "";""",
    ),
    Check(
        id="model-missing-unchecked",
        fixture="writable",
        path="/settings#model",
        setup=_unset("llm_provider.provider"),
        act="await providersLoaded();",
        script="""
            const checked = $$("input[name=provider]:checked").map((input) => input.value);
            expect(checked.length === 0, `checked: ${checked}`);
            expect(navOf("model").classList.contains("missing"), "Model is not marked");
        """,
        sabotage_preload="""
const realFetch = window.fetch;
window.fetch = async (input, init) => {
  const res = await realFetch(input, init);
  if (!String(input).endsWith("/api/settings") || (init && init.method)) return res;
  const data = await res.json();
  data.values["llm_provider.provider"] = data.providers[0].name;
  return new Response(JSON.stringify(data), {status: res.status, headers: res.headers});
};
""",
    ),
    Check(
        id="notify-off-arms",
        fixture="writable",
        path="/settings#notifications",
        act="""
            await settingsLoaded();
            ctx.shown = visible($("#notify-off"));
            $("#notify-off").click();
            await sleep(300);
        """,
        script="""
            expect(ctx.shown, "Turn off is hidden while a webhook is stored");
            const button = $("#notify-off");
            expect(button.classList.contains("armed"), "not armed");
            expect(button.textContent === "Confirm turn off", `text: ${button.textContent}`);
        """,
        sabotage_preload="""
document.addEventListener("click", (event) => {
  if (!event.target.closest("#notify-off")) return;
  fetch("/api/settings/notify", {method: "POST",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify({off: true})});
}, true);
""",
        receipt=_calls([]),
    ),
    Check(
        id="notify-off-stores",
        fixture="writable",
        path="/settings#notifications",
        act="""
            await settingsLoaded();
            $("#notify-off").click();
            await sleep(200);
            $("#notify-off").click();
            await waitFor(() => settled($("#notify-result")), "the notice");
        """,
        script="""
            const state = $("#notify-state"), notice = $("#notify-result");
            expect(visible(state) && state.textContent === "Notifications are off.",
              `state: ${visible(state) && state.textContent}`);
            expect(!visible($("#notify-off")), "Turn off is still shown");
            expect($("#discord-webhook").value === "", `field: ${$("#discord-webhook").value}`);
            expect(!notice.classList.contains("err"), `an error: ${notice.textContent}`);
            const wanted = "Notifications are off. The next run finishes without a message.";
            expect(notice.textContent === wanted, `notice: ${notice.textContent}`);
            expect(saveOf("notifications").disabled, "Save is enabled after turning off");
        """,
        sabotage="""$("#notify-state").hidden = true;""",
        receipt=_last_call({"notify.discord_webhook_url": ""}),
    ),
    Check(
        id="notify-off-hidden-when-off",
        fixture="writable",
        path="/settings#notifications",
        setup=lambda fixture: fixture.values.update({"notify.discord_webhook_url": ""}),
        act="await settingsLoaded();",
        script="""
            expect(!visible($("#notify-off")), "Turn off is shown with notifications off");
            const state = $("#notify-state");
            expect(visible(state) && state.textContent === "Notifications are off.",
              `state: ${visible(state) && state.textContent}`);
        """,
        sabotage="""$("#notify-off").hidden = false;""",
    ),
    Check(
        id="readonly-settings",
        fixture="readonly",
        path="/settings#model",
        act="""
            await settingsLoaded();
            ctx.notices = {};
            for (const tab of ["model", "digest", "pipeline", "notifications"]) {
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
            const tabs = ["model", "digest", "pipeline", "notifications"];
            const live = tabs.filter((t) => !saveOf(t).disabled);
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
            const reason = "No writable source table here — edit sources.yaml instead.";
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
    # Clipping inside `.choices` or `.table-wrap` never widens the document, so
    # the fits checks above cannot see it; these measure the boxes themselves.
    Check(
        id="fits-375-choice-states",
        fixture="writable",
        path="/settings#model",
        width=375,
        act="""
            await providersLoaded();
            await waitFor(() => $$("input[name=embedding-provider]").length, "embedders");
        """,
        script="""
            const states = $$(".choice > .label");
            expect(states.some((s) => s.textContent === "CLOUDFLARE_EMBEDDING_API_TOKEN missing"),
              "the long state is not in the fixture");
            const cut = states.filter((state) => {
              const r = state.getBoundingClientRect();
              const box = state.closest(".choices").getBoundingClientRect();
              return state.scrollWidth > state.clientWidth
                || r.left < box.left || r.right > box.right + 0.5;
            }).map((state) => state.textContent);
            expect(cut.length === 0, `cut off: ${cut.join("; ")}`);
        """,
        sabotage="""
            $$(".choice").forEach((row) => {
              row.style.display = "grid";
              row.style.gridTemplateColumns = "auto 1fr auto";
            });
        """,
    ),
    Check(
        id="fits-375-source-editor",
        fixture="writable",
        path="/settings#sources",
        width=375,
        act="""await openRow("Hacker News");""",
        script="""
            const wrap = $(".table-wrap");
            const outside = (where) => {
              const box = wrap.getBoundingClientRect();
              const right = Math.min(box.right, viewport());
              return $$("input, select, button, summary", editor()).filter(visible)
                .filter((el) => {
                  const r = el.getBoundingClientRect();
                  return r.left < Math.max(box.left, 0) || r.right > right + 0.5;
                })
                .map((el) => `${where}: ${el.id || el.dataset.act || el.dataset.type}`);
            };
            const problems = outside("rows unscrolled");
            wrap.scrollLeft = wrap.scrollWidth;
            await waitFor(() => wrap.scrollLeft > 0, "the rows to scroll");
            problems.push(...outside("rows scrolled to the end"));
            expect(problems.length === 0, problems.join("; "));
        """,
        sabotage="""
            const body = $(".editor-body");
            body.style.position = "static";
            body.style.width = "auto";
        """,
    ),
    Check(
        id="fits-375-tab-hint",
        fixture="writable",
        path="/settings",
        width=375,
        act="await settingsLoaded();",
        script="""
            const nav = $(".settings-nav");
            expect(nav.scrollWidth > nav.clientWidth, "the tab bar does not overflow here");
            // A pseudo-element has no box to read, and the hint takes no taps, so
            // for this measure only both pseudo-elements are made hit-testable: a
            // hit at the bar's right edge then lands on the nav while the hint is there.
            const hittable = document.createElement("style");
            hittable.textContent =
              ".settings-nav::before, .settings-nav::after { pointer-events: auto !important; }";
            document.head.append(hittable);
            try {
              const bar = nav.getBoundingClientRect();
              nav.scrollLeft = 0;
              const edge = document.elementFromPoint(bar.right - 4, bar.top + bar.height / 2);
              expect(edge === nav, `no hint at the start: ${edge?.dataset?.tab}`);
              nav.scrollLeft = nav.scrollWidth;
              await waitFor(() => nav.scrollLeft > 0, "the tab bar to scroll");
              const sources = navOf("sources"), r = sources.getBoundingClientRect();
              const hit = document.elementFromPoint(r.right - 1, r.top + r.height / 2);
              expect(r.right <= bar.right && sources.contains(hit),
                `Sources is covered at the end: ${hit?.className}`);
            } finally {
              hittable.remove();
            }
        """,
        sabotage="""
            const off = document.createElement("style");
            off.textContent = ".settings-nav::before { content: none; }";
            document.head.append(off);
        """,
    ),
    Check(
        id="fits-375-tab-hint-taps-through",
        fixture="writable",
        path="/settings",
        width=375,
        act="await settingsLoaded();",
        script="""
            const nav = $(".settings-nav"), bar = nav.getBoundingClientRect();
            nav.scrollLeft = 0;
            const tab = document.elementFromPoint(bar.right - 4, bar.top + bar.height / 2)
              ?.closest(".settings-nav a");
            expect(tab, "a tap at the bar's edge reaches no tab");
            tab.click();
            await waitFor(() => same(panels(), [tab.dataset.tab]), tab.dataset.tab);
        """,
        sabotage="""
            const eats = document.createElement("style");
            eats.textContent = ".settings-nav::before { pointer-events: auto; }";
            document.head.append(eats);
        """,
    ),
]
# Every tap target takes taps over 44 x 44 on a phone, and none covers another: a
# `More` stays off the control above it, which the Publish hours inputs sit right on.
MORE_AND_CONTROLS = "details.more summary, .input, .select"
CHECKS += [
    Check(
        id="tap-tabs-375",
        fixture="writable",
        path="/settings",
        width=375,
        act="await settingsLoaded();",
        script="""expectTapTargets(".settings-nav a");""",
        sabotage=restyle(".settings-nav a { min-height: 0 !important; }"),
    ),
    *(
        Check(
            id=check_id,
            fixture="writable",
            path="/settings",
            width=375,
            act="await settingsLoaded();",
            script=f"""
                let mores = 0;
                const problems = await eachCategory(() => {{
                  mores += $$("details.more summary").filter(visible).length;
                  return $$({json.dumps(MORE_AND_CONTROLS)})
                    .filter(visible).map(tapProblem).filter(Boolean);
                }});
                expect(mores, "no More shows");
                expect(!problems.length, problems.join("; "));
            """,
            sabotage=restyle(f"details.more summary::after {{ {rule} }}"),
        )
        for check_id, rule in (
            ("tap-more-375", "content: none !important;"),
            ("tap-more-apart-375", "inset: min(0px, calc((100% - 44px) / 2)) 0 !important;"),
        )
    ),
]
# Tracked topics: the Sources table and editor again, where no topic is a valid answer.
CHECKS += [
    Check(
        id="tracking-rows",
        fixture="writable-topics",
        path="/settings#tracking",
        act="await topicsListed();",
        script="""
            expect(same(topicNames(), ["Anthropic", "Chips"]), `rows: ${topicNames()}`);
            const line = $(".feed-problem", topicRowOf("Chips"));
            const text = line && line.textContent;
            const want = "Set for gemini-embedding-001; runs embed with @cf/baai/bge-m3, "
              + "so it is skipped";
            expect(text === want, `line: ${text}`);
            expect(!$(".feed-problem", topicRowOf("Anthropic")), "a matching topic shows a line");
        """,
        sabotage="""$(".feed-problem", topicRowOf("Chips")).remove();""",
    ),
    Check(
        id="tracking-empty-is-no-missing-mark",
        fixture="writable",
        path="/settings#tracking",
        act="await topicsListed();",
        script="""
            const rows = $$("#topic-body tr").map((row) => row.textContent.trim());
            expect(same(rows, ["No topics yet."]), `rows: ${rows}`);
            expect(!navOf("tracking").classList.contains("missing"), "Tracking is marked missing");
            expect(!$("#add-topic").disabled, "Add topic is disabled");
        """,
        sabotage="""navOf("tracking").classList.add("missing");""",
    ),
    Check(
        id="tracking-add",
        fixture="writable",
        path="/settings#tracking",
        act="""
            await topicsListed();
            $("#add-topic").click();
            await waitFor(editor, "the editor");
            ctx.model = $("#t-model", editor()).value;
            setValue($("#t-name", editor()), "Lotteries");
            setValue($("#t-description", editor()), "Lottery draws and jackpots");
            setValue($("#t-threshold", editor()), "0.6");
            editorAct("save").click();
            await waitFor(() => topicRowOf("Lotteries"), "the saved row");
        """,
        script="""
            expect(ctx.model === "@cf/baai/bge-m3", `the model started as ${ctx.model}`);
            expect(same(topicNames(), ["Lotteries"]), `rows: ${topicNames()}`);
        """,
        sabotage="""topicRowOf("Lotteries").remove();""",
        receipt=_topic_stored(
            "Lotteries",
            description="Lottery draws and jackpots",
            threshold=0.6,
            model=PROBE_EMBEDDING_MODEL,
        ),
    ),
    Check(
        id="tracking-no-threshold-refused",
        fixture="writable",
        path="/settings#tracking",
        act="""
            await topicsListed();
            $("#add-topic").click();
            await waitFor(editor, "the editor");
            setValue($("#t-name", editor()), "Lotteries");
            setValue($("#t-description", editor()), "Lottery draws and jackpots");
            editorAct("save").click();
            await waitFor(() => $("#t-threshold", editor()).classList.contains("invalid"),
                          "the refusal");
        """,
        script="""
            const field = $("#t-threshold", editor());
            expect(field.getAttribute("aria-invalid") === "true", "the threshold is not marked");
            expect(!topicRowOf("Lotteries"), "a topic with no threshold was listed");
        """,
        sabotage="""$("#t-threshold", editor()).removeAttribute("aria-invalid");""",
        receipt=_topic_gone("Lotteries"),
    ),
    Check(
        id="tracking-remove",
        fixture="writable-topics",
        path="/settings#tracking",
        act="""
            await topicsListed();
            topicRowOf("Chips").click();
            await waitFor(editor, "the editor");
            editorAct("remove").click();
            ctx.armed = editorAct("remove").textContent;
            editorAct("remove").click();
            await waitFor(() => !topicRowOf("Chips"), "the row to go");
        """,
        script="""
            expect(ctx.armed === "Confirm remove", `the first press read ${ctx.armed}`);
            expect(same(topicNames(), ["Anthropic"]), `rows: ${topicNames()}`);
        """,
        sabotage="""
            const row = $("#topic-body").insertRow();
            row.className = "src-row";
            row.insertCell().textContent = "Chips";
        """,
        receipt=_topic_gone("Chips"),
    ),
]
# The page fits at the largest type size too.
CHECKS += [
    largest_twin(check, check.id.replace("fits-", "fits-largest-"), "writable-largest")
    for check in CHECKS
    if check.id.startswith("fits-")
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
// The list answered, empty or not.
const sourcesListed = () =>
  waitFor(() => $$("#src-body tr").length && !$("#src-body [data-loading]"), "the sources list");
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
// A notice showing a save's result, not what the save is still doing.
const settled = (notice) => visible(notice) && notice.getAttribute("aria-busy") !== "true";
const eachCategory = async (measure) => {
  const problems = [];
  for (const tab of ["model", "digest", "pipeline", "notifications", "sources", "tracking"]) {
    location.hash = tab;
    await waitFor(() => same(panels(), [tab]), tab);
    problems.push(...measure(tab));
  }
  return problems;
};
const topicNames = () =>
  $$("#topic-body tr.src-row").map((row) => $("td", row).firstChild.textContent);
const topicsListed = () =>
  waitFor(() => $$("#topic-body tr").length && !$("#topic-body [data-loading]"), "the topic list");
const topicRowOf = (name) => $(`#topic-body tr.src-row[data-name="${CSS.escape(name)}"]`);
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
