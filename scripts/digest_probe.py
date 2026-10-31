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
from urllib.parse import unquote

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

KINDS = (
    "signed-in",
    "signed-in-largest",
    "signed-in-smallest",
    "vote-fails",
    "pictured",
    "no-layer",
    "one-layer",
)

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


# A story image that loads without the network, at the feed picture's usual 1200 x 630.
STORY_IMAGE = (
    "data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%201200%20630'"
    "%3E%3Crect%20width='1200'%20height='630'%20fill='%23c2523b'/%3E%3C/svg%3E"
)
# One the fixture server answers 404 for, so it fails to load.
BROKEN_STORY_IMAGE = "/story-image-missing.jpg"


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
    # The lead runs several lines and carries a story image that loads; the first
    # feature names one that fails, and the rest name none.
    lead_summary = "The lead story runs long enough to wrap across several lines of its card. " * 4
    features = [
        _item(
            "lead",
            "The lead story of the issue",
            ["Lead Source"],
            score=8.5,
            summary=lead_summary.strip(),
            tags=["Lead", "Long"],
            image_url=STORY_IMAGE,
        )
    ]
    features += [
        _item(
            f"feature-{n}",
            f"Feature story {n}",
            [f"Source {n}"],
            score=8.0 - n / 10,
            image_url=BROKEN_STORY_IMAGE if n == 1 else None,
        )
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


def render_page(issue: DigestContent | None = None) -> str:
    with tempfile.TemporaryDirectory(prefix="digest-probe-") as unused:
        return HtmlDigestWriter(Path(unused)).render(issue or content())


# A story image the fixture serves only after this long, so a lazy image near a
# layer's end arrives after the reader has reached that end.
SLOW_IMAGE_MS = 400
SLOW_IMAGE = "/story-image.svg"


def pictured_content() -> DigestContent:
    """`content` with a picture on every Stories item, served slowly, one near the end broken."""
    issue = content()
    pebbles = [
        item
        for section in (
            *issue.featured_articles,
            *issue.attention_sections,
            *issue.fan_sections,
        )
        for item in section.items
    ]
    for n, item in enumerate(pebbles):
        item.image_url = f"{SLOW_IMAGE}?n={n}"
    # The fan stories close the Stories layer: the next to last fails, the last loads.
    issue.fan_sections[-1].items[-2].image_url = BROKEN_STORY_IMAGE
    return issue


def no_layer_content() -> DigestContent:
    """An issue with no layer: the masthead, and Tracking long enough to scroll."""
    issue = content()
    issue.featured_articles = []
    issue.news_clusters = []
    issue.fan_sections = []
    issue.attention_sections = []
    issue.filtered_headlines = []
    issue.articles_included = 0
    issue.tracked_topics[0].items = [
        _item(f"tracked-{n}", f"Tracked hit {n}", ["Wire"]) for n in range(1, 41)
    ]
    return issue


def one_layer_content() -> DigestContent:
    """An issue whose one layer is Headlines, after Tracking."""
    issue = content()
    issue.featured_articles = []
    issue.news_clusters = []
    issue.fan_sections = []
    issue.attention_sections = []
    return issue


ISSUES = {
    "pictured": pictured_content,
    "no-layer": no_layer_content,
    "one-layer": one_layer_content,
}


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
    `pictured` serves `pictured_content`, every Stories item with a slow picture;
    `no-layer` and `one-layer` serve the issues of those names.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown fixture {kind!r}")
    page = render_page(ISSUES[kind]() if kind in ISSUES else None)
    archive = render_archive()
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

    async def slow_image(_: web.Request) -> web.Response:
        await asyncio.sleep(SLOW_IMAGE_MS / 1000)
        svg = unquote(STORY_IMAGE.split(",", 1)[1])
        return web.Response(text=svg, content_type="image/svg+xml")

    app.router.add_get(PAGE, serve_page)
    app.router.add_get(ARCHIVE, serve_archive)
    app.router.add_get(SLOW_IMAGE, slow_image)
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
// With script one layer shows at a time; the checks of items read every layer at once,
// stacked as a page without script lays them out, so they show the hidden ones first.
const showEveryLayer = () => $$("main > *, .masthead, .footer")
  .forEach((e) => { e.hidden = false; });
const signedIn = () => (showEveryLayer(), waitFor(() => {
  const groups = $$(".vote-group");
  return groups.length && groups.every(visible);
}, "the vote buttons"));
// The layered page as a reader gets it: the sections that are layers, the one shown,
// and whether the window has reached either end of it.
const layerSections = () => $$("main > .section")
  .filter((s) => $(".featured-grid, .sand-grid, .headlines-compact", s));
// The votes the sign-in probe shows lengthen the layer, so its end is read after them.
const layered = () => waitFor(() => $(".layer-dock") && $$(".vote-group").some(visible),
  "the layer dock and the vote buttons");
const isHidden = (el) => !el.getClientRects().length;
const shownLayers = () => layerSections().filter((s) => !isHidden(s))
  .map((s) => $(".section-tag", s).textContent.trim());
const atLayerBottom = () => scrollY >= document.documentElement.scrollHeight - innerHeight - 1;
const toLayerBottom = () => window.scrollTo({top: document.documentElement.scrollHeight,
  behavior: "instant"});
const frames = () => new Promise((done) =>
  requestAnimationFrame(() => requestAnimationFrame(done)));
// Focus never stays on what a switch hid; a hidden element can stay activeElement.
const expectFocusShown = () => {
  const a = document.activeElement;
  expect(!a || a === document.body || !isHidden(a), `focus stays on hidden ${named(a)}`);
};
const expectFocusOn = (el) => expect(document.activeElement === el,
  `focus on ${named(document.activeElement)}, want ${named(el)}`);
const expectShown = (name) => {
  const shown = shownLayers();
  expect(same(shown, [name]), `layers shown: ${shown.join(", ") || "none"}, want ${name}`);
  expectFocusShown();
};
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


# What a progress strip of one segment adds: with no layer the page is long enough that
# the fill grows as it scrolls; the only layer has no neighbour, in the dock or an edge.
PROGRESS_ALONE = {
    "no-layer": "expect(before < after - 1, `at the top the fill is already ${before}px`);",
    "one-layer": """
        expect(isHidden($(".layer-back")) && isHidden($(".layer-skip")),
          "the dock names a neighbour of the only layer");
        expect(!document.body.matches(".has-prev, .has-next"), "an edge shows");
    """,
}

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
                showEveryLayer();
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
            act="await layered();",
            script="""
                const segments = $$(".read-progress span");
                const layers = layerSections();
                expect(segments.length === layers.length,
                  `${segments.length} segments for ${layers.length} layers`);
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
        act="await layered();",
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
        act="await layered();",
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
    # An issue with no layer still draws the strip, as one segment that is the whole page;
    # one with a single layer draws that layer's one segment, with no neighbour in the dock.
    # Each fills to its end as the window reaches the page's end.
    *(
        Check(
            id=f"progress-{kind}-{width}",
            fixture=kind,
            path=PAGE,
            width=width,
            act=(
                'await waitFor(() => $(".read-progress"), "the reading progress bar");'
                if kind == "no-layer"
                else "await layered();"
            ),
            script=f"""
                const segments = $$(".read-progress span");
                expect(segments.length === 1, `${{segments.length}} segments`);
                const strip = $(".read-progress").getBoundingClientRect();
                const segment = segments[0].getBoundingClientRect();
                expect(segment.width > strip.width - 40, `the segment spans ${{segment.width}}px`);
                const filled = () => $(".read-progress i").getBoundingClientRect().width;
                window.scrollTo({{top: 0, behavior: "instant"}});
                await frames();
                const before = filled();
                toLayerBottom();
                await frames();
                const after = filled();
                expect(Math.abs(after - segment.width) <= 1,
                  `at the end the fill is ${{after}}px of ${{segment.width}}px`);
                {PROGRESS_ALONE[kind]}
            """,
            sabotage=restyle(".read-progress i { width: 0 !important; }"),
        )
        for kind, width in (("no-layer", 360), ("no-layer", 1440), ("one-layer", 1440))
    ),
    # A refused vote's notice sits right below the meta row it reports on, in the text's
    # column of an item with an image as of one without.
    *(
        Check(
            id=f"pebble-vote-error-under-meta-{n}-1440",
            fixture="signed-in",
            path=PAGE,
            width=1440,
            act=f"""
                await signedIn();
                showFailure($(".vote-group", $$(".featured-item")[{n}]), VOTE_FAILED.notConfigured);
            """,
            script=f"""
                const item = $$(".featured-item")[{n}];
                const notice = $(".vote-error", item);
                expect(notice, "no failure notice in the item");
                const meta = $(".meta", item).getBoundingClientRect();
                const box = notice.getBoundingClientRect();
                const gap = box.top - meta.bottom;
                expect(gap >= -0.5 && gap <= 1, `the notice sits ${{gap}}px below the meta row`);
                expect(Math.abs(box.left - meta.left) <= 0.5,
                  `the notice starts at ${{box.left}}, the meta row at ${{meta.left}}`);
            """,
            sabotage=restyle(".featured-item .vote-error { margin-top: var(--s-6) !important; }"),
        )
        for n in (0, 2)
    ),
    # A story image is the spec's box: 280px wide on a desktop, at the feed picture's
    # 1200 / 630, greyscale at .55 until its item is hovered or focused, and on the left
    # of the text with its top on the title's.
    Check(
        id="story-image-box-1440",
        fixture="signed-in",
        path=PAGE,
        width=1440,
        act="await signedIn();",
        script="""
            const image = $(".featured-item .story-image");
            expect(image, "no story image");
            const box = image.getBoundingClientRect();
            const title = $("h3", image.parentElement).getBoundingClientRect();
            const style = getComputedStyle(image);
            expect(Math.abs(box.width - 280) <= 0.5, `the image is ${box.width}px wide`);
            expect(Math.abs(box.width / box.height - 1200 / 630) < 0.02,
              `the image is ${box.width} x ${box.height}`);
            expect(style.filter === "grayscale(1)" && style.opacity === "0.55",
              `at rest the image has filter ${style.filter}, opacity ${style.opacity}`);
            expect(title.left >= box.right && Math.abs(title.top - box.top) <= 1,
              `the title at ${title.left},${title.top}, the image ends at ${box.right},${box.top}`);
        """,
        # Without its transition, so the sabotaged colour is read at once, not mid-fade.
        sabotage=restyle(
            ".featured-item > .story-image"
            " { filter: none !important; transition: none !important; }"
        ),
    ),
    # On a phone the image is 112px and sits beside the title alone; the summary below
    # takes the item's full width.
    Check(
        id="story-image-box-360",
        fixture="signed-in",
        path=PAGE,
        width=360,
        act="await signedIn();",
        script="""
            const image = $(".featured-item .story-image");
            expect(image, "no story image");
            const box = image.getBoundingClientRect();
            const item = image.parentElement;
            const title = $("h3", item).getBoundingClientRect();
            const summary = $(".summary", item).getBoundingClientRect();
            expect(Math.abs(box.width - 112) <= 0.5, `the image is ${box.width}px wide`);
            expect(title.left >= box.right, `title at ${title.left}, image ends ${box.right}`);
            expect(Math.abs(summary.left - box.left) <= 0.5 && summary.top >= box.bottom,
              `the summary at ${summary.left},${summary.top}, the image ${box.left}-${box.bottom}`);
            const problem = overflow("360");
            expect(!problem, problem);
        """,
        sabotage=restyle(
            ".featured-item:has(> .story-image)"
            " { grid-template-columns: 280px minmax(0, 1fr) !important; }"
        ),
    ),
    # Focusing within the item gives its image back its colour.
    Check(
        id="story-image-colour-on-focus-1440",
        fixture="signed-in",
        path=PAGE,
        width=1440,
        focused=True,
        act="""
            await signedIn();
            $(".featured-item:has(> .story-image) h3 a").focus();
        """,
        script="""
            const image = $(".featured-item .story-image");
            await waitFor(() => {
              const style = getComputedStyle(image);
              return style.filter === "none" && style.opacity === "1";
            }, "the focused item's image in colour").catch(() => {});
            const style = getComputedStyle(image);
            expect(style.filter === "none" && style.opacity === "1",
              `focused, the image has filter ${style.filter}, opacity ${style.opacity}`);
        """,
        sabotage=restyle(
            ".featured-item:is(:hover, :focus-within) > .story-image"
            " { filter: grayscale(1) !important; }"
        ),
    ),
    # An image that fails to load removes its box, and an item with no image never had one:
    # only the lead keeps its image, and the first feature's title opens its item.
    Check(
        id="story-image-broken-removed-1440",
        fixture="signed-in",
        path=PAGE,
        width=1440,
        act="""
            await signedIn();
            $$(".featured-item")[1].scrollIntoView({block: "center", behavior: "instant"});
        """,
        script="""
            const broken = $$(".featured-item")[1];
            await waitFor(() => !$(".story-image", broken), "the broken image's removal")
              .catch(() => {});
            expect(!$(".story-image", broken), "the broken image keeps its box");
            expect(broken.firstElementChild.tagName === "H3",
              `the item opens with ${broken.firstElementChild.tagName}`);
            const pictured = $$(".featured-item").filter((item) => $(".story-image", item));
            expect(pictured.length === 1, `${pictured.length} items show an image`);
        """,
        sabotage_preload=('addEventListener("error", (e) => e.stopImmediatePropagation(), true);'),
    ),
    # The reading progress bar spans the site bar's inner width, the column its brand and
    # links sit in, not the whole window. A bar stretched edge to edge is the sabotage.
    *(
        Check(
            id=f"progress-in-column-{width}",
            fixture="signed-in",
            path=PAGE,
            width=width,
            act="await layered();",
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
    # The sand blocks fill the column as a grid: two side by side on a desktop, one above
    # the other on a phone, with no sideways scroll there.
    Check(
        id="sand-side-by-side-1440",
        fixture="signed-in",
        path=PAGE,
        width=1440,
        act="await signedIn();",
        script="""
            const [a, b] = $$(".news-cluster").map((block) => block.getBoundingClientRect());
            expect(a && b, "fewer than two sand blocks");
            expect(Math.abs(a.top - b.top) <= 1 && b.left >= a.right,
              `first at ${a.left},${a.top}, second at ${b.left},${b.top}`);
        """,
        sabotage=restyle(".sand-grid { display: block !important; }"),
    ),
    Check(
        id="sand-stacks-360",
        fixture="signed-in",
        path=PAGE,
        width=360,
        act="await signedIn();",
        script="""
            const [a, b] = $$(".news-cluster").map((block) => block.getBoundingClientRect());
            expect(a && b, "fewer than two sand blocks");
            expect(b.top >= a.bottom, `second starts at ${b.top}, first ends at ${a.bottom}`);
            const problem = overflow("360");
            expect(!problem, problem);
        """,
        sabotage=restyle(
            ".sand-grid { display: grid !important;"
            " grid-template-columns: repeat(2, 300px) !important; }"
        ),
    ),
    # A water row with no summary and no tags is one line, shorter than the 44px tap
    # area its 24px vote buttons keep, so a short row must still be 44px tall or a tap
    # between two rows votes on the neighbour.
    Check(
        id="water-row-holds-tap-area-360",
        fixture="signed-in",
        path=PAGE,
        width=360,
        act=(
            "await signedIn();"
            " $$('.headline-item .summary, .headline-item .tags').forEach((e) => e.remove());"
        ),
        script="""
            const rows = $$(".headline-item").map((row) => row.getBoundingClientRect().height);
            expect(rows.length >= 2, "fewer than two water rows");
            const short = rows.filter((h) => h < 44);
            expect(!short.length, `rows of ${short.join(", ")}px are shorter than the tap area`);
        """,
        sabotage=restyle(".headline-item { min-height: 0 !important; }"),
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


def _layer_counts() -> list[tuple[str, int]]:
    """Each layer the fixture has, in page order, with its item count."""
    layers = layer_by_lane(content())
    return [
        ("Stories", len(layers.pebbles)),
        ("Topics", len(layers.sand)),
        ("Headlines", len(layers.water)),
    ]


LAYERS = _layer_counts()
NEXT_LAYER = f"{LAYERS[1][1]} {LAYERS[1][0]}"


def _ignored(event: str, when: str = "true") -> str:
    """A sabotage: the page's layer script never hears `event` while `when` holds."""
    return f"""addEventListener("{event}", (e) => {{
        if ({when}) e.stopImmediatePropagation();
    }}, {{capture: true}});"""


# A sabotage: a page that switches layers, as a layer that cannot hold a reader would.
SWITCHED = """$(".layer-skip").click();"""
# The window's bottom, `above` pixels short of the end of the first layer.
NEAR_BOTTOM = """
await layered();
window.scrollTo({top: document.documentElement.scrollHeight - innerHeight - {above},
  behavior: "instant"});
await frames();
"""
EXPECT_TOPICS = """
await waitFor(() => same(shownLayers(), ["Topics"]), "the Topics layer shown");
await frames();
expectShown("Topics");
"""
# With script one layer shows at a time (ui-language section 4, Layers; section 6, digest).
_CHECKS += [
    # The first layer shows with the masthead and Tracking, and nothing else; the dock
    # names it and the next, and the reading progress has one segment per layer, as
    # wide as its share of the items.
    *(
        Check(
            id=f"layers-one-at-a-time-{width}",
            fixture="signed-in",
            path=PAGE,
            width=width,
            act="await layered(); await frames();",
            script=f"""
                expectShown("Stories");
                expect(!isHidden($(".masthead")), "the masthead is hidden");
                expect(isHidden($(".footer")), "the footer shows on the first layer");
                const where = $(".layer-where").textContent;
                const dockText = {json.dumps(f"{LAYERS[0][0]} · {LAYERS[0][1]}")};
                expect(where === dockText, `dock: ${{where}}`);
                expect(isHidden($(".layer-back")), "back shows on the first layer");
                const skip = $(".layer-skip").getAttribute("aria-label");
                expect(skip === {json.dumps(f"Skip to {NEXT_LAYER}")}, `skip: ${{skip}}`);
                const grow = $$(".read-progress span").map((s) => s.style.flexGrow);
                const shares = {json.dumps([str(n) for _, n in LAYERS])};
                expect(same(grow, shares), `segments: ${{grow}}`);
            """,
            sabotage=restyle("main > .section[hidden] { display: block !important; }"),
        )
        for width in (360, 1440)
    ),
    # At the end of a layer a fresh pull of 160px switches to the next one, which lands
    # on its heading with the layer left hidden.
    *(
        Check(
            id=f"layer-pull-{width}",
            fixture="signed-in",
            path=PAGE,
            width=width,
            act=NEAR_BOTTOM.replace("{above}", "0"),
            gestures=({"wait": 300}, {"wheel": 100}, {"wait": 50}, {"wheel": 100}),
            script=EXPECT_TOPICS
            + """
                expect(scrollY === 0, `landed at ${scrollY}, not on the heading`);
                expectFocusOn($(".section-tag", layerSections()[1]));
            """,
            sabotage=_ignored("wheel"),
        )
        for width in (360, 1440)
    ),
    # The wheel gesture that reaches the edge never counts, however far it goes on.
    Check(
        id="layer-edge-gesture-spent-1440",
        fixture="signed-in",
        path=PAGE,
        act=NEAR_BOTTOM.replace("{above}", "200"),
        gestures=(
            {"wait": 300},
            *({"wheel": 100}, {"wait": 40}) * 8,
            {"wait": 100},
        ),
        script="""
            expect(atLayerBottom(), `stopped at ${scrollY}, short of the layer's end`);
            expectShown("Stories");
        """,
        sabotage=SWITCHED,
    ),
    # One key press at an edge reached by scrolling only arms it, and says so.
    Check(
        id="layer-key-first-press-arms-1440",
        fixture="signed-in",
        path=PAGE,
        act=NEAR_BOTTOM.replace("{above}", "0"),
        gestures=({"wait": 200}, {"key": "ArrowDown"}),
        script=f"""
            await frames();
            expectShown("Stories");
            const hint = $(".layer-edge.down .layer-pull");
            expect(!isHidden(hint), "no hint at the armed edge");
            const text = $(".label", hint).textContent;
            expect(text === {json.dumps(f"Press ↓ again for {NEXT_LAYER}")}, `hint: ${{text}}`);
        """,
        sabotage="""addEventListener("keydown", () => $(".layer-skip").click(),
            {capture: true});""",
    ),
    # Holding N switches once: its auto-repeat must not run through every layer.
    Check(
        id="layer-n-held-switches-once-1440",
        fixture="signed-in",
        path=PAGE,
        act="await layered();",
        gestures=({"hold": "n", "repeats": 20}, {"wait": 100}),
        script=EXPECT_TOPICS,
        sabotage="""addEventListener("keydown", (e) => {
            if (e.repeat) $(".layer-skip").click();
        }, {capture: true});""",
    ),
    # A held key scrolls to the edge and never past it; the next fresh press switches,
    # with the heading's focus ring showing because the switch came from the keyboard.
    Check(
        id="layer-key-held-never-switches-1440",
        fixture="signed-in",
        path=PAGE,
        act=NEAR_BOTTOM.replace("{above}", "120"),
        gestures=({"wait": 200}, {"hold": "ArrowDown", "repeats": 20}, {"wait": 100}),
        script="""
            expect(atLayerBottom(), `the held key stopped at ${scrollY}, short of the end`);
            expectShown("Stories");
        """,
        sabotage="""addEventListener("keydown", (e) => {
            if (e.repeat) $(".layer-skip").click();
        }, {capture: true});""",
    ),
    Check(
        id="layer-key-fresh-press-switches-1440",
        fixture="signed-in",
        path=PAGE,
        act=NEAR_BOTTOM.replace("{above}", "120"),
        gestures=(
            {"wait": 200},
            {"hold": "ArrowDown", "repeats": 20},
            {"wait": 100},
            {"key": "ArrowDown"},
        ),
        script=EXPECT_TOPICS
        + """
            const heading = $(".section-tag", layerSections()[1]);
            expectFocusOn(heading);
            expect(!heading.classList.contains("quiet"), "a keyboard switch hides the focus ring");
        """,
        sabotage=_ignored("keydown", "!e.repeat"),
    ),
    # N switches to the next layer from anywhere in it.
    Check(
        id="layer-n-key-1440",
        fixture="signed-in",
        path=PAGE,
        act="await layered();",
        gestures=({"key": "n"},),
        script=EXPECT_TOPICS,
        sabotage=_ignored("keydown"),
    ),
    # Skip goes to the next layer and back returns to the end of the one left, hiding
    # the layer it leaves each time.
    *(
        Check(
            id=f"layer-skip-and-back-{width}",
            fixture="signed-in",
            path=PAGE,
            width=width,
            act="await layered();",
            gestures=(
                {"press": ".layer-skip", "pointer": "mouse"},
                {"release": True},
                {"wait": 100},
                {"press": ".layer-back", "pointer": "mouse"},
                {"release": True},
            ),
            script="""
                await waitFor(() => same(shownLayers(), ["Stories"]), "the Stories layer shown");
                await frames();
                expectShown("Stories");
                expect(isHidden(layerSections()[1]), "the Topics layer still shows");
                expect(scrollY > 0 && atLayerBottom(), `back landed at ${scrollY}, not the end`);
            """,
            sabotage=restyle(".layer-back { pointer-events: none !important; }"),
        )
        for width in (360, 1440)
    ),
    # Going back lands focus on the previous layer's last item, at its end: by a key, a
    # pull or the dock's back. The ring shows only after the keyboard switch. The sabotage
    # is a switch that leaves focus where it was, on the heading of the layer it hid.
    *(
        Check(
            id=f"layer-back-focus-{by}-{width}",
            fixture="signed-in",
            path=PAGE,
            width=width,
            act="""
                await layered();
                $(".layer-skip").click();
                await frames();
                $(".section-tag", layerSections()[1]).focus();
            """,
            gestures=gestures,
            script=f"""
                await waitFor(() => same(shownLayers(), ["Stories"]), "the Stories layer shown");
                await frames();
                expectShown("Stories");
                const items = $$(".featured-item");
                const last = items[items.length - 1];
                expectFocusOn(last);
                expect(scrollY > 0 && atLayerBottom(), `back landed at ${{scrollY}}, not the end`);
                const quiet = last.classList.contains("quiet");
                expect(quiet === {json.dumps(by != "key")},
                  `after a {by} switch the ring ${{quiet ? "hides" : "shows"}}`);
            """,
            sabotage="""
                const focus = HTMLElement.prototype.focus;
                HTMLElement.prototype.focus = function (options) {
                  if (!this.matches(".featured-item")) focus.call(this, options);
                };
            """,
        )
        for by, width, gestures in (
            ("key", 1440, ({"wait": 200}, {"key": "ArrowUp"}, {"wait": 50}, {"key": "ArrowUp"})),
            ("pull", 360, ({"wait": 300}, {"wheel": -100}, {"wait": 50}, {"wheel": -100})),
            ("pull", 1440, ({"wait": 300}, {"wheel": -100}, {"wait": 50}, {"wheel": -100})),
            ("dock", 1440, ({"press": ".layer-back", "pointer": "mouse"}, {"release": True})),
        )
    ),
    # Back lands focus on Stories' last item; its image turns to colour only when the
    # switch came from the keyboard, as the ring does. The sabotage colours any focused item.
    *(
        Check(
            id=f"layer-back-image-{by}-1440",
            fixture="pictured",
            path=PAGE,
            act="""
                await layered();
                $(".layer-skip").click();
                await frames();
                $(".section-tag", layerSections()[1]).focus();
            """,
            gestures=gestures,
            script=f"""
                await waitFor(() => same(shownLayers(), ["Stories"]), "the Stories layer shown");
                await frames();
                const items = $$(".featured-item");
                const last = items[items.length - 1];
                expectFocusOn(last);
                const image = $(".story-image", last);
                expect(image, "the last Stories item has its image");
                await new Promise((done) => setTimeout(done, 300));
                const style = getComputedStyle(image);
                const colour = style.filter === "none" && style.opacity === "1";
                expect(colour === {json.dumps(by == "key")},
                  `after a {by} switch the image has filter ${{style.filter}},`
                  + ` opacity ${{style.opacity}}`);
            """,
            sabotage=restyle(
                ".featured-item:focus > .story-image"
                + (
                    " { filter: grayscale(1) !important; opacity: .55 !important; }"
                    if by == "key"
                    else " { filter: none !important; opacity: 1 !important; }"
                )
            ),
        )
        for by, gestures in (
            ("key", ({"wait": 200}, {"key": "ArrowUp"}, {"wait": 50}, {"key": "ArrowUp"})),
            ("dock", ({"press": ".layer-back", "pointer": "mouse"}, {"release": True})),
        )
    ),
    # A link to an item in a hidden layer switches to it, on load and on a hash change,
    # and brings the item under the bars.
    *(
        Check(
            id=f"layer-hash-{when}-{width}",
            fixture="signed-in",
            path=PAGE + "#headline-2" if when == "on-load" else PAGE,
            width=width,
            act="await layered();" + ("" if when == "on-load" else ' location.hash = "#topic-2";'),
            script=f"""
                const name = {json.dumps("Headlines" if when == "on-load" else "Topics")};
                await waitFor(() => same(shownLayers(), [name]), `the ${{name}} layer shown`);
                await frames();
                expectShown(name);
                const item = $({json.dumps("#headline-2" if when == "on-load" else "#topic-2")});
                // Right under the bars, or as near as the end of a short layer lets it come.
                const {{top, bottom}} = item.getBoundingClientRect();
                const bar = $(".site-bar").getBoundingClientRect().bottom;
                const dock = $(".layer-dock").getBoundingClientRect().top;
                expect((Math.abs(top - bar) <= 1 || atLayerBottom()) && top >= bar - 1
                  && bottom <= dock, `the item spans ${{top}}-${{bottom}}, under ${{bar}}`);
            """,
            sabotage_preload=(
                "document.getElementById = () => null;"
                if when == "on-load"
                else _ignored("hashchange")
            ),
        )
        for when, width in (("on-load", 1440), ("on-load", 360), ("change", 360))
    ),
    # On a phone the dock covers no vote at the end of any layer: the page keeps the
    # dock's height free below its last item, and the edge's fade takes no taps.
    Check(
        id="layer-dock-clear-of-votes-360",
        fixture="signed-in",
        path=PAGE,
        width=360,
        act="""
            await layered();
            await waitFor(() => $$(".vote-group").some(visible), "the vote buttons");
        """,
        script="""
            const problems = [];
            for (let n = 0; n < layerSections().length; n++) {
              if (n) $(".layer-skip").click();
              toLayerBottom();
              await frames();
              const dock = $(".layer-dock").getBoundingClientRect();
              const bar = $(".site-bar").getBoundingClientRect().bottom;
              const seen = $$(".promote-btn").filter((b) => !isHidden(b)).filter((b) => {
                const r = b.getBoundingClientRect();
                return r.top >= bar && r.top < innerHeight;
              });
              if (!seen.length) problems.push(`layer ${n + 1}: no vote in view at its end`);
              for (const b of seen) {
                const r = b.getBoundingClientRect();
                if (r.bottom > dock.top || !lands(b, r.left + r.width / 2, r.top + r.height / 2))
                  problems.push(`layer ${n + 1}: ${named(b.closest(".vote-group").parentElement)}`
                    + ` ends at ${r.bottom}, the dock starts at ${dock.top}`);
              }
            }
            expect(!problems.length, problems.join("; "));
        """,
        sabotage=restyle(".layer-dock { height: 50vh !important; }"),
    ),
    # With a picture on every Stories item, the ones near the layer's end load only once
    # the reader is there, slowly, and one fails and loses its box. The end must stay the
    # end: the pull still switches, and back lands on the end with the last item above the
    # dock. The sabotage is an image box that reserves no height, in a browser without
    # scroll anchoring, as WebKit is: Chromium's anchoring would hide the growth.
    *(
        Check(
            id=f"layer-end-holds-with-images-{width}",
            fixture="pictured",
            path=PAGE,
            width=width,
            act="""
                ctx.loads = 0; ctx.errors = 0; ctx.seen = [];
                new MutationObserver(() => ctx.seen.push(shownLayers().join()))
                  .observe(document.body, {subtree: true, attributeFilter: ["hidden"]});
                document.addEventListener("load", (e) => {
                  if (e.target.matches?.(".story-image")) ctx.loads++; }, true);
                addEventListener("error", (e) => {
                  if (e.target.matches?.(".story-image")) ctx.errors++; }, true);
            """
            + NEAR_BOTTOM.replace("{above}", "0"),
            gestures=(
                {"wait": 1000},
                {"wheel": 100},
                {"wait": 50},
                {"wheel": 100},
                {"wait": 100},
                {"press": ".layer-back", "pointer": "mouse"},
                {"release": True},
                {"wait": 1000},
            ),
            script="""
                expect(ctx.loads > 0 && ctx.errors > 0,
                  `${ctx.loads} pictures loaded and ${ctx.errors} failed near the end`);
                expect(ctx.seen.includes("Topics"), `the pull never showed Topics: ${ctx.seen}`);
                await frames();
                expectShown("Stories");
                expect(scrollY > 0 && atLayerBottom(), `back landed at ${scrollY}, not the end`);
                const items = $$(".featured-item");
                const last = items[items.length - 1].getBoundingClientRect();
                const dock = $(".layer-dock").getBoundingClientRect().top;
                expect(last.bottom <= dock, `the last item ends at ${last.bottom}, dock ${dock}`);
                const pictured = items.map((i) => $(".story-image", i)).filter(Boolean)
                  .filter((image) => !isHidden(image));
                expect(pictured.every((image) => image.getBoundingClientRect().height > 0),
                  "a picture in view has no height");
            """,
            sabotage=restyle(
                ".featured-item > .story-image { aspect-ratio: auto !important;"
                " height: auto !important; } html { overflow-anchor: none !important; }"
            ),
        )
        for width in (360, 1440)
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
