#!/usr/bin/env python3
"""Drive the digest page in headless Chromium and check what a reader actually gets.

pytest holds the digest's markup and CSS, but not where a browser puts things:
whether each kind of item keeps its ↑ and ↓ side by side, on the row of the
meta before them. This runs those checks against the page `render` writes for
a digest holding every layer, pebble, sand and water, served in-process next to a stand-in for
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
    CENTRED_VOTE_AREAS,
    NO_BUTTON_AREAS,
    NO_SITE_BAR_AREAS,
    SEND_CREDENTIAL,
    Check,
    VoteFixture,
    at_largest_type_scale,
    at_smallest_type_scale,
    base_prelude,
    chromium,
    largest_twin,
    require_node,
    restyle,
    run_all,
    smallest_twin,
)
from css_computed import find_browser

from cyris.adapters.output.html_digest import HtmlDigestWriter
from cyris.domain.layers import layer_by_lane
from cyris.domain.models import (
    DigestContent,
    DigestItem,
    DigestSection,
    UsageStats,
)

DATE = "2026-01-02"
PERIOD = "morning"
PAGE = f"/{HtmlDigestWriter.digest_filename(DATE, PERIOD)}"
ARCHIVE = "/index.html"

KINDS = ("signed-in", "signed-in-largest", "signed-in-smallest", "vote-fails")

# Every element a vote group sits in, one per layer: a pebble card, a sand block, a water row.
ITEM_SELECTORS = (
    ".featured-item",
    ".news-cluster",
    ".headline-item",
)

# The prose blocks the fixture draws as wide as the container, where an uncapped one
# would run the full page width.
PROSE_SELECTORS = (
    ".featured-item .summary",
    ".news-cluster .summary",
    ".headline-item .summary",
)

# The widths the ticket accepts the page at.
WIDTHS = (360, 880, 1000, 1440)

# Widths the masthead sets title and stats card side by side at, densest just above
# the breakpoint, where the fallback-font title is widest for its column.
HEAD_WIDTHS = (721, 740, 760, 800, 880, 1000, 1440)

# The head width the title has the least room at, so the largest type size is checked there.
TIGHTEST_HEAD_WIDTH = 721


# It ends in a URL, the one token no space breaks, as an excerpt or a summary can.
LONG_WIRE_SUMMARY = (
    "The regulator approved the merger of the two largest regional carriers on condition"
    " that they sell eleven airport slots and keep fares on four routes capped, per"
    " https://regulator.example.test/decisions/2026/regional-carriers-merger-approval-conditions"
)


def url_of(slug: str) -> str:
    return f"https://example.test/{slug}"


def _item(slug: str, title: str, sources: list[str], **extra) -> DigestItem:
    urls = [url_of(f"{slug}-{n}") for n in range(len(sources))]
    summary = extra.pop("summary", f"{title}: a summary.")
    return DigestItem(title=title, summary=summary, sources=sources, urls=urls, **extra)


def _members(slug: str, sources: list[str]) -> list[DigestItem]:
    """A cluster's articles, as `cluster_news` keeps them: each its own title and excerpt."""
    return [
        _item(f"{slug}-{n}", f"Report {n} of the {slug} topic", [source], passed_through=True)
        for n, source in enumerate(sources)
    ]


