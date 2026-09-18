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
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from aiohttp import web
from cdp_probe import Check, base_prelude, chromium, require_node, run_all
from css_computed import find_browser

from cyris.adapters.output.html_digest import HtmlDigestWriter
from cyris.domain.models import ArticleState, StoredArticle, Tier

DATE = "2026-01-02"
PERIOD = "morning"
PAGE = f"/{DATE}-{PERIOD}-raw.html"

KINDS = ("signed-in", "signed-out", "no-worker", "vote-fails", "fails-once")

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


@dataclass
class Fixture:
    """The raw page and a stand-in `/api/vote`; `posts` is the receipt of every vote."""

    app: web.Application
    posts: list[dict] = field(default_factory=list)
    post_headers: list[dict] = field(default_factory=list)


def build_fixture(kind: str) -> Fixture:
    """Serve the raw page with the `/api/vote` answers of one deployment `kind`.

    `signed-in` answers the probe and takes votes; `signed-out` refuses the
    probe; `no-worker` has no route at all, as on bare pages.dev; `vote-fails`
    signs in but refuses every vote; `fails-once` refuses only the first vote.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown fixture {kind!r}")
    page = render_page()
    app = web.Application()
    fixture = Fixture(app)

    async def serve_page(_: web.Request) -> web.Response:
        return web.Response(text=page, content_type="text/html")

    async def probe(_: web.Request) -> web.Response:
        if kind == "signed-out":
            return web.json_response({"authorized": False}, status=401)
        return web.json_response({"authorized": True})

    async def vote(request: web.Request) -> web.Response:
        fixture.posts.append(await request.json())
        fixture.post_headers.append(dict(request.headers))
        refused = kind == "vote-fails" or (kind == "fails-once" and len(fixture.posts) == 1)
        if refused:
            return web.json_response({"ok": False}, status=502)
        return web.json_response({"ok": True})

    app.router.add_get(PAGE, serve_page)
    if kind != "no-worker":
        app.router.add_get("/api/vote", probe)
        app.router.add_post("/api/vote", vote)
    return fixture


def registry_problems(checks: Iterable[Check]) -> list[str]:
    """Name every check the self-test could not trust: repeated, unsabotaged, or unserved."""
    problems, seen = [], set()
    for check in checks:
        if check.id in seen:
            problems.append(f"{check.id}: named twice")
        seen.add(check.id)
        if not (check.sabotage.strip() or check.sabotage_preload):
            problems.append(f"{check.id}: no sabotage")
        if check.fixture not in KINDS:
            problems.append(f"{check.id}: unknown fixture {check.fixture}")
    return problems


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

# Installed before the page loads: a new tab is recorded rather than opened.
RECORD_OPEN = """
window.__opened = [];
window.open = (...args) => { window.__opened.push(args); return null; };
"""

RAW_PRELUDE = (
    base_prelude()
    + """
const rowOf = (title) => $$("a[target=_blank]").find((a) => a.textContent === title).parentElement;
const voteButton = (title, vote) => $(`.promote-btn[data-vote="${vote}"]`, rowOf(title));
const marked = (title, vote, mark) => voteButton(title, vote).classList.contains(mark);
const storedVotes = () => JSON.parse(localStorage.getItem("cyris-votes") || "{}");
const signedIn = () => waitFor(() => visible(voteButton("Pending Two", "up")), "the vote buttons");
"""
)


def vote_body(slug: str, vote: str) -> dict:
    return {"url": url_of(slug), "vote": vote, "digest_date": DATE}


def _posted(*wanted: dict) -> Callable[[Fixture], str | None]:
    """A receipt: the stand-in `/api/vote` took exactly these vote bodies, in order."""
    return lambda fixture: None if fixture.posts == list(wanted) else f"posts: {fixture.posts}"


CHECKS: list[Check] = [
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
    Check(
        id="list-vote-failure-marks-error",
        fixture="vote-fails",
        path=PAGE,
        act="""
            await signedIn();
            voteButton("Pending Two", "up").click();
            await waitFor(() => marked("Pending Two", "up", "error"), "the vote marked failed");
        """,
        script="""
            const up = voteButton("Pending Two", "up");
            expect(up.classList.contains("error"), "up is not marked failed");
            expect(!up.classList.contains("done"), "up is marked done");
            const stored = JSON.stringify(storedVotes());
            expect(!stored.includes("pending-two"), `stored: ${stored}`);
        """,
        sabotage="""$$(".promote-btn").forEach((b) => b.classList.remove("error"));""",
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
]


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
