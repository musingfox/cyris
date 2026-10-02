#!/usr/bin/env python3
"""Drive the raw page in headless Chromium and check what a reader actually gets.

pytest holds the raw page's markup and CSS, but not what its script does in a
browser: when the List / Triage switch appears, which article the triage card
deals, what a vote sends and what the page shows after it, and whether the page
fits a phone. This runs those checks against the page `render_raw` writes,
served in-process next to a stand-in for the app Worker's `/api/vote`, whose
answers each fixture chooses and whose every vote request is kept as a receipt.

Every check carries a sabotage that breaks the thing it reads. `--self-test`
runs each check clean, where it must pass, and sabotaged, where it must fail,
so a check that cannot fail is reported rather than trusted.

This script must never be collected by pytest: it needs Chromium and Node,
which makes it a reviewer-run gate rather than a test. Importing it is safe,
and `tests/test_css_receipt.py` does so to check its fixtures and registry.
"""

import argparse
import asyncio
import dataclasses
import json
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from aiohttp import web
from cdp_probe import (
    SEND_CREDENTIAL,
    Check,
    VoteFixture,
    at_largest_type_scale,
    base_prelude,
    chromium,
    largest_twin,
    require_node,
    run_all,
)
from css_computed import find_browser

from cyris.adapters.output.html_digest import HtmlDigestWriter
from cyris.domain.models import ArticleState, StoredArticle, Tier

DATE = "2026-01-02"
PERIOD = "morning"
PAGE = f"/{DATE}-{PERIOD}-raw.html"

KINDS = (
    "signed-in",
    "signed-in-largest",
    "signed-out",
    "no-worker",
    "vote-fails",
    "fails-once",
    "slow-vote",
    "vote-signed-out",
    "vote-unconfigured",
    "vote-hangs",
)

# How long `slow-vote` holds each vote: long enough for a check to act, and for
# CDP input to land, while the vote is still in flight.
SLOW_VOTE_S = 1.0

# How long `vote-hangs` holds each vote: far past the page's timeout as
# `SHORT_VOTE_TIMEOUT` sets it, short enough not to hold up the fixture's teardown.
HANG_VOTE_S = 2.0

# (source, title, state, score, slug): two sources of three, so the deck deals
# Pending Two, Pending Three, Pending Four, then the title written as markup.
ARTICLES = (
    ("Source A", "Accepted One", ArticleState.ACCEPTED, 0.9, "accepted-one"),
    ("Source A", "Pending Two", ArticleState.PENDING, 0.8, "pending-two"),
    ("Source A", "Pending Three", ArticleState.PENDING, None, "pending-three"),
    ("Source B", "Pending Four", ArticleState.PENDING, 0.5, "pending-four"),
    ("Source B", "Rejected Five", ArticleState.REJECTED, 0.2, "rejected-five"),
    ("Source B", "<b>Six</b>", ArticleState.PENDING, None, "six"),
)


def url_of(slug: str) -> str:
    return f"https://example.test/{slug}"


def _articles() -> list[StoredArticle]:
    timestamp = datetime(2026, 1, 2, tzinfo=UTC)
    return [
        StoredArticle(
            url=url_of(slug),
            original_id=slug,
            title=title,
            content="Raw probe fixture article.",
            published_at=timestamp,
            source_name=source,
            source_tier=Tier.FILTER,
            state=state,
            first_seen_at=timestamp,
            score=score,
        )
        for source, title, state, score, slug in ARTICLES
    ]


def render_page() -> str:
    with tempfile.TemporaryDirectory(prefix="raw-probe-") as unused:
        return HtmlDigestWriter(Path(unused)).render_raw(DATE, PERIOD, _articles())


