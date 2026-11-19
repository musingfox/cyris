#!/usr/bin/env python3
"""Walk /onboarding on a real server in headless Chromium, through to the first issue.

The page probes (`onboarding_probe.py`, `settings_probe.py`) serve the page over
in-memory stores and a stand-in for `POST /run`. This drives what they stand in
for: given the URL of a deployment whose `sources` table is empty, it presses
through every step as a reader would, adds the feeds it is given by address,
starts the first issue, waits for it to end, and reads the store.

It exits 0 only when the store holds at least one `sources` row and the first
issue's `digest_runs` row has `status=ok`. Anything else exits non-zero and names
the step or receipt, with the last browser console errors. It refuses to start
on a store whose `sources` is not empty, or a site that says its own are not,
because the wizard would not be walking a new install. The walk writes to the
site and the verdict reads the store, so its language step saves a language and
timezone neither held, and no other write reaches the site until the store holds
them. It judges page actions and store receipts, never what a model wrote.

`--skip-feed-checker` is for `e2e_onboarding_local.py` alone. The real check refuses a
local address by design, so a loopback feed can only get past it by the browser answering
in its place. A run against a URL with a public feed never uses it, and exercises the real check.

The browser half, which knows no page, is `cdp_probe.py`.

This script must never be collected by pytest: it needs Chromium, Node and a
running deployment, which makes it an operator-run gate rather than a test.
Importing it is safe, and `tests/test_onboarding_walk.py` does so to check its
store modes and its driver.
"""

import argparse
import asyncio
import contextlib
import io
import json
import os
import sys
import zoneinfo
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import feedparser
import httpx
from cdp_probe import CDP_SESSION, Check, base_prelude, chromium, require_node, script_expression
from css_computed import find_browser

from cyris.adapters.fetch.feed_check import unique_name
from cyris.adapters.store.d1 import D1Client, D1Error, D1Queryable
from cyris.adapters.store.local_d1 import LocalD1Client

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2

PAGE = "/onboarding"

# Past the page's own 15 minutes (RUN_WAIT_MS in static/onboarding.js, which is the run
# container's cap), so a run that never ends is the page's verdict and not this script's.
FIRST_ISSUE_TIMEOUT_S = 20 * 60

# A read a page waits on, across a container waking up.
LOAD_S = 60
# One Next: the Model step's makes a real provider call of up to 30 seconds per route it tries.
SAVE_S = 120
# Past the server's own CHECK_DEADLINE_SECONDS (60), which answers with a refusal of its own.
FEED_CHECK_S = 90
# The step's own words are "a minute or two"; a large pick polls in batches of 20 seconds.
CARDS_S = 300
# What every step but the first issue and the feed checks can take in all, as the kill behind
# their own limits, each of which fails its step first and with a reason.
WALK_MARGIN_S = 30 * 60
# What a driver that has stopped the walk takes to fail what it holds and end, past which it
# is killed.
HALT_S = 10

# The last errors a failure prints: the newest are nearest the failure.
CONSOLE_ERRORS_KEPT = 10
CONSOLE_ERROR_MAX_CHARS = 500

# What the reader answers first on the language and schedule steps. Each is passed over for
# the next the page offers while --url or the store already holds it, so the language step's
# save is always sent, and what it writes is this run's own on both sides.
READER_LANGUAGE = "en"
READER_TIMEZONE = "UTC"
READER_HOURS = (8, 20)
HOUR_FIELDS = ("#o-morning", "#o-evening")
LANGUAGE_KEY = "digest.output_language"
TIMEZONE_KEY = "general.timezone"
SCHEDULE_KEY = "general.digest_schedule"

ACCESS_HEADERS = {
    "CF-Access-Client-Id": "CYRIS_ACCESS_CLIENT_ID",
    "CF-Access-Client-Secret": "CYRIS_ACCESS_CLIENT_SECRET",
}

FEED_CHECK_PATH = "/api/onboarding/feed-check"
# What the wizard page itself reads to decide whether this is a new install.
SITE_SOURCES_PATH = "/api/onboarding/feeds"
# What it reads the stored settings and the languages it offers from.
SITE_SETTINGS_PATH = "/api/settings"
# The language step's save, the one write let through before the store is found to hold it.
LANGUAGE_SAVE = {"step": "language", "path": "/api/settings/values"}
# Where `--skip-feed-checker` is allowed: the addresses the real check refuses and this
# machine answers. Anything else can reach the real check, so nothing may stand in for it.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
FEED_READ_S = 30

# What `--break` answers in the browser, so the fault needs no switch in production code.
BREAKS = {
    "feed-check": {
        "path": FEED_CHECK_PATH,
        "status": 500,
        "body": json.dumps({"ok": False, "error": "--break feed-check answered this request."}),
    },
}


