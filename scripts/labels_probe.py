#!/usr/bin/env python3
"""Drive /labels in headless Chromium and check what a reader actually gets.

pytest holds the labels API and the page's markup, but not what its script does
in a browser: which item the card deals, that nothing of the pipeline's verdict
shows, what an answer sends and what the page shows after it, and whether the
page fits a phone. This runs those checks against the page `render_labels_page`
renders, with its real script and stylesheet, served in-process beside a
stand-in `/api/labels` whose every answer is kept as a receipt.

Every check carries a sabotage that breaks the thing it reads. `--self-test`
runs each check clean, where it must pass, and sabotaged, where it must fail,
so a check that cannot fail is reported rather than trusted.

This script must never be collected by pytest: it needs Chromium and Node,
which makes it a reviewer-run gate rather than a test. Importing it is safe,
and `tests/test_css_receipt.py` does so to check its fixtures and registry.
"""

import argparse
import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field

from aiohttp import web
from cdp_probe import (
    NO_SITE_BAR_AREAS,
    Check,
    at_largest_type_scale,
    base_prelude,
    chromium,
    largest_twin,
    require_node,
    restyle,
    run_all,
)
from css_computed import find_browser

from cyris.entrypoints.triage_server import STATIC_DIR, render_labels_page

PAGE = "/labels"

KINDS = (
    "sample",
    "sample-largest",
    "answer-fails",
    "signed-out",
    "all-answered",
    "no-sample",
)

# (url, title, source, excerpt): the order the stand-in deals them in.
ITEMS = (
    ("https://example.test/one", "Title One", "Source A", "Excerpt of the first article."),
    ("https://example.test/two", "Title Two", "Source B", "Excerpt of the second article."),
    ("https://example.test/three", "<b>Three</b>", "Source A", ""),
)


@dataclass
class LabelsFixture:
    """The page beside a stand-in `/api/labels`; `posts` is the receipt of every answer."""

    app: web.Application
    posts: list[dict] = field(default_factory=list)


def _deck(answered: int, total: int) -> dict:
    item = None
    if answered < total:
        url, title, source, excerpt = ITEMS[answered]
        item = {"url": url, "title": title, "source": source, "excerpt": excerpt}
    return {"answered": answered, "total": total, "item": item}


def build_fixture(kind: str) -> LabelsFixture:
    """Serve /labels with the `/api/labels` answers of one deployment `kind`.

    `sample` deals the three `ITEMS` and takes every answer, and `sample-largest`
    does too on the page at the largest type size; `answer-fails` deals them but
    refuses every answer with D1's 500; `signed-out` refuses every answer with
    the app Worker's own 401; `all-answered` has every item answered, and
    `no-sample` has none drawn.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown fixture {kind!r}")
    page = render_labels_page()
    if kind == "sample-largest":
        page = at_largest_type_scale(page)
    app = web.Application()
    fixture = LabelsFixture(app)
    total = {"no-sample": 0}.get(kind, len(ITEMS))
    progress = {"answered": total if kind == "all-answered" else 0}

    async def serve_page(_: web.Request) -> web.Response:
        return web.Response(text=page, content_type="text/html")

    async def probe(_: web.Request) -> web.Response:
        return web.json_response({"authorized": True})

    async def read(_: web.Request) -> web.Response:
        return web.json_response(_deck(progress["answered"], total))

    async def answer(request: web.Request) -> web.Response:
        fixture.posts.append(await request.json())
        if kind == "answer-fails":
            return web.json_response({"ok": False, "error": "D1 is unreachable"}, status=500)
        if kind == "signed-out":
            return web.json_response({"error": "unauthorized"}, status=401)
        progress["answered"] += 1
        return web.json_response({"ok": True, **_deck(progress["answered"], total)})

    app.router.add_get(PAGE, serve_page)
    app.router.add_get("/api/vote", probe)
    app.router.add_get("/api/labels", read)
    app.router.add_post("/api/labels", answer)
    app.router.add_static("/static", STATIC_DIR)
    return fixture


def answer_as(label: str) -> str:
    """A sabotage installed before the page loads: every answer the page sends says `label`."""
    return f"""{{
const realFetch = window.fetch;
window.fetch = (input, init) => init && init.method === "POST"
  ? realFetch(input, {{
      ...init, body: JSON.stringify({{...JSON.parse(init.body), label: "{label}"}}),
    }})
  : realFetch(input, init);
}}"""


# Installed before the page loads: a new tab is recorded rather than opened.
RECORD_OPEN = """
window.__opened = [];
window.open = (...args) => { window.__opened.push(args); return null; };
"""

REDUCED_MOTION = (("prefers-reduced-motion", "reduce"),)

# Installed before the page loads: the page's script is told motion is fine while
# the browser still stops every transition, so a fly-out never ends.
MOTION_ALLOWED = """{
const realMatch = window.matchMedia.bind(window);
window.matchMedia = (query) => query.includes("prefers-reduced-motion")
  ? {matches: false, media: query, addEventListener() {}, removeEventListener() {}}
  : realMatch(query);
}"""

# A sabotage: the card is swapped for a copy, which carries none of its listeners.
DEAD_CARD = """$("#l-card").replaceWith($("#l-card").cloneNode(true));"""

LABELS_PRELUDE = (
    base_prelude()
    + """
