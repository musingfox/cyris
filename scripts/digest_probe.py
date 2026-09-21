#!/usr/bin/env python3
"""Drive the digest page in headless Chromium and check what a reader actually gets.

pytest holds the digest's markup and CSS, but not where a browser puts things:
whether each kind of item keeps its ↑ and ↓ side by side, on the row of the
meta before them. This runs those checks against the page `render` writes for
a digest holding every kind of item, served in-process next to a stand-in for
the app Worker's `/api/vote`, whose every vote request is kept as a receipt.

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
from cyris.domain.models import DigestContent, DigestItem, DigestSection, UsageStats

DATE = "2026-01-02"
PERIOD = "morning"
PAGE = f"/{HtmlDigestWriter.digest_filename(DATE, PERIOD)}"

KINDS = ("signed-in", "signed-in-largest", "vote-fails")

# Every element a vote group sits in, one per promote_btn call site.
ITEM_SELECTORS = (
    ".lead-story",
    ".featured-item",
    ".news-cluster",
    ".article-item",
    ".attention-item",
    ".headline-item",
)

# The widths the ticket accepts the page at.
WIDTHS = (360, 880, 1000, 1440)

# Widths the masthead sets title and stats card side by side at, densest just above
# the breakpoint, where the fallback-font title is widest for its column.
HEAD_WIDTHS = (721, 740, 760, 800, 880, 1000, 1440)

# The head width the title has the least room at, so the largest type size is checked there.
TIGHTEST_HEAD_WIDTH = 721


def url_of(slug: str) -> str:
    return f"https://example.test/{slug}"


def _item(slug: str, title: str, sources: list[str], **extra) -> DigestItem:
    urls = [url_of(f"{slug}-{n}") for n in range(len(sources))]
    summary = f"{title}: a summary."
    return DigestItem(title=title, summary=summary, sources=sources, urls=urls, **extra)


def content() -> DigestContent:
    """A digest with every item kind, a folded cluster and a folded headline."""
    features = [_item("lead", "The lead story of the issue", ["Lead Source"], score=8.5)]
    features += [
        _item(f"feature-{n}", f"Feature story {n}", [f"Source {n}"], score=8.0 - n / 10)
        for n in range(1, 6)
    ]
    return DigestContent(
        date=DATE,
        period=PERIOD,
        sources_processed=12,
        articles_received=40,
        articles_included=20,
        usage=UsageStats(input_tokens=1000, output_tokens=500, api_calls=3, model="probe"),
        featured_articles=[DigestSection(heading="Top", items=features)],
        # The busy cluster comes first: a click on the first cluster votes on three URLs.
        news_clusters=[
            DigestSection(
                heading="A busy topic",
                items=[_item("busy", "Busy", ["Wire A", "Wire B", "Wire C"])],
                story_id=f"{DATE}-{PERIOD}-0",
            ),
            DigestSection(
                heading="A quiet topic",
                items=[_item("quiet", "Quiet", ["Wire D", "Wire E"])],
                story_id=f"{DATE}-{PERIOD}-1",
            ),
        ],
        fan_sections=[
            DigestSection(
                heading="Followed",
                items=[_item(f"fan-{n}", f"Followed story {n}", ["Fan"]) for n in range(1, 3)],
            )
        ],
        # One section, so the five items share one list.
        attention_sections=[
            DigestSection(
                heading="Worth watching",
                description="Things to keep an eye on.",
                items=[_item(f"radar-{n}", f"Radar item {n}", ["Blog"]) for n in range(1, 6)],
            )
        ],
        filtered_headlines=[
            _item("wire-1", "Headline one", ["Wire"]),
            _item(
                "wire-2",
                "Headline two",
                ["Wire"],
                ref_urls=[url_of(f"ref-{n}") for n in range(1, 4)],
            ),
            _item("wire-3", "Headline three", ["Wire"]),
        ],
        triage_pending_count=4,
    )


def render_page() -> str:
    with tempfile.TemporaryDirectory(prefix="digest-probe-") as unused:
        return HtmlDigestWriter(Path(unused)).render(content())


def build_fixture(kind: str) -> VoteFixture:
    """Serve the digest page with the `/api/vote` answers of one deployment `kind`.

    Every kind answers the probe signed in; `signed-in` takes every vote,
    `signed-in-largest` does too on the page served at the largest type size,
    and `vote-fails` refuses every vote.
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
        return web.json_response({"authorized": True})

    async def vote(request: web.Request) -> web.Response:
        fixture.posts.append(await request.json())
        fixture.post_headers.append(dict(request.headers))
        if kind == "vote-fails":
            return web.json_response({"ok": False}, status=502)
        return web.json_response({"ok": True})

    app.router.add_get(PAGE, serve_page)
    app.router.add_get("/api/vote", probe)
    app.router.add_post("/api/vote", vote)
    return fixture