@dataclass(frozen=True)
class Choice:
    """What the walk answers on the language and schedule steps."""

    language: str
    timezone: str
    hours: tuple[int, int]

    @property
    def saved(self) -> dict[str, str]:
        """What the language step's save writes."""
        return {LANGUAGE_KEY: self.language, TIMEZONE_KEY: self.timezone}


FIRST_CHOICE = Choice(READER_LANGUAGE, READER_TIMEZONE, READER_HOURS)


def schedule_of(hours: tuple[int, int]) -> list[str]:
    """The publish hours as the store holds them."""
    return [f"{hour:02d}:00" for hour in hours]


def choose(
    languages: list[str], site: Mapping[str, object], store: Mapping[str, object], flag: str
) -> Choice:
    """The first answer to each step's settings that neither the site nor the store holds.

    `languages` are the tags the page offers. Raises ValueError naming a setting the page
    offers no such answer for.
    """

    def first(key: str, label: str, offered: list, held_as: Callable = lambda option: option):
        held = (site.get(key), store.get(key))
        for option in offered:
            if held_as(option) not in held:
                return option
        raise ValueError(
            f"the walk needs {label} that neither --url ({json.dumps(held[0])}) nor {flag} "
            f"({json.dumps(held[1])}) holds, so that the store can show the language step's "
            f"save as this run's, and the page offers no other ({len(offered)} offered)"
        )

    offered_languages = [READER_LANGUAGE] if READER_LANGUAGE in languages else []
    offered_languages = list(dict.fromkeys([*offered_languages, *languages]))
    # The page lists the zones its browser knows; the server takes any its tz database does.
    zones = [READER_TIMEZONE, *sorted(zoneinfo.available_timezones() - {READER_TIMEZONE})]
    # The first choice an hour later each time: two sides hold two pairs at most.
    hours = [tuple(sorted((hour + shift) % 24 for hour in READER_HOURS)) for shift in range(24)]
    return Choice(
        language=first(LANGUAGE_KEY, "an output language", offered_languages),
        timezone=first(TIMEZONE_KEY, "a timezone", zones),
        hours=first(SCHEDULE_KEY, "a pair of publish hours", hours, schedule_of),
    )


def wizard_prelude(feeds: list[str], first_issue_s: int, choice: Choice) -> str:
    """The page-side helpers of this walk, on top of `base_prelude`."""
    return f"""
const FEEDS = {json.dumps(feeds)};
const READER_LANGUAGE = {json.dumps(choice.language)};
const READER_TIMEZONE = {json.dumps(choice.timezone)};
const READER_HOURS = {json.dumps(dict(zip(HOUR_FIELDS, choice.hours, strict=True)))};
const LOAD_MS = {LOAD_S * 1000};
const SAVE_MS = {SAVE_S * 1000};
const FEED_CHECK_MS = {FEED_CHECK_S * 1000};
const CARDS_MS = {CARDS_S * 1000};
const FIRST_ISSUE_MS = {first_issue_s * 1000};
const hash = () => location.hash.slice(1);
const press = async (selector, ms = LOAD_MS) => {{
  const el = await waitFor(() => {{
    const found = $(selector);
    return visible(found) && !found.disabled && found;
  }}, `${{selector}} to take a press`, ms);
  el.click();
}};
const enter = (el, value) => {{
  el.value = value;
  el.dispatchEvent(new Event("input", {{bubbles: true}}));
  el.dispatchEvent(new Event("change", {{bubbles: true}}));
}};
const refusal = (selector) => {{
  const notice = $(selector);
  return notice && !notice.hidden && notice.classList.contains("err")
    ? notice.textContent.trim() : "";
}};
// The hash alone is not arrival: it changes before the page's own hashchange draws the step.
const arrive = async (to, shown, ms) => {{
  await waitFor(() => (hash() === to && visible($(shown))) || refusal("#o-result"), `${{to}}`, ms);
  expect(hash() === to, refusal("#o-result"));
}};
const next = async (to, shown, ms = SAVE_MS) => {{
  await press("#o-next");
  await arrive(to, shown, ms);
}};
"""


def _step(step_id: str, script: str) -> Check:
    return Check(id=step_id, fixture="deployment", path=PAGE, script=script)