const face = () => ["#l-source", "#l-title", "#l-excerpt"].map((s) => $(s).textContent);
const progress = () => $("#l-progress").textContent;
const dealt = (title) => waitFor(() => visible($("#l-card")) && $("#l-title").textContent === title,
  `the card titled ${title}`);
const pressAnswer = (label) => $(`#l-actions [data-label="${label}"]`).click();
"""
)


def _posted(*wanted: dict) -> Callable[[LabelsFixture], str | None]:
    """A receipt: the stand-in `/api/labels` took exactly these answers, in order."""
    return lambda fixture: None if fixture.posts == list(wanted) else f"posts: {fixture.posts}"


def answer_body(label: str, index: int = 0) -> dict:
    return {"url": ITEMS[index][0], "label": label}


# Words for what the pipeline decided; none may show on the page.
VERDICT_WORDS = ("accepted", "rejected", "pending", "filtered", "score", "tier", "news")

_CHECKS: list[Check] = [
    Check(
        id="card-shows-the-first-item",
        fixture="sample",
        path=PAGE,
        act="""await dealt("Title One");""",
        script="""
            const wanted = ["Source A", "Title One", "Excerpt of the first article."];
            expect(same(face(), wanted), `face: ${JSON.stringify(face())}`);
            expect(progress() === "0 of 3 answered · 3 remaining", `progress: ${progress()}`);
            expect($("#l-title").lang === "" && $("#l-excerpt").lang === "", "lang is set");
        """,
        sabotage="""$("#l-excerpt").textContent = "";""",
        receipt=_posted(),
    ),
    Check(
        id="card-shows-no-verdict",
        fixture="sample",
        path=PAGE,
        act="""await dealt("Title One");""",
        script=f"""
            const text = document.body.innerText.toLowerCase();
            const shown = {json.dumps(VERDICT_WORDS)}.filter((word) => text.includes(word));
            expect(!shown.length, `the page says ${{shown.join(", ")}}`);
            expect(!$$(".state, .pill, .score").length, "a verdict component is on the page");
        """,
        sabotage="""
            $("#l-card").insertAdjacentHTML("beforeend",
              '<span class="state rejected">rejected</span>');
        """,
    ),
    Check(
        id="markup-in-a-title-is-text",
        fixture="sample",
        path=PAGE,
        act="""
            await dealt("Title One");
            pressAnswer("skip");
            await dealt("Title Two");
            pressAnswer("skip");
            await dealt("<b>Three</b>");
        """,
        script="""
            expect(!$("#l-title b"), "the title rendered as markup");
            expect($("#l-excerpt").hidden, "an empty excerpt shows");
        """,
        sabotage="""$("#l-title").innerHTML = $("#l-title").textContent;""",
        receipt=_posted(answer_body("skip", 0), answer_body("skip", 1)),
    ),
    *(
        Check(
            id=f"{label}-sends-its-answer-and-deals-the-next",
            fixture="sample",
            path=PAGE,
            act=f"""
                await dealt("Title One");
                pressAnswer({json.dumps(label)});
            """,
            script="""
                await dealt("Title Two");
                expect(progress() === "1 of 3 answered · 2 remaining", `progress: ${progress()}`);
                expect(!$("#l-actions button:disabled"), "a button stays disabled");
            """,
            sabotage_preload=answer_as("up" if label == "skip" else "skip"),
            receipt=_posted(answer_body(label)),
        )
        for label in ("up", "down", "skip")
    ),
    *(
        Check(
            id=f"swipe-{label}-answers-{label}",
            fixture="sample",
            path=PAGE,
            act="""await dealt("Title One");""",
            gestures=(
                {"press": "#l-card", "pointer": "mouse"},
                {"move": 200 if label == "up" else -200},
                {"release": True},
            ),
            script="""await dealt("Title Two");""",
            sabotage=DEAD_CARD,
            receipt=_posted(answer_body(label)),
        )
        for label in ("up", "down")
    ),
    Check(
        id="tap-opens-the-article",
        fixture="sample",
        path=PAGE,
        preload=RECORD_OPEN,
        act="""await dealt("Title One");""",
        gestures=({"press": "#l-card", "pointer": "mouse"}, {"release": True}),
        script="""
            const wanted = [["https://example.test/one", "_blank", "noopener"]];
            expect(same(window.__opened, wanted), `opened: ${JSON.stringify(window.__opened)}`);
        """,
        sabotage=DEAD_CARD,
        receipt=_posted(),
    ),
    Check(
        id="reduced-motion-deals-the-next",
        fixture="sample",
        path=PAGE,
        media=REDUCED_MOTION,
        act="""
            await dealt("Title One");
            pressAnswer("up");
        """,
        script="""
            await waitFor(() => $("#l-title").textContent === "Title Two", "the next card", 2000);
        """,
        sabotage_preload=MOTION_ALLOWED,
        receipt=_posted(answer_body("up")),
    ),
    *(
        Check(
            id=f"refused-answer-{name}",
            fixture=fixture,
            path=PAGE,
            act="""
                await dealt("Title One");
                pressAnswer("up");
                await waitFor(() => !$("#l-error").hidden, "the error notice");
            """,
            script=f"""
                const text = $("#l-error").textContent;
                expect(visible($("#l-error")), "the notice is hidden");
                expect($("#l-error").getAttribute("role") === "alert", "the notice is silent");
                expect(text.includes({json.dumps(reason)}), `notice: ${{text}}`);
                expect($("#l-title").textContent === "Title One", "the card moved on");
                expect(!$("#l-actions button:disabled"), "a button stays disabled");
                expect(progress() === "0 of 3 answered · 3 remaining", `progress: ${{progress()}}`);
            """,
            sabotage="""$("#l-error").hidden = true;""",
            receipt=_posted(answer_body("up")),
        )
        for name, fixture, reason in (
            ("says-why", "answer-fails", "HTTP 500): D1 is unreachable"),
            ("says-sign-in-again", "signed-out", "sign in again"),
        )
    ),
    *(
        Check(
            id=f"{kind}-says-what-to-run",
            fixture=kind,
            path=PAGE,
            act="""await waitFor(() => !$("#l-empty").hidden, "the closing sentence");""",
            script=f"""
                expect(!visible($("#l-card")) && !visible($("#l-actions")), "a card shows");
                expect(visible($("#l-empty")), "the closing sentence is hidden");
                expect($("#l-empty").textContent.includes({json.dumps(command)}),
                  `says: ${{$("#l-empty").textContent}}`);
                expect(progress() === {json.dumps(shown)}, `progress: ${{progress()}}`);
            """,
            sabotage="""$("#l-empty").hidden = true;""",
        )
        for kind, command, shown in (
            ("all-answered", "cyris labels report", "3 of 3 answered · 0 remaining"),
            ("no-sample", "cyris labels draw", "No sample"),
        )
    ),
    Check(
        id="fits-375",
        fixture="sample",
        path=PAGE,
        width=375,
        act="""await dealt("Title One");""",
        script="""
            const problem = overflow("labels");
            expect(!problem, problem);
            const tops = $$("#l-actions .btn").map((b) => b.getBoundingClientRect().top);
            expect(new Set(tops).size === 1, `the answers sit on ${new Set(tops).size} rows`);
        """,
        sabotage="""$(".deck-wrap").style.minWidth = "600px";""",
    ),
    Check(
        id="tap-answers-375",
        fixture="sample",
        path=PAGE,
        width=375,
        act="""await dealt("Title One");""",
        script="""expectTapTargets("#l-actions .btn");""",
        sabotage=restyle("#l-actions .btn { height: 30px !important; }"),
    ),
    Check(
        id="tap-site-bar-375",
        fixture="sample",
        path=PAGE,
        width=375,
        act="""await waitFor(() => visible($(".site-nav a.gated")), "the archive link");""",
        script="""expectTapTargets(".brand, .site-nav a");""",
        sabotage=NO_SITE_BAR_AREAS,
    ),
]
# The page never scrolls sideways at the largest type size either.
_CHECKS += [
    largest_twin(check, check.id.replace("fits-", "fits-largest-"), "sample-largest")
    for check in _CHECKS
    if check.id == "fits-375"
]

CHECKS = _CHECKS


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
        good = asyncio.run(
            run_all(checks, socket_url, args.self_test, build_fixture, LABELS_PRELUDE)
        )
    raise SystemExit(0 if good else 1)


if __name__ == "__main__":
    main()