def content() -> DigestContent:
    """A digest with every layer: pebble cards, sand blocks with their folds, water rows."""
    # The lead's summary and tags make its card taller than the two beside it, so the
    # meta rows of one grid row only line up when the card puts its meta at the bottom.
    lead_summary = "The lead story runs long enough to wrap across several lines of its card. " * 4
    features = [
        _item(
            "lead",
            "The lead story of the issue",
            ["Lead Source"],
            score=8.5,
            summary=lead_summary.strip(),
            tags=["Lead", "Long"],
        )
    ]
    features += [
        _item(f"feature-{n}", f"Feature story {n}", [f"Source {n}"], score=8.0 - n / 10)
        for n in range(1, 6)
    ]
    featured = [DigestSection(heading="Top", items=features)]
    busy = _item("busy", "Busy", ["Wire A", "Wire B", "Wire C"])
    busy_members = _members("busy", busy.sources)
    busy.urls = [m.urls[0] for m in busy_members]
    return DigestContent(
        date=DATE,
        period=PERIOD,
        sources_processed=12,
        articles_received=40,
        articles_included=20,
        usage=UsageStats(input_tokens=1000, output_tokens=500, api_calls=3, model="probe"),
        featured_articles=featured,
        # The busy topic comes first: a click on the first sand block votes on three URLs.
        # The quiet one was stored before members, so its fold is built from its links.
        news_clusters=[
            DigestSection(
                heading="A busy topic",
                items=[busy],
                members=busy_members,
                tags=["Anthropic", "AI"],
                story_id=f"{DATE}-{PERIOD}-0",
            ),
            DigestSection(
                heading="A quiet topic",
                items=[_item("quiet", "Quiet", ["Wire D", "Wire E"])],
                story_id=f"{DATE}-{PERIOD}-1",
            ),
        ],
        # Above the pebbles, links only: no vote group sits in Tracking.
        tracked_topics=[
            DigestSection(
                heading="A tracked topic",
                items=[_item(f"tracked-{n}", f"Tracked hit {n}", ["Wire"]) for n in range(1, 3)],
            )
        ],
        fan_sections=[
            DigestSection(
                heading="Followed",
                items=[_item(f"fan-{n}", f"Followed story {n}", ["Fan"]) for n in range(1, 3)],
            )
        ],
        # Five low-score read items: each is a pebble card, after the Features.
        attention_sections=[
            DigestSection(
                heading="Worth watching",
                description="Things to keep an eye on.",
                items=[_item(f"radar-{n}", f"Radar item {n}", ["Blog"]) for n in range(1, 6)],
            )
        ],
        # Three water rows: one with a sentence as long as the filter writes, one with a short
        # one, and one the filter wrote no summary for.
        filtered_headlines=[
            _item("wire-1", "Headline one", ["Wire"], summary=LONG_WIRE_SUMMARY),
            _item(
                "wire-2",
                "Headline two",
                ["Wire"],
                ref_urls=[url_of(f"ref-{n}") for n in range(1, 4)],
            ),
            _item("wire-3", "Headline three", ["Wire"], summary=""),
        ],
        triage_pending_count=4,
    )


def render_page() -> str:
    with tempfile.TemporaryDirectory(prefix="digest-probe-") as unused:
        return HtmlDigestWriter(Path(unused)).render(content())


def render_archive() -> str:
    """The archive of this issue and the one before it: a headline card and one row."""
    issues = [(DATE, PERIOD), ("2026-01-01", "evening")]
    files = (HtmlDigestWriter.digest_filename, HtmlDigestWriter.raw_filename)
    names = [name(*issue) for issue in issues for name in files]
    with tempfile.TemporaryDirectory(prefix="digest-probe-") as unused:
        return HtmlDigestWriter(Path(unused)).render_index(names, content=content())