STEPS = (
    _step(
        "opening",
        """
expect(location.pathname === "/onboarding", `the page is ${location.href}`);
if (!visible($("#o-show"))) await press("#o-skip-intro");
await press("#o-show");
await waitFor(() => visible($('.step[data-step="language"]')), "the language step");
""",
    ),
    _step(
        "language",
        """
await waitFor(() => !$(".step [data-loading]"), "the settings to load", LOAD_MS);
expect(!refusal("#o-result"), refusal("#o-result"));
const answer = (selector, value, what) => {
  enter($(selector), value);
  expect($(selector).value === value, `no ${what} ${value} is offered`);
};
answer("#o-output-language", READER_LANGUAGE, "language");
answer("#o-timezone", READER_TIMEZONE, "timezone");
await next("schedule", '.step[data-step="schedule"]');
""",
    ),
    _step(
        "schedule",
        """
for (const [selector, hour] of Object.entries(READER_HOURS)) enter($(selector), hour);
await next("sources", '.step[data-step="sources"]');
""",
    ),
    _step(
        "sources",
        """
await waitFor(() => !$("#o-feeds-loading"), "the feed list to load", LOAD_MS)
  .catch((error) => { throw new Error($("#o-feeds-loading")?.textContent || error.message); });
expect(!visible($("#o-feeds-had")), "the wizard says this deployment already has sources");
await next("sources/feeds", "#o-feeds-screen");
for (const url of FEEDS) {
  enter($("#o-own-url"), url);
  await press("#o-own-add");
  await waitFor(() => {
    const notice = $("#o-own-result");
    return !notice.hidden && !notice.hasAttribute("aria-busy");
  }, `the check of ${url}`, FEED_CHECK_MS);
  expect(!refusal("#o-own-result"), `feed check of ${url}: ${refusal("#o-own-result")}`);
}
await next("votes", '.step[data-step="votes"]');
""",
    ),
    _step(
        "votes",
        """
const loading = $("#o-cards-loading");
await waitFor(() => loading.hidden || !loading.hasAttribute("aria-busy"), "the cards", CARDS_MS);
expect(loading.hidden, loading.textContent.trim());
$('#o-cards [data-vote="up"]')?.click();
await next("notify", '.step[data-step="notify"]');
""",
    ),
    _step(
        "notify",
        """
await next("model", '.step[data-step="model"]');
""",
    ),
    _step(
        "model",
        """
await next("first-issue", '.step[data-step="first-issue"]');
""",
    ),
    _step(
        "first-issue",
        """
await waitFor(() => $("#o-issue-loading").hidden, "the first issue's plan", LOAD_MS);
expect(visible($("#o-start")), refusal("#o-result") || $("#o-issue-plan").textContent.trim());
await press("#o-start");
""",
    ),
    _step(
        "first-issue-ends",
        """
// Read from the controls the page shows, not their words: past the language step the page
// is worded in the language it saved. A failed issue alone shows Start beside Finish.
const ending = await waitFor(() => {
  const start = $("#o-start"), notice = $("#o-result");
  if (visible(start) && visible($("#o-finish"))) return "failed";
  if (visible(start) || !visible($("#o-back")) || !visible($("#o-finish"))) return "";
  if (notice.hidden) return "closed";
  return notice.hasAttribute("aria-busy") ? "" : "done";
}, "the first issue to end", FIRST_ISSUE_MS);
const said = $("#o-result").textContent.trim() || $("#o-issue-plan").textContent.trim();
expect(ending === "done", said);
""",
    ),
)

STEP_IDS = ("open", *(step.id for step in STEPS))

# What a step the page reloads under must show in the document that replaces its own, which
# its program died with. A language save that changes the catalog the page is worded from
# opens the next step in a reload (`reloadOn` in static/onboarding.js), so the language step
# arrives at the schedule step there. A reload under any other step fails it.
ARRIVALS = {
    "language": """
await arrive("schedule", '.step[data-step="schedule"]', LOAD_MS);
""",
}

