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
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from aiohttp import web
from css_computed import _devtools_port, _page_socket

CHECK_TIMEOUT_S = 30
POINTERS = ("mouse", "touch")
HEIGHT = 900
MINIMUM_NODE = 22


class Fixture(Protocol):
    """What the core needs from a probe's fixture: the app to serve."""

    app: web.Application


@dataclass(frozen=True)
class Check:
    """One behaviour, read from the DOM and, where something was written, the fixture.

    `act` performs the steps and may record observations in `ctx`; `sabotage`
    runs after it in a self-test; `gestures` are then sent as real browser
    input; `script` then asserts with `expect`. A sabotage that cannot be
    applied through the page is a `sabotage_preload` instead, installed before
    the page loads.

    `media` names CSS media features the browser reports for the whole check,
    such as `(("prefers-reduced-motion", "reduce"),)`.

    A gesture step is `{"press": selector, "pointer": "mouse" | "touch"}`,
    `{"move": dx}`, `{"release": True}` or `{"hover": selector}`; a move or a
    release belongs to the press before it.
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

    def __post_init__(self) -> None:
        pressed = False
        for step in self.gestures:
            keys = set(step)
            if keys == {"press", "pointer"} and step["pointer"] in POINTERS:
                pressed = True
            elif keys == {"move"} and isinstance(step["move"], int) and pressed:
                pass
            elif keys == {"release"} and pressed:
                pressed = False
            elif keys != {"hover"}:
                raise ValueError(f"{self.id}: gesture step {step} is unknown or has no press")

    @property
    def paths(self) -> tuple[str, ...]:
        return (self.path,) if isinstance(self.path, str) else self.path


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
const mouse = (type, x, y, held) => call('Input.dispatchMouseEvent',
  { type, x, y, button: held || type !== 'mouseMoved' ? 'left' : 'none',
    buttons: held ? 1 : 0, clickCount: type === 'mouseMoved' ? 0 : 1 });
const perform = async (gestures) => {
  let at = null, pointer = 'mouse';
  for (const step of gestures) {
    if ('hover' in step) {
      const { x, y } = await centre(step.hover);
      await mouse('mouseMoved', x, y, false);
    } else if ('press' in step) {
      at = await centre(step.press);
      pointer = step.pointer;
      if (pointer === 'touch') {
        await call('Input.dispatchTouchEvent',
          { type: 'touchStart', touchPoints: [{ x: at.x, y: at.y, id: 1 }] });
      } else {
        await mouse('mouseMoved', at.x, at.y, false);
        await mouse('mousePressed', at.x, at.y, true);
      }
    } else if ('move' in step) {
      const from = at.x;
      for (let i = 1; i <= 10; i++) {
        at = { x: from + (step.move * i) / 10, y: at.y };
        if (pointer === 'touch') {
          await call('Input.dispatchTouchEvent',
            { type: 'touchMove', touchPoints: [{ x: at.x, y: at.y, id: 1 }] });
        } else {
          await mouse('mouseMoved', at.x, at.y, true);
        }
      }
    } else if ('release' in step) {
      if (pointer === 'touch') {
        await call('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
      } else {
        await call('Input.dispatchMouseEvent',
          { type: 'mouseReleased', x: at.x, y: at.y, button: 'left', buttons: 0, clickCount: 1 });
      }
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