def build_fixture(kind: str) -> VoteFixture:
    """Serve the raw page with the `/api/vote` answers of one deployment `kind`.

    `signed-in` answers the probe and takes votes, and `signed-in-largest` does
    too on the page served at the largest type size; `signed-out` refuses the
    probe; `no-worker` has no route at all, as on bare pages.dev; `vote-fails`
    signs in but refuses every vote; `fails-once` refuses only the first vote;
    `slow-vote` takes every vote, each only after `SLOW_VOTE_S`. The rest sign in
    and refuse every vote as the app Worker would: `vote-signed-out` with its own
    401 for a lapsed session, `vote-unconfigured` with its 503 for a missing
    promote Worker URL; `vote-hangs` answers only after `HANG_VOTE_S`.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown fixture {kind!r}")
    page = render_page()
    if kind == "signed-in-largest":
        page = at_largest_type_scale(page)
    app = web.Application()
    fixture = VoteFixture(app)

    async def serve_page(_: web.Request) -> web.Response:
        return web.Response(text=page, content_type="text/html")

    async def probe(_: web.Request) -> web.Response:
        if kind == "signed-out":
            return web.json_response({"authorized": False}, status=401)
        return web.json_response({"authorized": True})

    async def vote(request: web.Request) -> web.Response:
        fixture.posts.append(await request.json())
        fixture.post_headers.append(dict(request.headers))
        if kind == "slow-vote":
            await asyncio.sleep(SLOW_VOTE_S)
        if kind == "vote-hangs":
            await asyncio.sleep(HANG_VOTE_S)
        if kind == "vote-signed-out":
            return web.json_response({"authorized": False, "error": "unauthorized"}, status=401)
        if kind == "vote-unconfigured":
            return web.json_response({"error": "promote worker not configured"}, status=503)
        refused = kind == "vote-fails" or (kind == "fails-once" and len(fixture.posts) == 1)
        if refused:
            return web.json_response({"ok": False}, status=502)
        return web.json_response({"ok": True})

    app.router.add_get(PAGE, serve_page)
    if kind != "no-worker":
        app.router.add_get("/api/vote", probe)
        app.router.add_post("/api/vote", vote)
    return fixture


# Installed before the page loads: every fetch the page makes is recorded once it settles.
RECORD_FETCHES = """{
window.__fetches = [];
const realFetch = window.fetch;
window.fetch = async (input, init) => {
  const entry = {url: String(input), method: (init && init.method) || "GET", settled: false};
  window.__fetches.push(entry);
  try { return await realFetch(input, init); } finally { entry.settled = true; }
};
}"""

# Installed before the page loads: every vote request gives up after 300ms, so a
# check need not wait out the page's real timeout.
SHORT_VOTE_TIMEOUT = """{
const realTimeout = AbortSignal.timeout.bind(AbortSignal);
AbortSignal.timeout = () => realTimeout(300);
}"""

# Installed before the page loads: a page that set no timeout on its vote requests.
DROP_VOTE_SIGNAL = """{
const realFetch = window.fetch;
window.fetch = (input, init) => {
  if (!init || !init.signal) return realFetch(input, init);
  const {signal, ...rest} = init;
  return realFetch(input, rest);
};
}"""

# Installed before the page loads: a browser older than AbortSignal.timeout, whose
# timers of a second or more run out in 300ms, so a check need not wait them out.
NO_ABORT_SIGNAL_TIMEOUT = """{
delete AbortSignal.timeout;
const realSetTimeout = window.setTimeout.bind(window);
window.setTimeout = (fn, ms, ...args) => realSetTimeout(fn, ms >= 1000 ? 300 : ms, ...args);
}"""

# Installed before the page loads: a browser with no way to abort a request either.
NO_ABORT_CONTROLLER = """window.AbortController = undefined;"""

# A sabotage: focus() stops working, so nothing the page does can give focus back.
NO_FOCUS = """HTMLElement.prototype.focus = function () {};"""

# Installed before the page loads: a new tab is recorded rather than opened.
RECORD_OPEN = """
window.__opened = [];
window.open = (...args) => { window.__opened.push(args); return null; };
"""


def with_votes(*slugs: str) -> str:
    """A preload: this browser has already voted up on these articles."""
    stored = json.dumps({url_of(slug): "up" for slug in slugs})
    return f"localStorage.setItem('cyris-votes', {json.dumps(stored)});"


# Chromium keeps one profile for the whole run, and a fixture's origin is only as
# fresh as its port, so every check starts from a browser that has voted nothing.
FORGET_VOTES = "localStorage.removeItem('cyris-votes');\n"

REDUCED_MOTION = (("prefers-reduced-motion", "reduce"),)

# Installed before the page loads: the page's script is told motion is fine while
# the browser still stops every transition, so a fly-out never ends.
MOTION_ALLOWED = """{
const realMatch = window.matchMedia.bind(window);
window.matchMedia = (query) => query.includes("prefers-reduced-motion")
  ? {matches: false, media: query, addEventListener() {}, removeEventListener() {}}
  : realMatch(query);
}"""

RAW_PRELUDE = (
    base_prelude()
    + """
