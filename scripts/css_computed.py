#!/usr/bin/env python3
"""Record what Chromium actually computes for the digest pages, at each breakpoint.

The rule receipt in ``css_receipt.py`` compares declarations after sorting them,
which makes it blind to the two failures a shared CSS partial is most likely to
cause: a ``background`` shorthand reordered ahead of its ``background-image``
longhand, and a rule that moves across the ``@media`` block it used to follow.
Only a browser sees either, so this takes a second receipt from one.

Chromium is driven over the DevTools Protocol through a short Node script,
because the protocol needs a WebSocket client and Node 22+ ships one globally
while the Python standard library has none. No browser-automation package is
installed for this, and none may be.

Output is ``computed.json`` beside the pages, shaped like ``rules.json`` so that
``css_receipt.py compare`` reads both the same way. What it measures lives in
``css_probes.json`` beside this file; ``--probes`` overrides it.

This script must never be collected by pytest: it needs Chromium and a free
debug port, which makes it a non-hermetic gate rather than a test. Importing it
is safe, and ``tests/test_css_receipt.py`` does so to check ``BREAKPOINTS``.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

# Chromium ships no installer that puts it on PATH on macOS, so a bundle lookup
# is the fallback behind $CYRIS_CHROMIUM and --browser.
BROWSER_NAMES = ("chromium", "chromium-browser", "chrome", "google-chrome")
BROWSER_BUNDLES = (
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)

# The `@media` breakpoints each page declares. These are the templates' own
# literals restated, and nothing links the two copies at runtime, so
# `tests/test_css_receipt.py` reads them back out of the rendered pages: a
# breakpoint that moves without this map moving stops the gate sampling inside
# its media block, which is the one failure this second receipt exists to catch.
BREAKPOINTS = {"digest": (880, 720), "index": (720,), "raw": (720, 640)}

# One width above every breakpoint, then each breakpoint itself, then a phone. A
# rule that moved across its own @media block only shows up when a width inside
# that block is sampled.
WIDTHS = (1440, *sorted({w for ws in BREAKPOINTS.values() for w in ws}, reverse=True), 375)
HEIGHT = 900

# The promote buttons only get these classes once the page's script has probed
# /api/vote, which never answers under file://. Without forcing them the
# selectors match nothing and the comparison passes on absent data.
FORCED_CLASSES = {".promote-btn": ("done", "error")}

# Probing named selector/property pairs rather than dumping every property is
# deliberate: a full dump drags in content-sized layout values that drift with
# webfont loading, and reports the drift as a change. Which pairs is data, not a
# literal: the set that produced a verdict must be the set the gate keeps
# watching, and that is too large to read as code.
PROBES_PATH = Path(__file__).resolve().parent / "css_probes.json"


def load_probes(path: Path) -> dict[str, dict[str, list[str]]]:
    """Read the {page: {selector: [property, ...]}} set the receipt measures."""
    return json.loads(path.read_text(encoding="utf-8"))


PAGE_SCRIPT = """
(() => {
  const probes = __PROBES__;
  const forced = __FORCED__;
  for (const [base, classNames] of Object.entries(forced)) {
    const template = document.querySelector(base);
    if (!template || !template.parentElement) continue;
    for (const className of classNames) {
      if (document.querySelector(base + '.' + className)) continue;
      const clone = template.cloneNode(true);
      clone.classList.add(className);
      template.parentElement.appendChild(clone);
    }
  }
  const result = {};
  for (const [selector, properties] of Object.entries(probes)) {
    const element = document.querySelector(selector);
    if (!element) { result[selector] = null; continue; }
    const style = getComputedStyle(element);
    const values = {};
    for (const property of properties) {
      values[property] = style.getPropertyValue(property);
    }
    result[selector] = values;
  }
  return JSON.stringify(result);
})()
"""

CDP_SCRIPT = """
const socket = new WebSocket(process.env.CDP_WS);
const pending = new Map();
let nextId = 0;
socket.addEventListener('message', (event) => {
  const message = JSON.parse(event.data);
  const resolve = pending.get(message.id);
  if (resolve) { pending.delete(message.id); resolve(message); }
});
const call = (method, params) => new Promise((resolve) => {
  const id = ++nextId;
  pending.set(id, resolve);
  socket.send(JSON.stringify({ id, method, params }));
});
const evaluate = (expression) =>
  call('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
await new Promise((resolve, reject) => {
  socket.addEventListener('open', resolve);
  socket.addEventListener('error', reject);
});
// Attaching can race past Page.loadEventFired, which then never arrives.
for (let attempt = 0; attempt < 400; attempt++) {
  const state = await evaluate('document.readyState');
  if (state.result?.result?.value === 'complete') break;
  await new Promise((resolve) => setTimeout(resolve, 25));
}
const widths = JSON.parse(process.env.CDP_WIDTHS);
const height = Number(process.env.CDP_HEIGHT);
const measured = {};
for (const width of widths) {
  // --window-size has a ~500px floor on macOS, so it cannot reach the phone
  // breakpoint at all; the emulation override is the only honest width.
  await call('Emulation.setDeviceMetricsOverride', {
    width, height, deviceScaleFactor: 1, mobile: false,
  });
  const answer = await evaluate(process.env.CDP_EXPRESSION);
  if (answer.result?.exceptionDetails) {
    console.error(JSON.stringify(answer.result.exceptionDetails));
    process.exit(1);
  }
  measured[String(width)] = JSON.parse(answer.result.result.value);
}
process.stdout.write(JSON.stringify(measured));
socket.close();
"""


def find_browser(explicit: str | None) -> str:
    """Resolve the Chromium binary from the flag, the environment, PATH, then bundles."""
    candidate = explicit or os.environ.get("CYRIS_CHROMIUM")
    if candidate:
        return candidate
    for name in BROWSER_NAMES:
        if found := shutil.which(name):
            return found
    for bundle in BROWSER_BUNDLES:
        if Path(bundle).exists():
            return bundle
    raise SystemExit("no Chromium found; pass --browser or set CYRIS_CHROMIUM")


def _devtools_port(profile_dir: Path, deadline: float) -> int:
    """Read the port Chromium chose, which it writes once the endpoint is live."""
    port_file = profile_dir / "DevToolsActivePort"
    while time.monotonic() < deadline:
        if port_file.exists() and (text := port_file.read_text(encoding="utf-8")).strip():
            return int(text.splitlines()[0])
        time.sleep(0.05)
    raise SystemExit("Chromium did not open a DevTools port")


def _page_socket(port: int, deadline: float) -> str:
    """Find the WebSocket URL of the page target Chromium opened at startup."""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/list", timeout=5
            ) as response:
                targets = json.load(response)
        except OSError:
            # The port file lands before the HTTP endpoint accepts connections,
            # and that window is what the deadline is here to wait out.
            targets = []
        for target in targets:
            if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                return target["webSocketDebuggerUrl"]
        time.sleep(0.05)
    raise SystemExit("Chromium exposed no page target")


def _page_script(probes: dict[str, list[str]]) -> str:
    forced = {base: list(names) for base, names in FORCED_CLASSES.items()}
    return PAGE_SCRIPT.replace("__PROBES__", json.dumps(probes)).replace(
        "__FORCED__", json.dumps(forced)
    )


def _run_cdp(socket_url: str, probes: dict[str, list[str]], widths: tuple[int, ...]) -> dict:
    environment = {
        **os.environ,
        "CDP_WS": socket_url,
        "CDP_EXPRESSION": _page_script(probes),
        "CDP_WIDTHS": json.dumps(list(widths)),
        "CDP_HEIGHT": str(HEIGHT),
    }
    process = subprocess.run(
        ["node", "--input-type=module", "-e", CDP_SCRIPT],
        capture_output=True,
        text=True,
        env=environment,
        timeout=180,
    )
    if process.returncode != 0:
        raise SystemExit(f"DevTools evaluation failed: {process.stderr.strip()}")
    return json.loads(process.stdout)


def measure_page(
    page: Path, probes: dict[str, list[str]], browser: str, widths: tuple[int, ...]
) -> dict:
    """Open one local page in headless Chromium and probe it at every width."""
    with tempfile.TemporaryDirectory(prefix="css-computed-") as profile:
        profile_dir = Path(profile)
        process = subprocess.Popen(
            [
                browser,
                "--headless",
                # A separate profile is what forces a real launch: without it an
                # already-running Chromium just takes the URL and honours no flags.
                f"--user-data-dir={profile_dir}",
                "--remote-debugging-port=0",
                # Webfonts arrive over the network, so their metrics differ run to
                # run; blocking them is what makes content-sized tracks comparable.
                "--disable-remote-fonts",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-gpu",
                page.resolve().as_uri(),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 60
            port = _devtools_port(profile_dir, deadline)
            return _run_cdp(_page_socket(port, deadline), probes, widths)
        finally:
            process.terminate()
            process.wait(timeout=30)


def receipt(
    snapshot_dir: Path,
    probes_by_page: dict[str, dict[str, list[str]]],
    browser: str,
    widths: tuple[int, ...],
) -> dict[str, dict[str, object]]:
    """Build a page-keyed receipt shaped like ``rules.json`` so ``compare`` reads both."""
    measured: dict[str, dict[str, object]] = {}
    for page_name, probes in sorted(probes_by_page.items()):
        page = snapshot_dir / f"{page_name}.html"
        if not page.exists():
            raise SystemExit(f"{page}: not found; run `css_receipt.py snapshot` first")
        by_width = measure_page(page, probes, browser, widths)
        measured[page_name] = {
            f"@{width} | {selector}": values
            for width, selectors in by_width.items()
            for selector, values in selectors.items()
        }
        # A selector that matches nothing would otherwise be recorded as null in
        # both snapshots and compare equal, passing the gate on absent data.
        unmatched = sorted(key for key, value in measured[page_name].items() if value is None)
        if unmatched:
            raise SystemExit(f"{page}: probes matched no element: {', '.join(unmatched)}")
    return measured


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot_dir", type=Path, help="directory `snapshot` wrote")
    parser.add_argument("--browser", default=None, help="Chromium binary (else $CYRIS_CHROMIUM)")
    parser.add_argument(
        "--probes",
        type=Path,
        default=None,
        help=f"JSON of {{page: {{selector: [property, ...]}}}} replacing {PROBES_PATH.name}",
    )
    args = parser.parse_args()

    probes_by_page = load_probes(args.probes or PROBES_PATH)

    measured = receipt(args.snapshot_dir, probes_by_page, find_browser(args.browser), WIDTHS)
    destination = args.snapshot_dir / "computed.json"
    destination.write_text(json.dumps(measured, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    probed = sum(len(page) for page in measured.values())
    print(f"{destination}: {probed} probes across {len(WIDTHS)} widths", file=sys.stderr)


if __name__ == "__main__":
    main()