# One long-lived session rather than one per step: the extra headers, the interception and
# the console subscription all last only as long as the session that asked for them.
WALK_DRIVER = (
    CDP_SESSION
    + """
const report = (event) => process.stdout.write(JSON.stringify(event) + '\\n');
const { createInterface } = await import('node:readline');
const input = createInterface({ input: process.stdin });
const lines = input[Symbol.asyncIterator]();
const job = JSON.parse((await lines.next()).value);
const deployment = new URL(job.url).origin;
const brief = (text) => String(text).slice(0, job.consoleMaxChars);
// Until the caller has found the language step's save in the store the verdict reads, that
// save is the one write let through to the site, so a --url and a store naming two installs
// stop there with nothing else written.
let walking = null;
let saveSent = false;
let checked = false;
let stopped = false;
let halt;
const halted = new Promise((resolve) => { halt = resolve; });
// Failed rather than left paused: a session that ends lets go on what it has paused.
const stop = async (requestId, event) => {
  stopped = true;
  await call('Fetch.failRequest', { requestId, errorReason: 'BlockedByClient' }).catch(() => {});
  if (event) report(event);
  halt();
};
const isSave = (url, method) => url.origin === deployment && method === 'POST'
  && url.pathname === job.save.path;
const sent = async (params) => {
  const { requestId, request } = params;
  if (stopped) return stop(requestId);
  const url = new URL(request.url);
  const rule = job.intercepts.find((r) => url.pathname === r.path);
  if (rule) {
    report({ intercepted: rule.path });
    // In `--feed` order: the walk waits for each check before it starts the next.
    if (rule.answers && rule.answers.length === 0) {
      return stop(requestId, { unanswered: rule.path });
    }
    const body = rule.answers ? JSON.stringify(rule.answers.shift()) : rule.body;
    await call('Fetch.fulfillRequest', {
      requestId,
      responseCode: rule.status,
      responseHeaders: [{ name: 'Content-Type', value: 'application/json' }],
      body: Buffer.from(body).toString('base64'),
    });
    return;
  }
  const own = url.origin === deployment;
  if (own && request.method !== 'GET' && !checked) {
    if (saveSent || walking !== job.save.step || !isSave(url, request.method)) {
      return stop(requestId, { blocked: { method: request.method, url: request.url } });
    }
    saveSent = true;
  }
  const headers = { ...request.headers, ...(own ? job.headers : {}) };
  // A request the page has dropped since it paused cannot be continued, and nothing
  // waits on it; a live one left paused fails its step on that step's own limit.
  await call('Fetch.continueRequest', Object.keys(job.headers).length
    ? { requestId, headers: Object.entries(headers).map(([name, value]) => ({ name, value })) }
    : { requestId });
};
const answered = async (params) => {
  const { requestId, request } = params;
  if (stopped) return stop(requestId);
  if (params.responseErrorReason) {
    await call('Fetch.continueRequest', { requestId });
    return;
  }
  // A redirect has no body to read back, and following it would send the request again. Its
  // status alone makes it one: the protocol may pause a response without its headers.
  if (Math.floor(params.responseStatusCode / 100) === 3) {
    const location = (params.responseHeaders ?? []).find(({ name }) => /^location$/i.test(name));
    return stop(requestId, { redirected: {
      method: request.method, url: request.url,
      status: params.responseStatusCode, location: location?.value ?? null,
    } });
  }
  const { body, base64Encoded } = await call('Fetch.getResponseBody', { requestId });
  const text = base64Encoded ? Buffer.from(body, 'base64').toString() : body;
  let answer = null;
  try { answer = JSON.parse(text); } catch {}
  if (saveSent && !checked && answer?.ok && isSave(new URL(request.url), request.method)) {
    report({ saved: { path: job.save.path, answer } });
    if ((await lines.next()).value !== 'go') return stop(requestId);
    checked = true;
  }
  await call('Fetch.fulfillRequest', {
    requestId,
    responseCode: params.responseStatusCode,
    // The body read back is decoded, and an edge may have compressed what it sent.
    responseHeaders: (params.responseHeaders ?? []).filter(
      ({ name }) => !/^content-(encoding|length)$/i.test(name)),
    body: Buffer.from(text).toString('base64'),
  });
};
socket.addEventListener('message', (event) => {
  const { method, params } = JSON.parse(event.data);
  if (method === 'Fetch.requestPaused') {
    const atResponse = params.responseStatusCode !== undefined || params.responseErrorReason;
    (atResponse ? answered : sent)(params).catch(() => {});
  } else if (method === 'Runtime.exceptionThrown') {
    const { exception, text } = params.exceptionDetails;
    report({ console: brief(exception?.description ?? text) });
  } else if (method === 'Runtime.consoleAPICalled' && params.type === 'error') {
    report({ console: brief(params.args.map((a) => a.value ?? a.description).join(' ')) });
  } else if (method === 'Log.entryAdded' && params.entry.level === 'error'
      && !params.entry.text.includes('ERR_BLOCKED_BY_CLIENT')) {
    report({ console: brief(`${params.entry.text} ${params.entry.url ?? ''}`.trim()) });
  } else if (method === 'Network.requestWillBeSent' && params.request.method === 'POST') {
    const url = new URL(params.request.url);
    if (url.pathname === '/run') report({ run: Object.fromEntries(url.searchParams) });
  }
});
await call('Page.enable');
await call('Network.enable');
await call('Runtime.enable');
await call('Log.enable');
await blockFonts();
// Every request pauses, so each is judged by its own origin and method: a write waits on
// the store check, and extra headers set once for the session would go to every host, a
// redirect's next hop included, such as Access's login page or an identity provider when
// the token is refused. A hop pauses again with its own URL.
await call('Fetch.enable', { patterns: [
  { urlPattern: `${deployment}${job.save.path}`, requestStage: 'Response' },
  ...job.intercepts.map((rule) => ({ urlPattern: `*${rule.path}` })),
  { urlPattern: '*' },
] });
// What Runtime.evaluate answers when the document it runs in is replaced under it.
const LEFT = /navigated or closed|context was destroyed/i;
// A step the page reloaded under, judged in the new document once it has loaded.
const arrived = async (step) => {
  try {
    const page = JSON.stringify(new URL(job.url).pathname);
    await settled(`window.__walkLeft === undefined && location.pathname === ${page}`);
    return JSON.parse(await evaluate(step.arrival));
  } catch (error) {
    return { ok: false, detail: `the page reloaded under this step: ${error.message}` };
  }
};
const walk = async () => {
  try {
    const { errorText } = await call('Page.navigate', { url: job.url });
    if (errorText) throw new Error(`${job.url}: ${errorText}`);
    await settled(`location.origin === ${JSON.stringify(new URL(job.url).origin)}`);
    report({ step: 'open', ok: true });
  } catch (error) {
    report({ step: 'open', ok: false, detail: error.message });
    return;
  }
  for (const step of job.steps) {
    walking = step.id;
    let result;
    try {
      // Marks the document the step runs in, so the one that replaces it is told apart.
      if (step.arrival) await evaluate('window.__walkLeft = true');
      result = JSON.parse(await evaluate(step.expression));
    } catch (error) {
      result = step.arrival && LEFT.test(error.message)
        ? await arrived(step) : { ok: false, detail: error.message };
    }
    report({ step: step.id, ...result });
    if (!result.ok) return;
  }
};
await Promise.race([walk(), halted]);
socket.close();
input.close();
process.stdin.destroy();
"""
)