const rowOf = (title) => $$("a[target=_blank]").find((a) => a.textContent === title).parentElement;
const voteButton = (title, vote) => $(`.promote-btn[data-vote="${vote}"]`, rowOf(title));
const marked = (title, vote, mark) => voteButton(title, vote).classList.contains(mark);
const storedVotes = () => JSON.parse(localStorage.getItem("cyris-votes") || "{}");
const probeAnswered = async () => {
  await waitFor(() => (window.__fetches || []).some(
    (f) => f.url.endsWith("/api/vote") && f.method === "GET" && f.settled), "the vote probe");
  await sleep(50);
};
const pressed = (view) => $(`[data-raw-view="${view}"]`).getAttribute("aria-pressed");
const switchReady = () => waitFor(() => visible($("#raw-views")), "the view switch");
const showView = async (view) => {
  await switchReady();
  $(`[data-raw-view="${view}"]`).click();
};
const pressDeck = async (dir) => {
  await showView("triage");
  $(`#t-${dir}`).click();
};
const leaning = () => ["lean-up", "lean-down"].filter((c) => $("#t-card").classList.contains(c));
const border = () => getComputedStyle($("#t-card")).borderTopColor;
// Removes one rule from the page's stylesheet, inside the media block named if any.
const dropRule = (selector, media = "") => {
  const sheet = $("style").sheet;
  const holder = media
    ? [...sheet.cssRules].find((r) => r.conditionText === media
        && [...r.cssRules].some((inner) => inner.selectorText === selector))
    : sheet;
  const index = [...holder.cssRules].findIndex((r) => r.selectorText === selector);
  if (index < 0) throw new Error(`no rule ${selector}`);
  holder.deleteRule(index);
};
const deck = () => ["#t-remaining", "#t-source", "#t-title"].map((id) => $(id).textContent);
const signedIn = () => waitFor(() => visible(voteButton("Pending Two", "up")), "the vote buttons");
// The failure notice the list shows for a row, which sits right after it.
const rowNotice = (title) => {
  const next = rowOf(title).nextElementSibling;
  return next && next.classList.contains("vote-error") ? next : null;
};
const stateOf = (title) => $(".state", rowOf(title));
"""
)


# A sabotage: the page takes every vote failure notice out as soon as it is put in.
STRIP_NOTICES = """{
const strip = () => $$(".vote-error").forEach((n) => n.remove());
strip();
new MutationObserver(strip).observe(document.body, {subtree: true, childList: true});
}"""


def vote_body(slug: str, vote: str) -> dict:
    return {"url": url_of(slug), "vote": vote, "digest_date": DATE}


def _posted(*wanted: dict) -> Callable[[VoteFixture], str | None]:
    """A receipt: the stand-in `/api/vote` took exactly these vote bodies, in order."""
    return lambda fixture: None if fixture.posts == list(wanted) else f"posts: {fixture.posts}"


def _bare_votes(fixture: VoteFixture) -> str | None:
    """A receipt: every vote arrived as url, vote and date alone, with no credential."""
    if not fixture.posts:
        return "no vote arrived"
    keys = [sorted(post) for post in fixture.posts]
    if keys != [["digest_date", "url", "vote"]] * len(keys):
        return f"vote bodies carry {keys}"
    sent = [sorted(key.lower() for key in headers) for headers in fixture.post_headers]
    if any("authorization" in headers for headers in sent):
        return f"a vote carried a credential: {sent}"
    return None


_CHECKS: list[Check] = [
    Check(
        id="list-vote-marks-done",
        fixture="signed-in",
        path=PAGE,
        act="""
            await signedIn();
            voteButton("Pending Two", "up").click();
            await waitFor(() => marked("Pending Two", "up", "done"), "the vote marked done");
        """,
        script="""
            expect(voteButton("Pending Two", "up").classList.contains("done"), "up is not done");
            const stored = storedVotes()["https://example.test/pending-two"];
            expect(stored === "up", `stored: ${stored}`);
        """,
        sabotage="""$$(".promote-btn").forEach((b) => b.classList.remove("done"));""",
        receipt=_posted(vote_body("pending-two", "up")),
    ),
    # A refused list vote says why on the line below its row, and nothing is kept.
    *(
        Check(
            id=f"list-vote-failure-{name}",
            fixture=fixture,
            path=PAGE,
            preload=SHORT_VOTE_TIMEOUT if fixture == "vote-hangs" else "",
            act="""
                await signedIn();
                voteButton("Pending Two", "up").click();
                await waitFor(() => rowNotice("Pending Two"), "the failure notice");
            """,
            script=f"""
                const notice = rowNotice("Pending Two");
                expect(visible(notice), "the failure notice is hidden");
                expect(notice.getAttribute("role") === "alert", "the notice is not announced");
                const text = notice.textContent;
                expect(text.includes({json.dumps(reason)}), `notice: ${{text}}`);
                const up = voteButton("Pending Two", "up");
                expect(up.classList.contains("error"), "up is not marked failed");
                expect(!up.classList.contains("done"), "up is marked done");
                expect(up.getAttribute("aria-pressed") === "false", "up is pressed");
                expect(stateOf("Pending Two").textContent === "pending", "the state changed");
                const stored = JSON.stringify(storedVotes());
                expect(!stored.includes("pending-two"), `stored: ${{stored}}`);
            """,
            sabotage="" if fixture == "vote-hangs" else STRIP_NOTICES,
            sabotage_preload=DROP_VOTE_SIGNAL if fixture == "vote-hangs" else "",
            receipt=_posted(vote_body("pending-two", "up")),
        )
        for name, fixture, reason in (
            ("says-why", "vote-fails", "HTTP 502"),
            ("signed-out-says-sign-in", "vote-signed-out", "sign in again"),
            ("unconfigured-names-the-url", "vote-unconfigured", "CYRIS_PROMOTE_WORKER_URL"),
            ("times-out", "vote-hangs", "No answer from the server"),
        )
    ),
    # A browser older than AbortSignal.timeout still votes, and still gives up.
    Check(
        id="list-vote-lands-without-abort-signal-timeout",
        fixture="signed-in",
        path=PAGE,
        preload=NO_ABORT_SIGNAL_TIMEOUT,
        act="""
            await signedIn();
            voteButton("Pending Two", "up").click();
        """,
        script="""
            await waitFor(() => marked("Pending Two", "up", "done"), "the vote marked done");
            expect(!rowNotice("Pending Two"), "a failure notice shows");
        """,
        sabotage_preload=NO_ABORT_CONTROLLER,
        receipt=_posted(vote_body("pending-two", "up")),
    ),
    Check(
        id="list-vote-times-out-without-abort-signal-timeout",
        fixture="vote-hangs",
        path=PAGE,
        preload=NO_ABORT_SIGNAL_TIMEOUT,
        act="""
            await signedIn();
            voteButton("Pending Two", "up").click();
            await waitFor(() => rowNotice("Pending Two"), "the failure notice", 1500);
        """,
        script="""
            const text = rowNotice("Pending Two").textContent;
            expect(text.includes("No answer from the server"), `notice: ${text}`);
        """,
        sabotage_preload=DROP_VOTE_SIGNAL,
        receipt=_posted(vote_body("pending-two", "up")),
    ),
    Check(
        id="list-vote-retry-clears-notice",
        fixture="fails-once",
        path=PAGE,
        act="""
            await signedIn();
            voteButton("Pending Two", "up").click();
            await waitFor(() => rowNotice("Pending Two"), "the failure notice");
            await waitFor(() => !voteButton("Pending Two", "up").disabled, "the buttons back");
            voteButton("Pending Two", "up").click();
            await waitFor(() => marked("Pending Two", "up", "done"), "the vote marked done");
        """,
        script="""expect(!rowNotice("Pending Two"), "the failure notice stays");""",
        sabotage="""
            const notice = document.createElement("p");
            notice.className = "vote-error";
            rowOf("Pending Two").after(notice);
        """,
    ),
    Check(
        id="list-vote-inflight-disables-group",
        fixture="slow-vote",
        path=PAGE,
        act="""
            await signedIn();
            voteButton("Pending Two", "up").click();
        """,
        script=f"""
            const on = $$(".promote-btn", rowOf("Pending Two")).filter((b) => !b.disabled);
            expect(on.length === 0, `enabled in flight: ${{on.map((b) => b.dataset.vote)}}`);
            voteButton("Pending Two", "down").click();
            await waitFor(() => marked("Pending Two", "up", "done"), "the vote marked done");
            await sleep({int(SLOW_VOTE_S * 1000)});
        """,
        sabotage="""$$(".promote-btn", rowOf("Pending Two")).forEach((b) => {
            b.disabled = false;
        });""",
        receipt=_posted(vote_body("pending-two", "up")),
    ),
    # The pressed state and the state text follow the vote, both ways.
    *(
        Check(
            id=f"list-vote-{vote}-shows-state",
            fixture="signed-in",
            path=PAGE,
            act=f"""
                await signedIn();
                voteButton("Pending Two", "{vote}").click();
                await waitFor(() => marked("Pending Two", "{vote}", "done"), "the vote");
            """,
            script=f"""
                const state = stateOf("Pending Two");
                expect(state.textContent === "{state}", `state text: ${{state.textContent}}`);
                expect(state.classList.contains("{state}"), `state class: ${{state.className}}`);
                const pressedOf = (v) => voteButton("Pending Two", v).getAttribute("aria-pressed");
                expect(pressedOf("{vote}") === "true", `{vote}: ${{pressedOf("{vote}")}}`);
                expect(pressedOf("{other}") === "false", `{other}: ${{pressedOf("{other}")}}`);
            """,
            sabotage=f"""
                const state = stateOf("Pending Two");
                state.className = "state pending";
                state.textContent = "pending";
                voteButton("Pending Two", "{vote}").setAttribute("aria-pressed", "false");
            """,
            receipt=_posted(vote_body("pending-two", vote)),
        )
        for vote, other, state in (("up", "down", "accepted"), ("down", "up", "rejected"))
    ),
    Check(
        id="list-stored-vote-shows-state",
        fixture="signed-in",
        path=PAGE,
        preload=with_votes("pending-two"),
        act="await signedIn();",
        script="""
            const text = stateOf("Pending Two").textContent;
            expect(text === "accepted", `state text: ${text}`);
            const pressed = voteButton("Pending Two", "up").getAttribute("aria-pressed");
            expect(pressed === "true", `up pressed: ${pressed}`);
        """,
        sabotage="""stateOf("Pending Two").textContent = "pending";""",
    ),
    Check(
        id="list-vote-keeps-focus",
        fixture="slow-vote",
        path=PAGE,
        act="""
            await signedIn();
            voteButton("Pending Two", "up").focus();
        """,
        sabotage=NO_FOCUS,
        script="""
            const up = voteButton("Pending Two", "up");
            expect(document.activeElement === up, "up never took focus");
            up.click();
            await waitFor(() => marked("Pending Two", "up", "done"), "the vote marked done");
            const now = document.activeElement;
            expect(now === up, `focus is on ${now && now.tagName}`);
        """,
        receipt=_posted(vote_body("pending-two", "up")),
    ),
    Check(
        id="row-title-wraps-400",
        fixture="signed-in",
        path=PAGE,
        width=400,
        act="await signedIn();",
        script="""
            const row = rowOf("Pending Two");
            const title = $("a", row).getBoundingClientRect();
            const state = $(".state", row).getBoundingClientRect();
            expect(title.top >= state.bottom, `title at ${title.top}, state ends ${state.bottom}`);
        """,
        sabotage="""$$(".raw-row a").forEach((a) => { a.style.gridColumn = "auto"; });""",
    ),
    Check(
        id="fits-400-list",
        fixture="signed-in",
        path=PAGE,
        width=400,
        act="await signedIn();",
        script="""const problem = overflow("list"); expect(!problem, problem);""",
        sabotage="""$(".container").style.minWidth = "600px";""",
    ),
    *(
        Check(
            id=f"gate-{kind}",
            fixture=kind,
            path=PAGE,
            preload=RECORD_FETCHES,
            act="await probeAnswered();",
            script="""
                expect(!visible($("#raw-views")), "the view switch shows");
                const panels = $$(".panel");
                expect(panels.length === 2 && panels.every(visible), "the list is gone");
                expect(!$$(".vote-group").some(visible), "vote buttons show");
            """,
            sabotage="""$("#raw-views").hidden = false;""",
        )
        for kind in ("signed-out", "no-worker")
    ),
    Check(
        id="gate-signed-in",
        fixture="signed-in",
        path=PAGE,
        act="""await waitFor(() => visible($("#raw-views")), "the view switch");""",
        script="""expect(visible($("#raw-views")), "the view switch is hidden");""",
        sabotage="""$("#raw-views").hidden = true;""",
    ),
    Check(
        id="sign-in-signed-out",
        fixture="signed-out",
        path=PAGE,
        act="""await waitFor(() => visible($(".signin-link")), "Sign in");""",
        script=f"""
            const href = $(".signin-link").getAttribute("href");
            const wanted = "/login?next=" + encodeURIComponent({json.dumps(PAGE)});
            expect(href === wanted, `Sign in links ${{href}}`);
            expect(!visible($(".settings-link")), "Settings shows");
        """,
        sabotage="""$(".signin-link").setAttribute("href", "/login");""",
    ),
    *(
        Check(
            id=f"sign-in-hidden-{kind}",
            fixture=kind,
            path=PAGE,
            preload=RECORD_FETCHES,
            act="await probeAnswered();",
            script="""expect(!visible($(".signin-link")), "Sign in shows");""",
            sabotage="""$(".signin-link").hidden = false;""",
        )
        for kind in ("signed-in", "no-worker")
    ),
    Check(
        id="default-list",
        fixture="signed-in",
        path=PAGE,
        act="await switchReady();",
        script="""
            expect(pressed("list") === "true", `List pressed: ${pressed("list")}`);
            expect(visible($("#raw-groups")), "the list is hidden");
            expect(!visible($("#raw-triage")), "the triage view shows");
        """,
        sabotage="""$("#raw-triage").hidden = false;""",
    ),
    Check(
        id="switch-to-triage",
        fixture="signed-in",
        path=PAGE,
        act="""
            window.__marker = 1;
            await showView("triage");
        """,
        script="""
            expect(pressed("triage") === "true", `Triage pressed: ${pressed("triage")}`);
            expect(pressed("list") === "false", `List pressed: ${pressed("list")}`);
            expect(visible($("#raw-triage")), "the triage view is hidden");
            expect(!visible($("#raw-groups")), "the list shows");
            expect(window.__marker === 1, "the page reloaded");
        """,
        sabotage="""$("#raw-groups").hidden = false;""",
    ),
    Check(
        id="switch-back-to-list",
        fixture="signed-in",
        path=PAGE,
        act="""
            await showView("triage");
            await showView("list");
        """,
        script="""
            expect(visible($("#raw-groups")), "the list is hidden");
            expect(!visible($("#raw-triage")), "the triage view shows");
            expect(pressed("list") === "true", `List pressed: ${pressed("list")}`);
        """,
        sabotage="""$("#raw-triage").hidden = false;""",
    ),
    Check(
        id="fits-400-triage",
        fixture="signed-in",
        path=PAGE,
        width=400,
        act="""await showView("triage");""",
        script="""
            const problem = overflow("triage");
            expect(!problem, problem);
            const down = $("#t-down").getBoundingClientRect();
            const up = $("#t-up").getBoundingClientRect();
            expect(down.height === 56 && up.height === 56, `heights ${down.height} ${up.height}`);
            expect(down.top === up.top, `tops ${down.top} ${up.top}`);
        """,
        sabotage="""$(".deck-wrap").style.minWidth = "600px";""",
    ),
    Check(
        id="deck-pending-only",
        fixture="signed-in",
        path=PAGE,
        act="""await showView("triage");""",
        script="""
            const shown = deck();
            expect(same(shown, ["4 remaining", "Source A", "Pending Two"]), `deck: ${shown}`);
        """,
        sabotage="""$("#t-title").textContent = "Accepted One";""",
    ),
    Check(
        id="deck-skips-voted",
        fixture="signed-in",
        path=PAGE,
        preload=with_votes("pending-two"),
        act="""await showView("triage");""",
        script="""
            const shown = deck();
            expect(same(shown, ["3 remaining", "Source A", "Pending Three"]), `deck: ${shown}`);
        """,
        sabotage="""$("#t-remaining").textContent = "4 remaining";""",
    ),
    Check(
        id="deck-title-is-text",
        fixture="signed-in",
        path=PAGE,
        preload=with_votes("pending-two", "pending-three", "pending-four"),
        act="""await showView("triage");""",
        script="""
            const title = $("#t-title").textContent;
            expect(title === "<b>Six</b>", `title: ${title}`);
            expect($("#t-title b") === null, "the title was parsed as markup");
        """,
        sabotage="""$("#t-title").innerHTML = "<b>Six</b>";""",
    ),
    Check(
        id="list-vote-leaves-deck",
        fixture="signed-in",
        path=PAGE,
        act="""
            await signedIn();
            voteButton("Pending Two", "up").click();
            await waitFor(() => marked("Pending Two", "up", "done"), "the vote marked done");
            await showView("triage");
        """,
        script="""
            const shown = deck();
            expect(same(shown, ["3 remaining", "Source A", "Pending Three"]), `deck: ${shown}`);
        """,
        sabotage="""$("#t-title").textContent = "Pending Two";""",
    ),
    Check(
        id="deck-empty",
        fixture="signed-in",
        path=PAGE,
        preload=with_votes("pending-two", "pending-three", "pending-four", "six"),
        act="""await showView("triage");""",
        script="""
            const shown = $$("#raw-triage *").filter(visible).map((el) => el.id);
            expect(same(shown, ["t-remaining"]), `visible: ${shown}`);
            const count = $("#t-remaining").textContent;
            expect(count === "0 remaining", `count: ${count}`);
        """,
        sabotage="""$("#t-card").hidden = false;""",
    ),
    *(
        Check(
            id=f"button-{vote}-votes",
            fixture="signed-in",
            path=PAGE,
            act=f"""
                await pressDeck("{vote}");
                await waitFor(() => deck()[0] === "3 remaining", "the next card");
            """,
            script=f"""
                const shown = deck();
                const next = ["3 remaining", "Source A", "Pending Three"];
                expect(same(shown, next), `deck: ${{shown}}`);
                expect(marked("Pending Two", "{vote}", "done"), "the list does not show the vote");
            """,
            sabotage="""$$(".promote-btn").forEach((b) => b.classList.remove("done"));""",
            receipt=_posted(vote_body("pending-two", vote)),
        )
        for vote in ("up", "down")
    ),
    Check(
        id="vote-post-carries-no-credential",
        fixture="signed-in",
        path=PAGE,
        act="""
            await pressDeck("up");
            await waitFor(() => deck()[0] === "3 remaining", "the next card");
        """,
        script="""expect(deck()[2] === "Pending Three", `title: ${deck()[2]}`);""",
        sabotage_preload=SEND_CREDENTIAL,
        receipt=_bare_votes,
    ),
    Check(
        id="double-press-one-vote",
        fixture="signed-in",
        path=PAGE,
        act="""
            await showView("triage");
            $("#t-up").click();
            $("#t-up").click();
            await waitFor(() => deck()[0] === "3 remaining", "the next card");
            await sleep(300);
        """,
        script="""expect(deck()[0] === "3 remaining", `count: ${deck()[0]}`);""",
        sabotage="""await castVote($(".vote-group", rowOf("Pending Two")), "up");""",
        receipt=_posted(vote_body("pending-two", "up")),
    ),
    # Two guards keep one card to one vote, and each check below holds one of them:
    # the buttons turn disabled at once, and `busy` turns a drag on the card away.
    Check(
        id="inflight-disabled",
        fixture="slow-vote",
        path=PAGE,
        act="""
            await showView("triage");
            $("#t-up").click();
        """,
        script="""
            const on = $$("#t-actions button").filter((b) => !b.disabled).map((b) => b.id);
            expect(on.length === 0, `enabled while the vote is in flight: ${on}`);
            await waitFor(() => deck()[0] === "3 remaining", "the next card");
        """,
        sabotage="""enableDeck(true);""",
        receipt=_posted(vote_body("pending-two", "up")),
    ),
    Check(
        id="swipe-during-button-vote",
        fixture="slow-vote",
        path=PAGE,
        act="""
            await showView("triage");
            $("#t-up").click();
        """,
        gestures=({"press": "#t-card", "pointer": "mouse"}, {"move": 200}, {"release": True}),
        script="""
            await waitFor(() => deck()[0] === "3 remaining", "the next card");
            await sleep(300);
            expect(deck()[0] === "3 remaining", `count: ${deck()[0]}`);
        """,
        sabotage="""busy = false;""",
        receipt=_posted(vote_body("pending-two", "up")),
    ),
    Check(
        id="switch-away-during-vote",
        fixture="slow-vote",
        path=PAGE,
        preload=RECORD_FETCHES,
        act="""
            await pressDeck("up");
            await showView("list");
            await waitFor(() => window.__fetches.some((f) => f.method === "POST" && f.settled),
              "the vote to land", 3000);
            await sleep(50);
        """,
        script="""
            await showView("triage");
            const shown = deck();
            expect(same(shown, ["3 remaining", "Source A", "Pending Three"]), `deck: ${shown}`);
            const card = $("#t-card");
            expect(card.style.transform === "", `the card is at ${card.style.transform}`);
            expect(!card.dataset.flying, `the card is still flying ${card.dataset.flying}`);
            const off = $$("#t-actions button").filter((b) => b.disabled).map((b) => b.id);
            expect(off.length === 0, `disabled: ${off}`);
        """,
        # A flight nothing settles is the jam itself.
        sabotage="""settle = () => {};""",
        receipt=_posted(vote_body("pending-two", "up")),
    ),
    Check(
        id="vote-failure-stays",
        fixture="vote-fails",
        path=PAGE,
        act="""
            await pressDeck("up");
            await waitFor(() => visible($("#t-error")), "the failure notice");
        """,
        script="""
            const shown = deck();
            expect(same(shown, ["4 remaining", "Source A", "Pending Two"]), `deck: ${shown}`);
            expect(visible($("#t-error")), "the failure notice is hidden");
            const text = $("#t-error").textContent;
            expect(text.includes("HTTP 502"), `notice: ${text}`);
            const notice = $("#t-error").getBoundingClientRect();
            const actions = $("#t-actions").getBoundingClientRect();
            expect(notice.top >= actions.bottom, "the notice is not under the buttons");
            expect($("#t-card").style.transform === "", "the card has moved");
            const done = $$(".promote-btn.done", rowOf("Pending Two")).length;
            expect(done === 0, "the list shows a vote");
        """,
        sabotage="""$("#t-error").hidden = true;""",
    ),
    Check(
        id="deck-signed-out-says-sign-in",
        fixture="vote-signed-out",
        path=PAGE,
        act="""
            await pressDeck("up");
            await waitFor(() => visible($("#t-error")), "the failure notice");
        """,
        script="""
            const text = $("#t-error").textContent;
            expect(text.includes("sign in again"), `notice: ${text}`);
        """,
        sabotage="""$("#t-error").textContent = "";""",
        receipt=_posted(vote_body("pending-two", "up")),
    ),
    # A keyboard reader presses Enter on Up card after card, so focus must stay on Up.
    Check(
        id="deck-enter-votes-each-card",
        fixture="signed-in",
        path=PAGE,
        act="""
            await showView("triage");
            $("#t-up").focus();
        """,
        sabotage=NO_FOCUS,
        script="""
            expect(document.activeElement === $("#t-up"), "Up never took focus");
            document.activeElement.click();
            await waitFor(() => deck()[0] === "3 remaining", "the next card");
            const now = document.activeElement;
            expect(now === $("#t-up"), `focus is on ${now && (now.id || now.tagName)}`);
            now.click();
            await waitFor(() => deck()[0] === "2 remaining", "the card after it");
        """,
        receipt=_posted(vote_body("pending-two", "up"), vote_body("pending-three", "up")),
    ),
    # The last card's vote hides the deck's buttons, so focus lands on the Triage switch.
    Check(
        id="deck-last-vote-focuses-the-switch",
        fixture="signed-in",
        path=PAGE,
        preload=with_votes("pending-two", "pending-three", "pending-four"),
        act="""
            await showView("triage");
            $("#t-up").focus();
        """,
        sabotage=NO_FOCUS,
        script="""
            expect(document.activeElement === $("#t-up"), "Up never took focus");
            $("#t-up").click();
            await waitFor(() => deck()[0] === "0 remaining", "the empty deck");
            const now = document.activeElement;
            const wanted = $('[data-raw-view="triage"]');
            expect(now === wanted, `focus is on ${now && (now.id || now.tagName)}`);
        """,
        receipt=_posted(vote_body("six", "up")),
    ),
    Check(
        id="vote-retry-clears-notice",
        fixture="fails-once",
        path=PAGE,
        act="""
            await pressDeck("up");
            await waitFor(() => visible($("#t-error")), "the failure notice");
            await waitFor(() => !$("#t-up").disabled, "the buttons back");
            $("#t-up").click();
            await waitFor(() => deck()[0] === "3 remaining", "the next card");
        """,
        script="""expect(!visible($("#t-error")), "the failure notice stays");""",
        sabotage="""$("#t-error").hidden = false;""",
    ),
    *(
        Check(
            id=check_id,
            fixture="signed-in",
            path=PAGE,
            width=width,
            act="""await showView("triage");""",
            gestures=({"press": "#t-card", "pointer": pointer}, {"move": dx}, {"release": True}),
            script="""await waitFor(() => deck()[0] === "3 remaining", "the next card");""",
            sabotage="""$("#t-card").style.pointerEvents = "none";""",
            receipt=_posted(vote_body("pending-two", vote)),
        )
        for check_id, width, pointer, dx, vote in (
            ("swipe-right-up", 1440, "mouse", 200, "up"),
            ("swipe-left-down", 1440, "mouse", -200, "down"),
            ("swipe-touch", 400, "touch", 200, "up"),
        )
    ),
    Check(
        id="short-drag-no-vote",
        fixture="signed-in",
        path=PAGE,
        act="""await showView("triage");""",
        gestures=({"press": "#t-card", "pointer": "mouse"}, {"move": 60}, {"release": True}),
        script="""
            await sleep(400);
            expect(deck()[0] === "4 remaining", `count: ${deck()[0]}`);
            expect($("#t-card").style.transform === "", "the card did not snap back");
            expect(leaning().length === 0, `leaning: ${leaning()}`);
        """,
        sabotage="""await castVote($(".vote-group", rowOf("Pending Two")), "up");""",
        receipt=_posted(),
    ),
    *(
        Check(
            id=f"lean-{vote}-drag",
            fixture="signed-in",
            path=PAGE,
            act="""await showView("triage");""",
            gestures=({"press": "#t-card", "pointer": "mouse"}, {"move": dx}),
            script=f"""
                expect(same(leaning(), ["lean-{vote}"]), `leaning: ${{leaning()}}`);
                await waitFor(() => border() === "{colour}", `the border ${{border()}}`, 2000);
            """,
            sabotage=f"""dropRule(".card.lean-{vote}");""",
        )
        for vote, dx, colour in (
            ("up", 60, "rgb(198, 255, 61)"),
            ("down", -60, "rgb(255, 91, 138)"),
        )
    ),
    Check(
        id="lean-button-hover",
        fixture="signed-in",
        path=PAGE,
        act="""await showView("triage");""",
        gestures=({"hover": "#t-up"},),
        script="""await waitFor(() => same(leaning(), ["lean-up"]), `lean ${leaning()}`, 2000);""",
        sabotage="""$("#t-up").style.pointerEvents = "none";""",
    ),
    Check(
        id="tap-opens-article",
        fixture="signed-in",
        path=PAGE,
        preload=RECORD_OPEN,
        act="""await showView("triage");""",
        gestures=({"press": "#t-card", "pointer": "mouse"}, {"release": True}),
        script="""
            const opened = window.__opened;
            const wanted = [["https://example.test/pending-two", "_blank", "noopener"]];
            expect(same(opened, wanted), `opened: ${JSON.stringify(opened)}`);
        """,
        sabotage="""$("#t-card").style.pointerEvents = "none";""",
        receipt=_posted(),
    ),
    Check(
        id="tap-touch-opens-article",
        fixture="signed-in",
        path=PAGE,
        width=400,
        preload=RECORD_OPEN,
        act="""await showView("triage");""",
        gestures=({"press": "#t-card", "pointer": "touch"}, {"release": True}),
        script="""
            const opened = window.__opened;
            const wanted = [["https://example.test/pending-two", "_blank", "noopener"]];
            expect(same(opened, wanted), `opened: ${JSON.stringify(opened)}`);
        """,
        sabotage="""$("#t-card").style.pointerEvents = "none";""",
        receipt=_posted(),
    ),
    *(
        Check(
            id=check_id,
            fixture="signed-in",
            path=PAGE,
            preload=RECORD_OPEN,
            act="""await showView("triage");""",
            gestures=gestures,
            script="""
                await sleep(200);
                expect(same(window.__opened, []), `opened: ${JSON.stringify(window.__opened)}`);
                expect(deck()[0] === "4 remaining", `count: ${deck()[0]}`);
            """,
            sabotage=sabotage,
            receipt=_posted(),
        )
        for check_id, gestures, sabotage in (
            *(
                (
                    f"{button}-click-no-open",
                    ({"press": "#t-card", "pointer": "mouse", "button": button}, {"release": True}),
                    # The page can no longer tell which button was pressed.
                    """Object.defineProperty(MouseEvent.prototype, "button", {get: () => 0});""",
                )
                for button in ("right", "middle")
            ),
            (
                "vertical-drag-no-open",
                (
                    {"press": "#t-card", "pointer": "mouse"},
                    {"move": 0, "dy": 200},
                    {"release": True},
                ),
                # The page can no longer see vertical movement.
                """Object.defineProperty(MouseEvent.prototype, "clientY", {get: () => 0});""",
            ),
        )
    ),
    Check(
        id="reduced-motion-still",
        fixture="signed-in",
        path=PAGE,
        media=REDUCED_MOTION,
        act="""await showView("triage");""",
        gestures=({"press": "#t-card", "pointer": "mouse"}, {"move": 200}),
        script="""
            const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
            expect(reduced, "the browser does not ask for reduced motion");
            const moved = getComputedStyle($("#t-card")).transform;
            expect(moved === "none", `the card moved: ${moved}`);
        """,
        sabotage="""dropRule(".card", "(prefers-reduced-motion: reduce)");""",
    ),
    *(
        Check(
            id=check_id,
            fixture="signed-in",
            path=PAGE,
            media=REDUCED_MOTION,
            act=act,
            gestures=gestures,
            script="""
                await waitFor(() => deck()[0] === "3 remaining", "the next card", 2000);
                expect(deck()[2] === "Pending Three", `title: ${deck()[2]}`);
            """,
            sabotage_preload=MOTION_ALLOWED,
        )
        for check_id, act, gestures in (
            (
                "reduced-motion-advances",
                """await pressDeck("up");""",
                (),
            ),
            (
                "reduced-motion-swipe-advances",
                """await showView("triage");""",
                ({"press": "#t-card", "pointer": "mouse"}, {"move": 200}, {"release": True}),
            ),
        )
    ),
]
# The page never scrolls sideways at the largest type size either.
_CHECKS += [
    largest_twin(check, check.id.replace("fits-", "fits-largest-"), "signed-in-largest")
    for check in _CHECKS
    if check.id in ("fits-400-list", "fits-400-triage")
]

CHECKS = [dataclasses.replace(check, preload=FORGET_VOTES + check.preload) for check in _CHECKS]


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
    with chromium(browser) as socket_url:
        good = asyncio.run(run_all(checks, socket_url, args.self_test, build_fixture, RAW_PRELUDE))
    raise SystemExit(0 if good else 1)


if __name__ == "__main__":
    main()