def first_vote_urls() -> dict[str, list[str]]:
    """What the first vote group of each item kind sends a vote for."""
    digest = content()
    features = [item for section in digest.featured_articles for item in section.items]
    return {
        ".lead-story": features[0].urls,
        ".featured-item": features[1].urls,
        ".news-cluster": digest.news_clusters[0].items[0].urls,
        ".article-item": digest.fan_sections[0].items[0].urls,
        ".attention-item": digest.attention_sections[0].items[0].urls,
        ".headline-item": digest.filtered_headlines[0].urls,
    }


def _voted(urls: list[str]) -> Callable[[VoteFixture], str | None]:
    """A receipt: one bare up vote arrived for each of `urls`, in any order, with no credential."""
    wanted = sorted(json.dumps({"url": url, "vote": "up", "digest_date": DATE}) for url in urls)

    def receipt(fixture: VoteFixture) -> str | None:
        if not fixture.posts:
            return "no vote arrived"
        sent = [sorted(key.lower() for key in headers) for headers in fixture.post_headers]
        if any("authorization" in headers for headers in sent):
            return f"a vote carried a credential: {sent}"
        bodies = sorted(
            json.dumps({key: post.get(key) for key in ("url", "vote", "digest_date")})
            for post in fixture.posts
        )
        extra = [sorted(set(post) - {"url", "vote", "digest_date"}) for post in fixture.posts]
        if bodies != wanted or any(extra):
            return f"posts: {fixture.posts}"
        return None

    return receipt


def _up(item: str) -> str:
    return f'{item} .promote-btn[data-vote="up"]'


def strip_mark_sabotage(mark: str) -> str:
    """A sabotage: the page takes `mark` off every vote button as soon as it is put on."""
    return f"""new MutationObserver(() => {{
        $$(".promote-btn.{mark}").forEach((b) => b.classList.remove("{mark}"));
    }}).observe(document.body, {{subtree: true, attributes: true, attributeFilter: ["class"]}});"""


# The column counts the body grids must show where the ticket names a width; every
# other sampled width only has to agree between the two grids.
GRID_COLUMNS = {360: 1, 880: 2, 1000: 2, 1440: 3}

# Chromium keeps one profile for the whole run, and a fixture's origin is only as
# fresh as its port, so every check starts from a browser that has voted nothing.
FORGET_VOTES = "localStorage.removeItem('cyris-votes');\n"

DIGEST_PRELUDE = (
    base_prelude()
    + f"const ITEM_SELECTORS = {json.dumps(ITEM_SELECTORS)};\n"
    + """
const signedIn = () => waitFor(() => {
  const groups = $$(".vote-group");
  return groups.length && groups.every(visible);
}, "the vote buttons");
// Fails on each problem `measure` names for a shown vote group, and on each item kind
// showing none.
const expectEveryItem = (measure) => {
  const problems = [];
  for (const item of ITEM_SELECTORS) {
    const groups = $$(`${item} .vote-group`).filter(visible);
    if (!groups.length) problems.push(`${item}: no vote group shows`);
    for (const group of groups) {
      const problem = measure(group);
      if (problem) problems.push(`${item}: ${problem}`);
    }
  }
  expect(!problems.length, problems.join("; "));
};
// The page scrolls smoothly, and a press must land where the button has settled.
const bringIntoView = (selector) => {
  $(selector).scrollIntoView({block: "center", behavior: "instant"});
};
// A grid's column count: the distinct left edges among its items.
const columns = (selector) => new Set(
  [...$(selector).children].map((item) => Math.round(item.getBoundingClientRect().left))).size;
const oneRow = (group) => {
  const up = $('[data-vote="up"]', group).getBoundingClientRect();
  const down = $('[data-vote="down"]', group).getBoundingClientRect();
  return Math.abs(up.top - down.top) > 1 ? `up at ${up.top}, down at ${down.top}` : "";
};
// How far the issue title's text runs past the stats card's left edge. The title's
// box is its grid column, so the text's own extent is read, not the box.
const titleOverrun = () => {
  const range = document.createRange();
  range.selectNodeContents($(".issue-title"));
  return range.getBoundingClientRect().right - $(".stats-card").getBoundingClientRect().left;
};
const besideMeta = (group) => {
  const before = group.previousElementSibling;
  if (!before) return "nothing before the vote group";
  const a = group.getBoundingClientRect();
  const b = before.getBoundingClientRect();
  return a.top < b.bottom && b.top < a.bottom
    ? ""
    : `votes span ${a.top}-${a.bottom}, the meta before them ${b.top}-${b.bottom}`;
};
"""
)


