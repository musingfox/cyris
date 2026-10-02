"""The browser half of a page probe: drive a served page in headless Chromium.

A page probe (`settings_probe.py`, `raw_probe.py`) owns its fixtures and its
checks; this module owns everything that does not know which page it is on.
It serves a fixture in-process, loads the page in Chromium over the DevTools
Protocol, runs a check's steps in the page, and reads the result back.

Chromium is driven through a short Node script, as `css_computed.py` does and
for the same reason: Node 22+ ships a WebSocket client and Python's standard
library has none. No browser-automation package is installed for this, and
none may be.

Every check carries a sabotage that breaks the thing it reads. `--self-test`
runs each check clean, where it must pass, and sabotaged, where it must fail,
so a check that cannot fail is reported rather than trusted.
"""

import asyncio
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol, get_args

from aiohttp import web
from css_computed import _devtools_port, _page_socket

from cyris.config import DigestConfig

CHECK_TIMEOUT_S = 30
POINTERS = ("mouse", "touch")
MOUSE_BUTTONS = ("left", "middle", "right")
KEYS = ("Enter", " ", "Tab")
HEIGHT = 900
# The UI spec's tap area (§4): the least a target takes taps over, each way.
TAP_PX = 44
MINIMUM_NODE = 22


class Fixture(Protocol):
    """What the core needs from a probe's fixture: the app to serve."""

    app: web.Application


@dataclass
class VoteFixture:
    """A page served beside a stand-in `/api/vote`; `posts` is the receipt of every vote."""

    app: web.Application
    posts: list[dict] = field(default_factory=list)
    post_headers: list[dict] = field(default_factory=list)


# Installed before the page loads: a page that learned a credential would send it with a POST.
SEND_CREDENTIAL = """{
const realFetch = window.fetch;
window.fetch = (input, init) => init && init.method === "POST"
  ? realFetch(input, {...init, headers: {...init.headers, Authorization: "x"}})
  : realFetch(input, init);
}"""


def restyle(css: str) -> str:
    """A sabotage: `css` is appended to the page as its last stylesheet."""
    return f"""{{
const style = document.createElement("style");
style.textContent = {json.dumps(css)};
document.head.append(style);
}}"""


# A sabotage: each vote button's hit area is centred on it, as on any other small
# button, instead of splitting the gap with its pair, so the two overlap.
CENTRED_VOTE_AREAS = restyle(
    ".vote-group > .promote-btn::after {"
    f" left: min(0px, calc((100% - {TAP_PX}px) / 2)) !important;"
    f" right: min(0px, calc((100% - {TAP_PX}px) / 2)) !important;"
    " width: auto !important; }"
)
# A sabotage: the small buttons lose their hit areas and take taps over 34px alone.
NO_BUTTON_AREAS = restyle(".btn.sm::after { content: none !important; }")
# A sabotage: the site bar's links take taps over their drawn boxes alone.
NO_SITE_BAR_AREAS = restyle(".brand::after, .site-nav a::after { content: none !important; }")


# The largest type size, and the style the app Worker injects for it
# (`typeScaleStyle` in workers/app/src/type_scale.js), byte for byte.
LARGEST_TYPE_SCALE = max(get_args(DigestConfig.model_fields["type_scale"].annotation))
LARGEST_TYPE_SCALE_STYLE = f"<style>html:root{{--type-scale:{LARGEST_TYPE_SCALE}}}</style>"
# The smallest, where glyphs are narrowest and a hit area reaches furthest past them.
SMALLEST_TYPE_SCALE = min(get_args(DigestConfig.model_fields["type_scale"].annotation))
SMALLEST_TYPE_SCALE_STYLE = f"<style>html:root{{--type-scale:{SMALLEST_TYPE_SCALE}}}</style>"


def _at_type_scale(page: str, style: str) -> str:
    if "</head>" not in page:
        raise ValueError("the page has no </head> to put the type size before")
    return page.replace("</head>", style + "</head>", 1)