def build_fixture(kind: str) -> VoteFixture:
    """Serve the digest page with the `/api/vote` answers of one deployment `kind`.

    Every kind answers the probe signed in; `signed-in` takes every vote,
    `signed-in-largest` and `signed-in-smallest` do too on the page served at
    the largest and the smallest type size, and `vote-fails` refuses every vote.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown fixture {kind!r}")
    page, archive = render_page(), render_archive()
    if kind.endswith("-largest"):
        page = at_largest_type_scale(page)
    if kind.endswith("-smallest"):
        page = at_smallest_type_scale(page)
    app = web.Application()
    fixture = VoteFixture(app)

    async def serve_page(_: web.Request) -> web.Response:
        return web.Response(text=page, content_type="text/html")

    async def serve_archive(_: web.Request) -> web.Response:
        return web.Response(text=archive, content_type="text/html")

    async def probe(_: web.Request) -> web.Response:
        return web.json_response({"authorized": True})

    async def vote(request: web.Request) -> web.Response:
        fixture.posts.append(await request.json())
        fixture.post_headers.append(dict(request.headers))
        if kind == "vote-fails":
            return web.json_response({"ok": False}, status=502)
        return web.json_response({"ok": True})

    app.router.add_get(PAGE, serve_page)
    app.router.add_get(ARCHIVE, serve_archive)
    app.router.add_get("/api/vote", probe)
    app.router.add_post("/api/vote", vote)
    return fixture


def first_vote_urls() -> dict[str, list[str]]:
    """What the first vote group of each layer sends a vote for."""
    layers = layer_by_lane(content())
    return {
        ".featured-item": layers.pebbles[0].item.urls,
        ".news-cluster": layers.sand[0].vote.urls,
        ".headline-item": layers.water[0].item.urls,
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
    # Signed in, every pebble, sand block and water row shows its one vote group with both
    # buttons drawn, and Tracking shows none. The stylesheet hides them all until the
    # probe's {"authorized": true} lifts that gate; a gate left down is the sabotage.
    *(
        Check(
            id=f"votes-shown-once-authorized-{width}",
            fixture="signed-in",
            path=PAGE,
            width=width,
            script="""
                const items = ITEM_SELECTORS.flatMap((selector) => $$(selector));
                await waitFor(() => $$(".vote-group").filter(visible).length === items.length,
                  `${items.length} vote groups shown`);
                const problems = items.flatMap((item) => {
                  const shown = $$(".vote-group", item).filter(visible);
                  const drawn = $$(".promote-btn", item).filter((b) => {
                    const r = b.getBoundingClientRect();
                    return visible(b) && r.width > 0 && r.height > 0;
                  });
                  return shown.length === 1 && drawn.length === 2
                    ? [] : [`${item.className}: ${shown.length} groups, ${drawn.length} buttons`];
                });
                expect(!problems.length, problems.join("; "));
                expect(!$$(".thematic-block .vote-group").length, "Tracking carries a vote");
            """,
            sabotage=restyle(".vote-group { display: none !important; }"),
        )
        for width in (360, 1440)
    ),
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
    # At the widest width, prose that spans the container still stops at --measure-wide.
    Check(
        id="prose-measure-1440",
        fixture="signed-in",
        path=PAGE,
        width=1440,
        act="await signedIn();",
        script=f"""
            const measure = parseFloat(
                getComputedStyle(document.documentElement).getPropertyValue("--measure-wide"));
            expect(Number.isFinite(measure) && measure > 0, `invalid --measure-wide: ${{measure}}`);
            for (const selector of {json.dumps(PROSE_SELECTORS)}) {{
                const blocks = $$(selector);
                expect(blocks.length, `no ${{selector}} on the page`);
                const wide = blocks.map((b) => b.getBoundingClientRect().width)
                    .filter((w) => w > measure + 0.5);
                expect(!wide.length, `${{selector}} is ${{wide}}px wide, past ${{measure}}px`);
            }}
        """,
        sabotage=f"""$$({json.dumps(", ".join(PROSE_SELECTORS))})
            .forEach((b) => {{ b.style.maxWidth = "none"; }});""",
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
    # The reading progress bar has one segment per section, and its fill grows as the
    # reader scrolls down. A fill pinned at nothing is the sabotage.
    *(
        Check(
            id=f"progress-moves-on-scroll-{width}",
            fixture="signed-in",
            path=PAGE,
            width=width,
            act="await signedIn();",
            script="""
                const segments = $$(".read-progress span");
                const sections = $$("main > .section");
                expect(segments.length === sections.length,
                  `${segments.length} segments for ${sections.length} sections`);
                const filled = () => $$(".read-progress i")
                  .reduce((sum, fill) => sum + fill.getBoundingClientRect().width, 0);
                const settle = () => new Promise((done) =>
                  requestAnimationFrame(() => requestAnimationFrame(done)));
                window.scrollTo({top: 0, behavior: "instant"});
                await settle();
                const before = filled();
                const half = document.documentElement.scrollHeight / 2;
                window.scrollTo({top: half, behavior: "instant"});
                await settle();
                const after = filled();
                expect(after > before + 1, `the fill went from ${before}px to ${after}px`);
            """,
            sabotage=restyle(".read-progress i { width: 0 !important; }"),
        )
        for width in (360, 1440)
    ),
    # On a phone the bar adds no sideways scroll, and it sits inside the site bar's box,
    # clear of its links, so it covers nothing the sticky bar does not already cover:
    # not the site bar's links and not a vote button scrolled beneath it.
    Check(
        id="progress-fits-360",
        fixture="signed-in",
        path=PAGE,
        width=360,
        act="await signedIn();",
        script="""
            await waitFor(() => $(".read-progress"), "the reading progress bar");
            const problem = overflow("360");
            expect(!problem, problem);
            const bar = $(".read-progress").getBoundingClientRect();
            expect(bar.right <= document.documentElement.clientWidth,
              `the bar ends at ${bar.right}px`);
        """,
        # Absolute, not fixed: a fixed box never widens the page's scroll width.
        sabotage=restyle(
            ".read-progress { position: absolute !important; width: 130vw !important; }"
        ),
    ),
    Check(
        id="progress-inside-site-bar-360",
        fixture="signed-in",
        path=PAGE,
        width=360,
        act="await signedIn();",
        script="""
            const settle = () => new Promise((done) =>
              requestAnimationFrame(() => requestAnimationFrame(done)));
            const overlaps = (a, b) => a.left < b.right && b.left < a.right
              && a.top < b.bottom && b.top < a.bottom;
            const problems = [];
            for (const top of [0, document.documentElement.scrollHeight / 2]) {
              window.scrollTo({top, behavior: "instant"});
              await settle();
              const bar = $(".read-progress").getBoundingClientRect();
              const site = $(".site-bar").getBoundingClientRect();
              if (bar.top < site.top || bar.bottom > site.bottom || bar.height <= 0)
                problems.push(`at ${top}: bar ${bar.top}-${bar.bottom}, `
                  + `site bar ${site.top}-${site.bottom}`);
              for (const link of $$(".brand, .site-nav a").filter(visible))
                if (overlaps(bar, link.getBoundingClientRect()))
                  problems.push(`at ${top}: covers ${link.textContent.trim() || "the brand"}`);
              // The bar takes no pointer events, so this reads what lies beneath it: the
              // site bar there, never a vote button or anything else of the page.
              const y = (bar.top + bar.bottom) / 2;
              for (let x = 1; x < bar.right; x += 10) {
                const under = document.elementFromPoint(x, y);
                if (under && !under.closest(".site-bar"))
                  problems.push(`at ${top}: covers ${under.className || under.tagName} at x=${x}`);
              }
            }
            expect(!problems.length, problems.join("; "));
        """,
        sabotage=restyle(".read-progress { top: calc(var(--bar-h) + 40vh) !important; }"),
    ),
    # The meta rows of the pebble cards in one grid row share their bottom edge, however
    # tall each card's own content is. A meta row left where its content ends is the sabotage.
    Check(
        id="pebble-meta-bottoms-1440",
        fixture="signed-in",
        path=PAGE,
        width=1440,
        act="await signedIn();",
        script="""
            const rows = new Map();
            for (const card of $$(".featured-item")) {
              const top = Math.round(card.getBoundingClientRect().top);
              rows.set(top, [...(rows.get(top) || []), card]);
            }
            const shared = [...rows.values()].filter((cards) => cards.length > 1);
            expect(shared.length, "no grid row holds two cards");
            const problems = shared.flatMap((cards) => {
              const bottoms = cards.map((c) => $(".meta", c).getBoundingClientRect().bottom);
              return Math.max(...bottoms) - Math.min(...bottoms) > 1 ? [bottoms.join(", ")] : [];
            });
            expect(!problems.length, `meta bottoms per row: ${problems.join("; ")}`);
        """,
        sabotage=restyle(".featured-item > :last-child { margin-top: 0 !important; }"),
    ),
    # A refused vote's notice follows the meta row inside the card, and the card's slack
    # stays above the meta row, so on a short card the notice sits right below its buttons.
    # Giving the notice a share of the slack, as a bare :last-child rule did, is the sabotage.
    Check(
        id="pebble-vote-error-under-meta-1440",
        fixture="signed-in",
        path=PAGE,
        width=1440,
        act="""
            await signedIn();
            const short = $$(".featured-item")[1];
            showFailure($(".vote-group", short), VOTE_FAILED.notConfigured);
        """,
        script="""
            const [lead, short] = $$(".featured-item");
            const notice = $(".vote-error", short);
            expect(notice, "no failure notice in the card");
            const meta = $(".meta", short).getBoundingClientRect();
            // The notice has no margin of its own, so it touches the row it reports on.
            const gap = notice.getBoundingClientRect().top - meta.bottom;
            expect(gap <= 1, `the notice sits ${gap}px below the meta row`);
            // The card still has slack to give, so a notice that took a share would show.
            const sameRow = lead.offsetHeight === short.offsetHeight;
            const used = [...short.children].reduce((sum, c) => sum + c.offsetHeight, 0);
            expect(sameRow && short.clientHeight > used + 1, "the short card has no slack");
        """,
        sabotage=restyle(".featured-item > :last-child { margin-top: auto !important; }"),
    ),
    # The reading progress bar spans the site bar's inner width, the column its brand and
    # links sit in, not the whole window. A bar stretched edge to edge is the sabotage.
    *(
        Check(
            id=f"progress-in-column-{width}",
            fixture="signed-in",
            path=PAGE,
            width=width,
            act="await signedIn();",
            script="""
                await waitFor(() => $(".read-progress"), "the reading progress bar");
                const inner = $(".site-bar-inner");
                const style = getComputedStyle(inner);
                const box = inner.getBoundingClientRect();
                const left = box.left + parseFloat(style.paddingLeft);
                const right = box.right - parseFloat(style.paddingRight);
                const bar = $(".read-progress span:first-child").getBoundingClientRect();
                const end = $(".read-progress span:last-child").getBoundingClientRect();
                expect(Math.abs(bar.left - left) <= 1 && Math.abs(end.right - right) <= 1,
                  `segments span ${bar.left}-${end.right}, the site bar's column ${left}-${right}`);
            """,
            sabotage=restyle(
                ".read-progress { max-width: none !important; padding: 0 !important; }"
            ),
        )
        for width in (1440, 2000)
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
    # On a phone the fallback-font title fits its column whole at the normal type size;
    # only the largest sets it wider, and there the masthead clips it (`fits-largest-360`).
    Check(
        id="title-whole-360",
        fixture="signed-in",
        path=PAGE,
        width=360,
        script="""
            const range = document.createRange();
            range.selectNodeContents($(".issue-title"));
            const over = range.getBoundingClientRect().right
                - $(".headline-block").getBoundingClientRect().right;
            expect(over <= 0, `the masthead clips ${over}px off the title`);
        """,
        sabotage="""$(".issue-title").style.fontSize = "63px";""",
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
    # A refused vote says why on the line below the row its buttons sit in, and is not kept.
    Check(
        id="vote-failure-says-why",
        fixture="vote-fails",
        path=PAGE,
        act=f"""
            await signedIn();
            bringIntoView({json.dumps(_up(".news-cluster"))});
        """,
        gestures=({"press": _up(".news-cluster"), "pointer": "mouse"}, {"release": True}),
        script=f"""
            const up = $({json.dumps(_up(".news-cluster"))});
            const row = up.closest(".vote-group").parentElement;
            const notice = await waitFor(() => {{
              const next = row.nextElementSibling;
              return next && next.classList.contains("vote-error") && next;
            }}, "the failure notice");
            expect(visible(notice), "the failure notice is hidden");
            expect(notice.getAttribute("role") === "alert", "the notice is not announced");
            expect(notice.textContent.includes("HTTP 502"), `notice: ${{notice.textContent}}`);
            expect(up.classList.contains("error"), "up is not marked failed");
            expect(!up.classList.contains("done"), "up is marked done");
            expect(up.getAttribute("aria-pressed") === "false", "up is pressed");
            const stored = localStorage.getItem("cyris-votes");
            expect(!stored || stored === "{{}}", `stored: ${{stored}}`);
        """,
        sabotage="""new MutationObserver(() => {
            $$(".vote-error").forEach((n) => n.remove());
        }).observe(document.body, {subtree: true, childList: true});""",
        receipt=_voted(first_vote_urls()[".news-cluster"]),
    ),
]
# The longest failure reason, shown under every item kind at once, stays inside the
# box it sits in on a phone, at the largest type size too, and the page does not scroll.
_CHECKS += [
    Check(
        id=f"vote-failure-fits-{width}-{fixture}",
        fixture=fixture,
        path=PAGE,
        width=width,
        act="""
            await signedIn();
            for (const item of ITEM_SELECTORS) {
              showFailure($(`${item} .vote-group`), VOTE_FAILED.notConfigured);
            }
        """,
        script=f"""
            const notices = $$(".vote-error");
            expect(notices.length === ITEM_SELECTORS.length, `${{notices.length}} notices`);
            const right = (el) => el.getBoundingClientRect().right;
            const spilled = notices.filter((n) => n.scrollWidth > n.clientWidth
              || right(n) > right(n.parentElement) + 0.5);
            const under = spilled.map((n) => n.parentElement.className);
            expect(!spilled.length, `spilled under ${{under}}`);
            const problem = overflow("{width}");
            expect(!problem, problem);
        """,
        sabotage="""$$(".vote-error").forEach((n) => { n.style.whiteSpace = "pre"; });""",
    )
    for width, fixture in ((360, "signed-in"), (360, "signed-in-largest"))
]
# Every tap target takes taps over 44 x 44 on a phone, and none covers another.
TAP_WIDTH = 375
_CHECKS += [
    Check(
        id=f"tap-{name}-{TAP_WIDTH}",
        fixture="signed-in",
        path=path,
        width=TAP_WIDTH,
        act="await signedIn();" if path == PAGE else "",
        script=f"expectTapTargets({json.dumps(selector)});",
        sabotage=sabotage,
    )
    for name, path, selector, sabotage in (
        ("votes", PAGE, ".promote-btn", NO_BUTTON_AREAS),
        ("votes-apart", PAGE, ".promote-btn", CENTRED_VOTE_AREAS),
        ("original", PAGE, ".meta > a.orig", restyle(".meta > .orig { min-width: 0 !important; }")),
        (
            "original-clear",
            PAGE,
            ".meta > a.orig",
            restyle(".vote-group { margin-left: 0 !important; }"),
        ),
        ("site-bar", PAGE, ".brand, .site-nav a", NO_SITE_BAR_AREAS),
        ("archive", ARCHIVE, ".btn.sm", NO_BUTTON_AREAS),
    )
]
# The archive's headline card and each row are their issue's Digest link, all but the
# All articles button, which still opens its own page. One script, two sabotages: one
# shrinks the Digest area back to the button, the other paints it over All articles.
EXPECT_ARCHIVE_HITS = """
const issues = $$(".front-card, .archive-row");
expect(issues.length === 2, `${issues.length} cards and rows, want 2`);
const centre = (el) => {
  const r = el.getBoundingClientRect();
  return [r.left + r.width / 2, r.top + r.height / 2];
};
const problems = [];
for (const issue of issues) {
  issue.scrollIntoView({block: "center", behavior: "instant"});
  const digest = $(".digest-link", issue), raw = $(".actions a:not(.digest-link)", issue);
  const r = issue.getBoundingClientRect();
  const parts = [...issue.children].filter((part) => !part.matches(".actions"));
  const wanted = [
    ...parts.map((part) => [digest, centre(part)]),
    [digest, [r.left + 2, r.top + 2]],
    [digest, [r.right - 2, r.bottom - 2]],
    [raw, centre(raw)],
  ];
  for (const [target, [x, y]] of wanted) {
    if (!lands(target, x, y)) {
      const hit = named(document.elementFromPoint(x, y));
      problems.push(`${named(issue)} (${x}, ${y}) lands on ${hit}, want ${named(target)}`);
    }
  }
}
expect(!problems.length, problems.join("; "));
"""
_CHECKS += [
    Check(
        id=f"archive-{name}-{width}",
        fixture="signed-in",
        path=ARCHIVE,
        width=width,
        script=EXPECT_ARCHIVE_HITS,
        sabotage=sabotage,
    )
    for width in (TAP_WIDTH, 1440)
    for name, sabotage in (
        ("card-opens-digest", restyle(".btn.sm.digest-link { position: relative !important; }")),
        (
            "all-articles-above-card",
            """$$(".actions").forEach((actions) => actions.append($(".digest-link", actions)));""",
        ),
    )
]
# Each water row with a summary draws it on its own line below the title, and a row
# without one draws none; `WIRE_SUMMARIES` says which rows the fixture gave one.
WIRE_SUMMARIES = [bool(water.item.summary) for water in layer_by_lane(content()).water]
EXPECT_WIRE_SUMMARIES = f"""
const rows = $$(".headline-item");
const wanted = {json.dumps(WIRE_SUMMARIES)};
expect(rows.length === wanted.length, `${{rows.length}} Wire rows, want ${{wanted.length}}`);
"""
_CHECKS += [
    Check(
        id=f"wire-summary-below-title-{width}",
        fixture="signed-in",
        path=PAGE,
        width=width,
        act="await signedIn();",
        script=EXPECT_WIRE_SUMMARIES
        + """
            const problems = rows.flatMap((row, n) => {
              const summary = $(".summary", row);
              if (!wanted[n]) return summary ? [`row ${n + 1} draws a summary`] : [];
              if (!visible(summary)) return [`row ${n + 1}: no summary shows`];
              const s = summary.getBoundingClientRect();
              const t = $(".text > a", row).getBoundingClientRect();
              const r = row.getBoundingClientRect();
              return s.top >= t.bottom - 0.5 && Math.abs(s.left - t.left) <= 0.5
                && s.right <= r.right + 0.5
                ? [] : [`row ${n + 1}: summary at ${s.left},${s.top}, title ends ${t.bottom}`];
            });
            expect(!problems.length, problems.join("; "));
        """,
        sabotage=restyle(".headline-item .summary { display: inline !important; }"),
    )
    for width in (TAP_WIDTH, 1440)
]
# The longest Wire summary wraps inside its row on a phone, its URL included, at every
# type size, and the page does not scroll sideways.
WIRE_FITS = Check(
    id=f"wire-fits-{TAP_WIDTH}",
    fixture="signed-in",
    path=PAGE,
    width=TAP_WIDTH,
    act="await signedIn();",
    script=EXPECT_WIRE_SUMMARIES
    + f"""
        const shown = $$(".headline-item .summary").filter(visible).length;
        const kept = wanted.filter(Boolean).length;
        expect(shown === kept, `${{shown}} Wire summaries show, want ${{kept}}`);
        // A vote button's hit area overflows the button by design, so only the text
        // boxes are read for overflow; every box must stay inside the list.
        const box = $(".headlines-compact").getBoundingClientRect();
        const spilled = $$(".headline-item *").filter(visible).filter((el) =>
          el.getBoundingClientRect().right > box.right + 0.5
          || (el.matches(".text, .summary") && el.scrollWidth > el.clientWidth));
        expect(!spilled.length, `spilled: ${{spilled.map((el) => el.className).join(" ")}}`);
        const problem = overflow("{TAP_WIDTH}");
        expect(!problem, problem);
    """,
    sabotage=restyle(".headline-item .summary { overflow-wrap: normal !important; }"),
)
_CHECKS += [
    WIRE_FITS,
    largest_twin(WIRE_FITS, f"wire-fits-largest-{TAP_WIDTH}", "signed-in-largest"),
    smallest_twin(WIRE_FITS, f"wire-fits-smallest-{TAP_WIDTH}", "signed-in-smallest"),
]
# The arrows and the ↗ are narrowest at the smallest type size, so their areas reach
# furthest past them there: the margins that keep neighbours apart are tightest.
SMALLEST_TAP_TWINS = ("votes", "votes-apart", "original", "original-clear")
_CHECKS += [
    smallest_twin(check, check.id.replace("tap-", "tap-smallest-"), "signed-in-smallest")
    for check in _CHECKS
    if check.id in {f"tap-{name}-{TAP_WIDTH}" for name in SMALLEST_TAP_TWINS}
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