_CHECKS: list[Check] = [
    *(
        Check(
            id=f"votes-one-row-{width}",
            fixture="signed-in",
            path=PAGE,
            width=width,
            act="await signedIn();",
            script="expectEveryItem(oneRow);",
            sabotage="""$$(".vote-group").forEach((g) => { g.style.flexDirection = "column"; });""",
        )
        for width in WIDTHS
    ),
    *(
        Check(
            id=f"votes-beside-meta-{width}",
            fixture="signed-in",
            path=PAGE,
            width=width,
            act="await signedIn();",
            script="expectEveryItem(besideMeta);",
            sabotage="""$$(".vote-group").forEach((g) => {
                g.style.display = "flex";
                g.style.width = "100%";
            });""",
        )
        for width in WIDTHS
    ),
    # Both body grids hold five items, so each shows as many columns as fit.
    *(
        Check(
            id=f"grids-agree-{width}",
            fixture="signed-in",
            path=PAGE,
            width=width,
            act="await signedIn();",
            script=f"""
                const featured = columns(".featured-grid");
                const attention = columns(".attention-list");
                const pinned = {json.dumps(GRID_COLUMNS.get(width))};
                const agree = featured === attention && (pinned === null || featured === pinned);
                expect(agree, `featured ${{featured}}, attention ${{attention}}, want ${{pinned}}`);
            """,
            sabotage="""
                $(".attention-list").style.gridTemplateColumns = "repeat(4, minmax(0, 1fr))";
            """,
        )
        for width in (360, 721, 880, 1000, 1100, 1160, 1440)
    ),
    # The page never scrolls sideways, even in the fallback font the probe renders.
    *(
        Check(
            id=f"fits-{width}",
            fixture="signed-in",
            path=PAGE,
            width=width,
            act="await signedIn();",
            script=f"""const problem = overflow("{width}"); expect(!problem, problem);""",
            sabotage=f"""$(".container").style.minWidth = "{width + 300}px";""",
        )
        for width in WIDTHS
    ),
    # Above the breakpoint the title sits beside the stats card, and in the fallback
    # font the probe renders, it must still stop short of it.
    *(
        Check(
            id=f"head-fits-{width}",
            fixture="signed-in",
            path=PAGE,
            width=width,
            script="""
                const overrun = titleOverrun();
                expect(overrun <= 0, `the title runs ${overrun}px into the stats card`);
            """,
            sabotage="""$(".headline-block").style.gridTemplateColumns = "minmax(0, 1fr) 100%";""",
        )
        for width in HEAD_WIDTHS
    ),
    # A real pointer press on each item kind's first up button: the button is hit, every
    # URL of its group is voted on, and the button is marked. The phone run is caught by
    # a page that drops the mark, the desktop run by a page that sends a credential.
    *(
        Check(
            id=f"vote-click-{item.lstrip('.')}-{width}",
            fixture="signed-in",
            path=PAGE,
            width=width,
            act=f"""
                await signedIn();
                bringIntoView({json.dumps(_up(item))});
            """,
            gestures=({"press": _up(item), "pointer": "mouse"}, {"release": True}),
            script=f"""
                const up = $({json.dumps(_up(item))});
                await waitFor(() => up.classList.contains("done"), "the vote marked done");
            """,
            sabotage=strip_mark_sabotage("done") if width == 360 else "",
            sabotage_preload=SEND_CREDENTIAL if width == 1440 else "",
            receipt=_voted(urls),
        )
        for item, urls in first_vote_urls().items()
        for width in (360, 1440)
    ),
    Check(
        id="vote-failure-marks-error",
        fixture="vote-fails",
        path=PAGE,
        act=f"""
            await signedIn();
            bringIntoView({json.dumps(_up(".news-cluster"))});
        """,
        gestures=({"press": _up(".news-cluster"), "pointer": "mouse"}, {"release": True}),
        script=f"""
            const up = $({json.dumps(_up(".news-cluster"))});
            await waitFor(() => up.classList.contains("error"), "the vote marked failed");
            expect(!up.classList.contains("done"), "up is marked done");
            const stored = localStorage.getItem("cyris-votes");
            expect(!stored || stored === "{{}}", `stored: ${{stored}}`);
        """,
        sabotage=strip_mark_sabotage("error"),
        receipt=_voted(first_vote_urls()[".news-cluster"]),
    ),
]
# A sabotage: the masthead stops clipping the title the fallback font sets too wide.
UNCLIPPED_MASTHEAD = """$(".headline-block").style.overflowX = "visible";"""


def _largest(check: Check) -> Check:
    """`check` again at the largest type size, under its own name."""
    twin = largest_twin(check, check.id.replace("fits-", "fits-largest-"), "signed-in-largest")
    # The masthead's `overflow-x: clip` is load-bearing at this size alone: on a phone the
    # fallback-font title is wider than the viewport at 1.125 and fits at 1. So this twin
    # takes the clip away instead of inheriting its twin's page-width sabotage.
    return (
        dataclasses.replace(twin, sabotage=UNCLIPPED_MASTHEAD) if check.id == "fits-360" else twin
    )


# The page never scrolls sideways at the largest type size either, and the title still
# clears the stats card where it has the least room.
_CHECKS += [
    _largest(check)
    for check in _CHECKS
    if check.id in {f"fits-{width}" for width in WIDTHS}
    or check.id == f"head-fits-{TIGHTEST_HEAD_WIDTH}"
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
        good = asyncio.run(
            run_all(checks, socket_url, args.self_test, build_fixture, DIGEST_PRELUDE)
        )
    raise SystemExit(0 if good else 1)


if __name__ == "__main__":
    main()