def at_largest_type_scale(page: str) -> str:
    """`page` as the app Worker serves it at the largest type size."""
    return _at_type_scale(page, LARGEST_TYPE_SCALE_STYLE)


def at_smallest_type_scale(page: str) -> str:
    """`page` as the app Worker serves it at the smallest type size."""
    return _at_type_scale(page, SMALLEST_TYPE_SCALE_STYLE)


@dataclass(frozen=True)
class Check:
    """One behaviour, read from the DOM and, where something was written, the fixture.

    `act` performs the steps and may record observations in `ctx`; `sabotage`
    runs after it in a self-test; `gestures` are then sent as real browser
    input; `script` then asserts with `expect`. A sabotage that cannot be
    applied through the page is a `sabotage_preload` instead, installed before
    the page loads.

    `media` names CSS media features the browser reports for the whole check,
    such as `(("prefers-reduced-motion", "reduce"),)`. `focused` reports the
    page as the focused window, without which a headless page matches no `:focus`.

    A gesture step is `{"press": selector, "pointer": "mouse" | "touch"}`,
    `{"move": dx}`, `{"release": True}` or `{"hover": selector}`; a move or a
    release belongs to the press before it. A mouse press may name its
    `"button"` (`MOUSE_BUTTONS`, default left), and a move may add a vertical
    `"dy"`. `{"key": name}` presses and releases one of `KEYS` on whatever has
    focus.
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
    setup: Callable[[Any], None] | None = None
    receipt: Callable[[Any], str | None] | None = None
    gestures: tuple[dict, ...] = ()
    media: tuple[tuple[str, str], ...] = ()
    focused: bool = False

    def __post_init__(self) -> None:
        pressed = False
        for step in self.gestures:
            keys = set(step)
            if (
                keys - {"button"} == {"press", "pointer"}
                and step["pointer"] in POINTERS
                and step.get("button", "left") in MOUSE_BUTTONS
                and ("button" not in keys or step["pointer"] == "mouse")
            ):
                pressed = True
            elif (
                keys in ({"move"}, {"move", "dy"})
                and all(isinstance(value, int) for value in step.values())
                and pressed
            ):
                pass
            elif keys == {"release"} and pressed:
                pressed = False
            elif keys == {"key"} and step["key"] in KEYS:
                pass
            elif keys != {"hover"}:
                raise ValueError(f"{self.id}: gesture step {step} is unknown or has no press")

    @property
    def paths(self) -> tuple[str, ...]:
        return (self.path,) if isinstance(self.path, str) else self.path


def largest_twin(check: Check, check_id: str, fixture: str) -> Check:
    """`check` again as `check_id`, on a `fixture` serving the page at the largest type size.

    It asserts that size took effect before anything else, so a fixture that
    failed to inject cannot pass as scale 1; its sabotage is its twin's.
    """
    return replace(
        check, id=check_id, fixture=fixture, script="expectLargestScale();\n" + check.script
    )


def smallest_twin(check: Check, check_id: str, fixture: str) -> Check:
    """`check` again as `check_id`, on a `fixture` serving the page at the smallest type size.

    Like `largest_twin`, it asserts that size took effect first; its sabotage is its twin's.
    """
    return replace(
        check, id=check_id, fixture=fixture, script="expectSmallestScale();\n" + check.script
    )


def registry_problems(checks: Iterable[Check], kinds: Iterable[str]) -> list[str]:
    """Name every check the self-test could not trust: repeated, unsabotaged, or unserved.

    `kinds` are the fixtures the probe can build.
    """
    problems, seen, served = [], set(), set(kinds)
    for check in checks:
        if check.id in seen:
            problems.append(f"{check.id}: named twice")
        seen.add(check.id)
        if not (check.sabotage.strip() or check.sabotage_preload):
            problems.append(f"{check.id}: no sabotage")
        if check.fixture not in served:
            problems.append(f"{check.id}: unknown fixture {check.fixture}")
    return problems


def base_prelude(scrolls_sideways: tuple[str, ...] = ()) -> str:
    """The page-side helpers every probe shares.

    `scrolls_sideways` names the containers allowed to scroll horizontally, so
    content inside them is not reported by `escaping` as page overflow.
    """
    allowed = json.dumps(", ".join(scrolls_sideways))
    return f"""
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const visible = (el) => !!el && el.offsetParent !== null;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const expect = (condition, detail) => {{ if (!condition) throw new Error(detail); }};
const waitFor = async (probe, what, ms = 5000) => {{
  const end = Date.now() + ms;
  while (Date.now() < end) {{
    try {{ const value = probe(); if (value) return value; }} catch {{}}
    await sleep(25);
  }}
  throw new Error(`timed out waiting for ${{what}}`);
}};
const same = (actual, wanted) => JSON.stringify(actual) === JSON.stringify(wanted);
// clientWidth, not innerWidth: a classic scrollbar takes its width out of the
// viewport, and content under it would otherwise pass.
const viewport = () => document.documentElement.clientWidth;
const SCROLLS_SIDEWAYS = {allowed};
const escaping = () => $$("body *")
  .filter((el) => !SCROLLS_SIDEWAYS || !el.closest(SCROLLS_SIDEWAYS))
  .filter((el) => el.getBoundingClientRect().right > viewport() + 0.5)
  .map((el) => `${{el.tagName.toLowerCase()}}.${{[...el.classList].join(".")}}`);