@dataclass
class Walk:
    """What the driver has said so far, printed as it comes: a real walk takes minutes."""

    passed: list[str] = field(default_factory=list)
    failure: str = ""
    console: deque[str] = field(default_factory=lambda: deque(maxlen=CONSOLE_ERRORS_KEPT))
    run: dict[str, str] | None = None
    # The store's newest digest_runs id, read as the page starts its run: a run of the same
    # period that ends earlier in the walk, a catch-up or a scheduled one, is not the issue.
    newest_run: Callable[[], int | str] | None = None
    run_after: int | str | None = None

    def take(self, event: dict) -> None:
        if "console" in event:
            self.console.append(event["console"])
        elif "run" in event:
            self.run = event["run"]
            if self.newest_run:
                self.run_after = self.newest_run()
            print(f"RUN started: {json.dumps(self.run)}", flush=True)
        elif "intercepted" in event:
            print(f"INTERCEPTED {event['intercepted']}", flush=True)
        elif "saved" in event:
            print(f"SAVED through {event['saved']['path']}: checking the store", flush=True)
        elif "redirected" in event:
            redirected = event["redirected"]
            self.fail(
                self.waiting_on,
                f"--url answered {redirected['method']} {redirected['url']} with "
                f"{redirected['status']}, a redirect to {redirected['location'] or 'nowhere'}: "
                "the walk follows no redirect of an answer it holds, so it stopped there",
            )
        elif "unanswered" in event:
            self.fail(
                self.waiting_on,
                f"the page asked {event['unanswered']} more often than --skip-feed-checker has "
                "answers, one per --feed, so the walk had none for it and stopped there",
            )
        elif "blocked" in event:
            blocked = event["blocked"]
            self.fail(
                self.waiting_on,
                f"the page sent {blocked['method']} {blocked['url']} before the store was found "
                "to hold the language step's save, the one write let through until then, so it "
                "was stopped there and never sent",
            )
        elif event["ok"]:
            self.passed.append(event["step"])
            print(f"PASS {event['step']}", flush=True)
        else:
            self.fail(event["step"], event["detail"])

    def fail(self, step: str, detail: str) -> None:
        """The first failure is the walk's; what a stop sets off after it is not."""
        if self.failure:
            return
        self.failure = f"step {step}: {detail}"
        print(f"FAIL {self.failure}", flush=True)

    @property
    def waiting_on(self) -> str:
        """The step no answer has come for, which is where a dead driver stopped."""
        return next((step for step in STEP_IDS if step not in self.passed), "the end of the walk")

    @property
    def finished(self) -> bool:
        return not self.failure and self.waiting_on not in STEP_IDS


def build_job(
    url: str,
    feeds: list[str],
    first_issue_s: int,
    headers: dict[str, str],
    broken: str | None,
    feed_check_answers: list[dict] | None = None,
    choice: Choice = FIRST_CHOICE,
) -> dict:
    """What the driver needs to walk the wizard at `url`."""
    prelude = base_prelude() + wizard_prelude(feeds, first_issue_s, choice)
    intercepts = [BREAKS[broken]] if broken else []
    if feed_check_answers:
        intercepts.append({"path": FEED_CHECK_PATH, "status": 200, "answers": feed_check_answers})
    return {
        "url": f"{url.rstrip('/')}{PAGE}",
        "headers": headers,
        "save": LANGUAGE_SAVE,
        "intercepts": intercepts,
        "consoleMaxChars": CONSOLE_ERROR_MAX_CHARS,
        "steps": [
            {
                "id": step.id,
                "expression": script_expression(step, prelude),
                **(
                    {"arrival": script_expression(_step(step.id, ARRIVALS[step.id]), prelude)}
                    if step.id in ARRIVALS
                    else {}
                ),
            }
            for step in STEPS
        ],
    }


async def drive_walk(
    socket_url: str,
    job: dict,
    budget_s: int,
    walk: Walk,
    confirm: Callable[[dict], str] = lambda saved: "",
) -> None:
    """Walk the job; `confirm` is shown the language step's save and answers why the walk must
    stop there."""
    process = await asyncio.create_subprocess_exec(
        "node",
        "--input-type=module",
        "-e",
        WALK_DRIVER,
        env={**os.environ, "CDP_WS": socket_url},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def say(line: str) -> None:
        # A driver that died on the socket leaves a closed pipe, and its stderr says why.
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            process.stdin.write(f"{line}\n".encode())
            await process.stdin.drain()

    await say(json.dumps(job))

    def kill() -> None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()

    halting: list[asyncio.TimerHandle] = []

    async def events() -> None:
        async for line in process.stdout:
            event = json.loads(line)
            walk.take(event)
            if "saved" in event:
                stop = await asyncio.to_thread(confirm, event["saved"])
                if stop:
                    walk.fail(walk.waiting_on, stop)
                # The driver fails a save told to stop, so the page never reads it as stored.
                await say("stop" if stop else "go")
            if walk.failure and not halting:
                halting.append(asyncio.get_running_loop().call_later(HALT_S, kill))

    try:
        _, stderr, _ = await asyncio.wait_for(
            asyncio.gather(events(), process.stderr.read(), process.wait()), budget_s
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        walk.fail(walk.waiting_on, f"no end after {budget_s} seconds")
        return
    finally:
        process.stdin.close()
        for handle in halting:
            handle.cancel()
    if process.returncode != 0 and not walk.failure:
        walk.fail(walk.waiting_on, f"the driver failed: {stderr.decode().strip()}")
    elif not walk.finished and not walk.failure:
        walk.fail(walk.waiting_on, "the driver ended before this step answered")


def read_feed_check_answers(feeds: list[str]) -> list[dict]:
    """The answer the feed check gives each feed: its own title and its address.

    Read from the feed itself, so the source the wizard stores is named as the real check
    would name it, and a feed nothing could read stops here rather than in the first issue.
    """
    answers: list[dict] = []
    for url in feeds:
        response = httpx.get(url, timeout=FEED_READ_S, trust_env=False)
        response.raise_for_status()
        parsed = feedparser.parse(io.BytesIO(response.content))
        if not parsed.version or not parsed.entries:
            raise ValueError(f"{url} is not a feed with articles")
        taken = [answer["name"] for answer in answers]
        name = unique_name(str(parsed.feed.get("title", "")), url, taken)
        answers.append({"ok": True, "name": name, "url": url})
    return answers


def access_headers(environ: Mapping[str, str]) -> dict[str, str]:
    """The Access service token's headers, or none when neither variable is set."""
    values = {header: environ.get(variable) for header, variable in ACCESS_HEADERS.items()}
    if any(values.values()) and not all(values.values()):
        raise ValueError(f"set both or neither of {', '.join(ACCESS_HEADERS.values())}")
    return values if all(values.values()) else {}


def open_store(args: argparse.Namespace) -> D1Queryable:
    if args.sqlite:
        # `LocalD1Client` creates a file it does not find, and a walk that only reads must
        # leave nothing behind on a mistyped path, or the next try finds a store with no tables.
        if not args.sqlite.is_file():
            raise D1Error(f"{args.sqlite} is not there: give the file the server opened")
        return LocalD1Client(args.sqlite)
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    api_token = os.environ.get("CLOUDFLARE_API_TOKEN")
    missing = [
        name
        for name, value in [
            ("CLOUDFLARE_ACCOUNT_ID", account_id),
            ("CLOUDFLARE_API_TOKEN", api_token),
        ]
        if not value
    ]
    if missing:
        raise D1Error(f"missing environment variable(s): {', '.join(missing)}")
    return D1Client(account_id=account_id, database_id=args.d1, api_token=api_token)


def source_names(store: D1Queryable) -> list[str]:
    return [row["name"] for row in store.query("SELECT name FROM sources").rows]


def site_read(url: str, path: str, headers: dict[str, str]) -> httpx.Response:
    """The site's answer at `path`, read as the wizard page reads it.

    A redirect is an answer that is not the route's, so it is not followed: the token
    goes to `url` and nowhere else.
    """
    response = httpx.get(
        f"{url.rstrip('/')}{path}", headers=headers, timeout=LOAD_S, trust_env=False
    )
    response.raise_for_status()
    return response


def site_awaits_sources(url: str, headers: dict[str, str]) -> bool:
    """Whether the site at `url` says, as it tells the wizard, that it has no source yet.

    An answer that says neither raises ValueError.
    """
    response = site_read(url, SITE_SOURCES_PATH, headers)
    answer = response.json()
    awaiting = answer.get("awaiting") if isinstance(answer, dict) else None
    if not isinstance(awaiting, bool):
        raise ValueError(
            f"it answered {response.text[:200]}, which says neither that it awaits "
            "its first source nor that it does not"
        )
    return awaiting


def site_settings(url: str, headers: dict[str, str]) -> tuple[list[str], dict[str, object]]:
    """The language tags the site's wizard offers, and the settings it says it holds.

    An answer that names neither raises ValueError.
    """
    response = site_read(url, SITE_SETTINGS_PATH, headers)
    answer = response.json()
    values = answer.get("values") if isinstance(answer, dict) else None
    languages = answer.get("languages") if isinstance(answer, dict) else None
    tags = [
        language.get("tag") if isinstance(language, dict) else None
        for language in (languages if isinstance(languages, list) else [None])
    ]
    if not isinstance(values, dict) or not all(isinstance(tag, str) for tag in tags):
        raise ValueError(
            f"it answered {response.text[:200]}, which names no settings and languages"
        )
    return tags, values


def store_settings(store: D1Queryable) -> tuple[dict[str, object], dict[str, str]]:
    """Every setting the store holds, decoded, and apart from them each one that is not JSON,
    which the production reader skips."""
    decoded: dict[str, object] = {}
    undecodable: dict[str, str] = {}
    for row in store.query("SELECT key, value FROM settings").rows:
        try:
            decoded[row["key"]] = json.loads(row["value"])
        except json.JSONDecodeError:
            undecodable[row["key"]] = row["value"]
    return decoded, undecodable


def store_flag(args: argparse.Namespace) -> str:
    return f"--sqlite {args.sqlite}" if args.sqlite else f"--d1 {args.d1}"


def store_mismatch(store: D1Queryable, flag: str, choice: Choice) -> str:
    """Why the store does not hold what the language step saved; empty when it does.

    Neither side held those values before the walk, so a store that holds them now is the
    one the site wrote them to.
    """
    try:
        held, undecodable = store_settings(store)
    except D1Error as e:
        return f"could not read {flag} for the language step's save: {e}"
    for key, value in choice.saved.items():
        if key not in held or held[key] != value:
            found = json.dumps(held[key]) if key in held else "nothing"
            if key in undecodable:
                found = f"a value that is not JSON, {undecodable[key][:80]!r}"
            return (
                f"--url and {flag} are not one install: the walk saved {key} = "
                f"{json.dumps(value)} at --url, and {flag} holds {found}"
            )
    return ""


def newest_run_id(store: D1Queryable) -> int:
    return store.query("SELECT COALESCE(MAX(id), 0) AS id FROM digest_runs").rows[0]["id"]


def newest_run_or_error(store: D1Queryable) -> int | str:
    try:
        return newest_run_id(store)
    except D1Error as e:
        return str(e)


def first_issue_row(store: D1Queryable, after: int, period: str) -> dict | None:
    """The first non-preview row of the run's period since the page started its run.

    The period keeps out a scheduled run of another period landing during the walk, as
    the page's own wait does, and `after` an earlier run's row: an empty `sources` says
    nothing of `digest_runs`, which a cleared deployment may still hold.
    """
    rows = store.query(
        "SELECT id, finished_at, status, period, dry_run, fetched, failed_sources, degraded, "
        "summary FROM digest_runs WHERE id > ? AND dry_run = 0 AND period = ? "
        "ORDER BY id LIMIT 1",
        [after, period],
    ).rows
    return rows[0] if rows else None


def read_receipts(
    store: D1Queryable, after: int | str | None, run: dict[str, str] | None
) -> list[str]:
    """Print the store's receipts, and name each that does not hold."""
    problems = []
    names = source_names(store)
    print(f"RECEIPT sources: {len(names)} row(s): {json.dumps(names, ensure_ascii=False)}")
    if not names:
        problems.append("receipt sources: no row")
    if run is None:
        problems.append("receipt digest_runs: the page never posted /run")
        return problems
    if not isinstance(after, int):
        problems.append(
            f"receipt digest_runs: could not read the store as the run started: {after}"
        )
        return problems
    row = first_issue_row(store, after, run.get("period", ""))
    if row is None:
        problems.append(f"receipt digest_runs: no {run.get('period')} row with id over {after}")
        return problems
    summary = json.loads(row.pop("summary") or "{}")
    print(f"RECEIPT digest_runs: {json.dumps(row, ensure_ascii=False)}")
    if row["status"] != "ok":
        reason = f": {summary['error']}" if summary.get("error") else ""
        problems.append(f"receipt digest_runs: id {row['id']} has status={row['status']}{reason}")
    return problems


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True, help="base URL of a deployment with no sources")
    parser.add_argument(
        "--feed",
        required=True,
        action="extend",
        nargs="+",
        metavar="URL",
        help="feed address to add on the source step; repeat it or give several",
    )
    store = parser.add_mutually_exclusive_group(required=True)
    store.add_argument("--sqlite", type=Path, metavar="PATH", help="the deployment's SQLite file")
    store.add_argument(
        "--d1",
        metavar="DATABASE_ID",
        help="the deployment's D1, with CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID set",
    )
    feed_check = parser.add_mutually_exclusive_group()
    feed_check.add_argument(
        "--break",
        dest="broken",
        choices=sorted(BREAKS),
        help="answer this request with an error in the browser",
    )
    feed_check.add_argument(
        "--skip-feed-checker",
        action="store_true",
        help="loopback only: answer the feed check in the browser with each feed's own title "
        "and address, so the server's FeedChecker never runs",
    )
    parser.add_argument(
        "--first-issue-timeout",
        type=int,
        default=FIRST_ISSUE_TIMEOUT_S,
        metavar="SECONDS",
        help=f"how long to wait for the first issue to end (default {FIRST_ISSUE_TIMEOUT_S})",
    )
    parser.add_argument("--browser", default=None, help="Chromium binary (else $CYRIS_CHROMIUM)")
    args = parser.parse_args(argv)
    for url in (args.url, *args.feed):
        # Where urlsplit and a browser read different hosts: no check of the host here could
        # say which one the browser will go to.
        if "\\" in url or "@" in urlsplit(url).netloc:
            parser.error(f"{url} holds a backslash or userinfo: give it as a plain address")
    if args.skip_feed_checker:
        elsewhere = [
            u for u in (args.url, *args.feed) if urlsplit(u).hostname not in LOOPBACK_HOSTS
        ]
        if elsewhere:
            parser.error(
                f"--skip-feed-checker is for a loopback run, and {', '.join(elsewhere)} is not: "
                "drop it, so the real feed check runs"
            )
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        headers = access_headers(os.environ)
        store = open_store(args)
        names = source_names(store)
        stored, _ = store_settings(store)
    except (ValueError, D1Error) as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return EXIT_REFUSED
    flag = store_flag(args)
    if names:
        print(
            f"REFUSED: the store, {flag}, holds {len(names)} sources row(s), "
            f"{json.dumps(names, ensure_ascii=False)}: "
            "the wizard would not be walking a new install.",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    # The store read above may be another install's: the site's own answer is what the
    # wizard acts on, and what the walk would write to.
    try:
        awaiting = site_awaits_sources(args.url, headers)
    except (ValueError, httpx.HTTPError) as e:
        print(f"REFUSED: could not read {SITE_SOURCES_PATH} at --url: {e}", file=sys.stderr)
        return EXIT_REFUSED
    if not awaiting:
        print(
            f"REFUSED: the site, --url {args.url}, says its sources are not empty "
            f"({SITE_SOURCES_PATH} is not awaiting them), while {flag} holds none: "
            "the wizard would not be walking a new install, and the two are not one install.",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    try:
        languages, site_values = site_settings(args.url, headers)
    except (ValueError, httpx.HTTPError) as e:
        print(f"REFUSED: could not read {SITE_SETTINGS_PATH} at --url: {e}", file=sys.stderr)
        return EXIT_REFUSED
    try:
        choice = choose(languages, site_values, stored, flag)
    except ValueError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return EXIT_REFUSED
    print(
        f"ANSWERING language {choice.language}, timezone {choice.timezone} and publish hours "
        f"{' and '.join(schedule_of(choice.hours))}: neither --url nor {flag} holds them",
        flush=True,
    )
    answers = None
    if args.skip_feed_checker:
        try:
            answers = read_feed_check_answers(args.feed)
        except (ValueError, httpx.HTTPError) as e:
            print(f"REFUSED: could not read the feed to answer its check: {e}", file=sys.stderr)
            return EXIT_REFUSED
        print(f"SKIPPING FeedChecker: the browser answers its check with {json.dumps(answers)}")

    browser = find_browser(args.browser)
    require_node()
    walk = Walk(newest_run=lambda: newest_run_or_error(store))
    job = build_job(
        args.url, args.feed, args.first_issue_timeout, headers, args.broken, answers, choice
    )
    budget_s = args.first_issue_timeout + FEED_CHECK_S * len(args.feed) + WALK_MARGIN_S
    with chromium(browser) as socket_url:
        asyncio.run(
            drive_walk(
                socket_url, job, budget_s, walk, lambda saved: store_mismatch(store, flag, choice)
            )
        )

    try:
        problems = read_receipts(store, walk.run_after, walk.run)
    except D1Error as e:
        problems = [f"receipt store: {e}"]
    for problem in problems:
        print(f"FAIL {problem}", flush=True)
    if walk.failure or problems:
        print("Last browser console errors:" if walk.console else "No browser console errors.")
        for line in walk.console:
            print(f"  {line}")
        return EXIT_FAILED
    print("OK: the wizard reached its first issue and the store shows it.")
    if args.skip_feed_checker:
        print("This run did not exercise FeedChecker: --skip-feed-checker answered its check.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