const overflow = (where) => document.documentElement.scrollWidth > viewport()
  ? `${{where}}: ${{document.documentElement.scrollWidth}}px wide, past ${{escaping().join(" ")}}`
  : "";
const typeScale = () =>
  getComputedStyle(document.documentElement).getPropertyValue("--type-scale").trim();
// A largest-size fixture that failed to inject would otherwise pass as scale 1.
const expectLargestScale = () => expect(typeScale() === {json.dumps(str(LARGEST_TYPE_SCALE))},
  `--type-scale is ${{typeScale()}}`);
const expectSmallestScale = () => expect(typeScale() === {json.dumps(str(SMALLEST_TYPE_SCALE))},
  `--type-scale is ${{typeScale()}}`);
// The UI spec's tap area: a target takes taps over at least TAP x TAP, and a tap on
// any point of its drawn box reaches it, so no neighbour's hit area covers it.
const TAP = {TAP_PX};
const lands = (el, x, y) => {{
  const hit = document.elementFromPoint(x, y);
  return !!hit && el.contains(hit);
}};
const named = (el) => {{
  if (!el) return "nothing";
  const text = (el.getAttribute("aria-label") || el.textContent).replace(/\\s+/g, " ").trim();
  const classes = [...el.classList].map((c) => "." + c).join("");
  return `${{el.tagName.toLowerCase()}}${{classes}} "${{text.slice(0, 24)}}"`;
}};
// The first point of a box, sampled every 4px and along its far edges, that misses `el`.
const missed = (el, left, right, top, bottom) => {{
  for (let x = left; ; x = Math.min(x + 4, right)) {{
    for (let y = top; ; y = Math.min(y + 4, bottom)) {{
      if (!lands(el, x, y)) return [x, y];
      if (y === bottom) break;
    }}
    if (x === right) break;
  }}
  return null;
}};
// An axis over TAP long is cut to the TAP around `centre`, inside what it reaches: a
// target needs TAP of its own, and a wide one's rounded ends are no part of that.
const tapSpan = (low, high, centre) => {{
  if (high - low + 1 <= TAP) return [low, high];
  const from = Math.min(Math.max(centre - TAP / 2, low), high + 1 - TAP);
  return [from, from + TAP - 1];
}};
// `el`'s box as far as no scrolling or clipping ancestor cuts it off.
const unclipped = (el) => {{
  let {{left, right, top, bottom}} = el.getBoundingClientRect();
  for (let box = el.parentElement; box; box = box.parentElement) {{
    if (getComputedStyle(box).overflowX === "visible") continue;
    const outer = box.getBoundingClientRect();
    const x = outer.left + box.clientLeft, y = outer.top + box.clientTop;
    left = Math.max(left, x);
    right = Math.min(right, x + box.clientWidth);
    top = Math.max(top, y);
    bottom = Math.min(bottom, y + box.clientHeight);
  }}
  return {{left, right, top, bottom, width: right - left, height: bottom - top}};
}};
// What is wrong with `el` as a tap target, or "". The box it owns is scanned out from
// its centre along both axes, then sampled whole, so a corner another target takes shows.
const tapProblem = (el) => {{
  el.scrollIntoView({{block: "center", inline: "center", behavior: "instant"}});
  const r = unclipped(el);
  const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  const reach = (dx, dy) => {{
    let n = 0;
    while (n < 4 * TAP && lands(el, cx + (n + 1) * dx, cy + (n + 1) * dy)) n++;
    return n;
  }};
  const left = cx - reach(-1, 0), right = cx + reach(1, 0);
  const top = cy - reach(0, -1), bottom = cy + reach(0, 1);
  const wide = Math.round(right - left + 1), high = Math.round(bottom - top + 1);
  const drawn = `${{Math.round(r.width)}}x${{Math.round(r.height)}}`;
  const size = `${{named(el)}}: drawn ${{drawn}}, taps ${{wide}}x${{high}}`;
  if (wide < TAP || high < TAP) {{
    const past = wide < TAP ? [right + 1, cy] : [cx, bottom + 1];
    return `${{size}}, then ${{named(document.elementFromPoint(...past))}} takes them`;
  }}
  // A rounded corner is outside the box a browser hit-tests, so the drawn box is
  // sampled as the cross its corners leave, and 1px in: a hit test snaps a fractional
  // point to a whole pixel, which can fall just outside an edge.
  const round = Math.min(parseFloat(getComputedStyle(el).borderTopLeftRadius) || 0,
    r.width / 2, r.height / 2);
  const [l, rr, t, b] = [r.left + 1, r.right - 1, r.top + 1, r.bottom - 1];
  const lost = missed(el, ...tapSpan(left, right, cx), ...tapSpan(top, bottom, cy))
    || missed(el, l + round, rr - round, t, b) || missed(el, l, rr, t + round, b - round);
  if (!lost) return "";
  return `${{size}}, but (${{lost}}) lands on ${{named(document.elementFromPoint(...lost))}}`;
}};
// Fails on each shown match of `selector` that is not a tap target, and when none shows.
const expectTapTargets = (selector) => {{
  const targets = $$(selector).filter(visible);
  expect(targets.length, `no ${{selector}} shows`);
  const problems = targets.map(tapProblem).filter(Boolean);
  expect(!problems.length, problems.join("; "));
}};
"""


def act_expression(check: Check, sabotaged: bool, prelude: str) -> str:
    """The page-side program run before the gestures: act, then maybe sabotage.

    What the act records in `ctx` is kept on the window for the script.
    """
    sabotage = check.sabotage if sabotaged else ""
    return f"""(async () => {{
{prelude}
const ctx = window.__probeCtx = {{}};
try {{ {{ {check.act} }} }} catch (error) {{
  return JSON.stringify({{ok: false, detail: `act: ${{error.message || error}}`}});
}}
try {{ {{ {sabotage} }} }} catch (error) {{
  return JSON.stringify({{ok: false, sabotageError: String(error.message || error)}});
}}
return JSON.stringify({{ok: true}});
}})()"""


def script_expression(check: Check, prelude: str) -> str:
    """The page-side program run after the gestures: assert."""
    return f"""(async () => {{
{prelude}
const ctx = window.__probeCtx || {{}};
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
// Input goes through CDP rather than in-page events: only a real pointer is one
// setPointerCapture accepts.
const touch = job.gestures.some((step) => step.pointer === 'touch');
const centre = async (selector) => {
  const box = await evaluate(`(() => {
    const el = document.querySelector(${JSON.stringify(selector)});
    if (!el) return null;
    el.scrollIntoView({ block: 'center' });
    const r = el.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  })()`);
  if (!box) throw new Error(`gesture: no element ${selector}`);
  return box;
};
const BUTTONS = { left: 1, right: 2, middle: 4 };
const mouse = (type, x, y, held, button = 'left') => call('Input.dispatchMouseEvent',
  { type, x, y, button: held || type !== 'mouseMoved' ? button : 'none',
    buttons: held ? BUTTONS[button] : 0, clickCount: type === 'mouseMoved' ? 0 : 1 });
const touchEvent = (type, touchPoints) => call('Input.dispatchTouchEvent', { type, touchPoints });
// Keyed by KEYS. Without `text` Chromium sends no keypress, and a focused button
// is activated by the keypress, not the keydown.
const KEY_EVENTS = {
  'Enter': { code: 'Enter', windowsVirtualKeyCode: 13, text: '\\r' },
  ' ': { code: 'Space', windowsVirtualKeyCode: 32, text: ' ' },
  'Tab': { code: 'Tab', windowsVirtualKeyCode: 9 },
};
const press = async (key) => {
  await call('Input.dispatchKeyEvent', { type: 'keyDown', key, ...KEY_EVENTS[key] });
  await call('Input.dispatchKeyEvent', { type: 'keyUp', key, ...KEY_EVENTS[key], text: undefined });
};
// Keyed by the POINTERS a Check accepts.
const input = {
  mouse: {
    press: async ({ x, y }, button) => {
      await mouse('mouseMoved', x, y, false);
      await mouse('mousePressed', x, y, true, button);
    },
    move: ({ x, y }, button) => mouse('mouseMoved', x, y, true, button),
    release: ({ x, y }, button) => mouse('mouseReleased', x, y, false, button),
  },
  touch: {
    press: ({ x, y }) => touchEvent('touchStart', [{ x, y, id: 1 }]),
    move: ({ x, y }) => touchEvent('touchMove', [{ x, y, id: 1 }]),
    release: () => touchEvent('touchEnd', []),
  },
};
const perform = async (gestures) => {
  let at = null, pointer = input.mouse, button = 'left';
  for (const step of gestures) {
    if ('key' in step) {
      await press(step.key);
    } else if ('hover' in step) {
      const { x, y } = await centre(step.hover);
      await mouse('mouseMoved', x, y, false);
    } else if ('press' in step) {
      at = await centre(step.press);
      pointer = input[step.pointer];
      button = step.button || 'left';
      await pointer.press(at, button);
    } else if ('move' in step) {
      const from = at, dy = step.dy || 0;
      for (let i = 1; i <= 10; i++) {
        at = { x: from.x + (step.move * i) / 10, y: from.y + (dy * i) / 10 };
        await pointer.move(at, button);
      }
    } else if ('release' in step) {
      await pointer.release(at, button);
    }
  }
};
await call('Page.enable');
await call('Network.enable');
// Fonts come from Google; the checks read layout, not type, and wait on nothing.
await call('Network.setBlockedURLs', { urls: ['*fonts.googleapis.com*', '*fonts.gstatic.com*'] });
// --window-size has a ~500px floor; the emulation override reaches a phone.
await call('Emulation.setDeviceMetricsOverride',
  { width: job.width, height: job.height, deviceScaleFactor: 1, mobile: false });
if (touch) await call('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 1 });
if (job.focused) await call('Emulation.setFocusEmulationEnabled', { enabled: true });
const results = [];
try {
  // Before the first navigation: a page reads matchMedia as it loads.
  if (job.media.length) {
    try {
      await call('Emulation.setEmulatedMedia', { features: job.media });
    } catch (error) {
      results.push({ ok: false, detail: error.message });
    }
  }
  for (const url of results.length ? [] : job.urls) {
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
    let result = JSON.parse(await evaluate(job.act));
    if (result.ok) {
      try {
        await perform(job.gestures);
        result = JSON.parse(await evaluate(job.script));
      } catch (error) {
        result = { ok: false, detail: error.message };
      }
    }
    results.push(result);
    for (const identifier of added) {
      await call('Page.removeScriptToEvaluateOnNewDocument', { identifier });
    }
    if (!result.ok) break;
  }
} finally {
  if (job.media.length) await call('Emulation.setEmulatedMedia', { features: [] });
  if (touch) await call('Emulation.setTouchEmulationEnabled', { enabled: false });
  if (job.focused) await call('Emulation.setFocusEmulationEnabled', { enabled: false });
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


def build_job(check: Check, base: str, sabotaged: bool, prelude: str) -> dict:
    """What the driver needs to run `check` against a fixture served at `base`."""
    preloads = [check.preload] if check.preload else []
    if sabotaged and check.sabotage_preload:
        preloads.append(check.sabotage_preload)
    return {
        "urls": [base + path for path in check.paths],
        "width": check.width,
        "height": HEIGHT,
        "preloads": preloads,
        "reload": check.reload,
        "act": act_expression(check, sabotaged, prelude),
        "gestures": list(check.gestures),
        "media": [{"name": name, "value": value} for name, value in check.media],
        "focused": check.focused,
        "script": script_expression(check, prelude),
    }


async def serve(app: web.Application) -> tuple[web.AppRunner, str]:
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    host, port = runner.addresses[0][:2]
    return runner, f"http://{host}:{port}"


async def drive(socket_url: str, job: dict) -> list[dict]:
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


async def run_check(
    check: Check,
    socket_url: str,
    sabotaged: bool,
    build_fixture: Callable[[str], Fixture],
    prelude: str,
) -> Outcome:
    """Serve a fresh fixture, drive the page through the check, then read the receipt."""
    fixture = build_fixture(check.fixture)
    if check.setup:
        check.setup(fixture)
    runner, base = await serve(fixture.app)
    try:
        try:
            results = await drive(socket_url, build_job(check, base, sabotaged, prelude))
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


def require_node() -> None:
    try:
        version = subprocess.run(
            ["node", "--version"], capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        version = ""
    match = re.match(r"v(\d+)", version.strip())
    if not match or int(match.group(1)) < MINIMUM_NODE:
        print(f"{Path(sys.argv[0]).name} needs Node 22+ (global WebSocket)", file=sys.stderr)
        raise SystemExit(2)


@contextlib.contextmanager
def chromium(browser: str) -> Iterator[str]:
    with tempfile.TemporaryDirectory(prefix="cdp-probe-chromium-") as profile:
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


async def run_all(
    checks: list[Check],
    socket_url: str,
    self_test: bool,
    build_fixture: Callable[[str], Fixture],
    prelude: str,
    before: Callable[[], Awaitable[None]] | None = None,
) -> bool:
    """Run every check, and under `self_test` run it sabotaged too; True when all held."""
    if before:
        await before()
    all_good = True
    for check in checks:
        clean = await run_check(check, socket_url, False, build_fixture, prelude)
        print(f"PASS {check.id}" if clean.ok else f"FAIL {check.id}: {clean.detail}", flush=True)
        all_good &= clean.ok
        if self_test:
            broken = await run_check(check, socket_url, True, build_fixture, prelude)
            caught = not broken.ok and not broken.sabotage_error
            detail = "" if caught else f": {broken.detail or 'passed while sabotaged'}"
            print(f"{'CAUGHT' if caught else 'MISSED'} {check.id}{detail}", flush=True)
            all_good &= caught
    return all_good
