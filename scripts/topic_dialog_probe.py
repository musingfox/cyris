#!/usr/bin/env python3
"""Drive the digest's topic dialog in Chromium, Firefox and WebKit and check what a reader gets.

pytest holds the dialog's markup, but not what its script does in a browser: whether a
sand card opens it and a vote on the card does not, whether every way out zooms back and
returns focus to the card's heading button, whether the zoom runs 650ms from the clicked
card, whether the card's and the dialog's votes agree, whether the layer switcher stands
still while it is open, and whether a touch pull closes it only when it should
(`docs/design/ui-language.md` §4 *topic dialog*). This runs those checks in each engine
against the page `render` writes for `digest_probe`'s issue, with a topic first whose
dialog is long enough to scroll on a phone. The page and a stand-in for the app Worker's
`/api/vote` are answered by Playwright's request routing, so no port is opened.

Each check names its effect (`dialog.open`, `document.activeElement`, the visible layer,
`getAnimations()` timing), never only an exit code, and prints one PASS or FAIL line per
browser. Every check carries a sabotage that breaks the thing it reads: `--self-test` runs
each check on the sabotaged page too, where it must fail, so a check that cannot fail is
reported rather than trusted.

Touch is driven differently per engine, and each touch line names its driver: Chromium
sends real touches through CDP `Input.dispatchTouchEvent`, which also scroll; Firefox
dispatches a synthetic `TouchEvent` from the page; WebKit in Playwright has no `Touch`
constructor, so it dispatches a plain `Event` carrying a `touches` list. Both synthetic
drivers test the dialog's pull logic, not the engine's touch pipeline, and the probe sets
the dialog's scroll position itself where a finger would have moved it.

Playwright is not a project dependency; run this with it added:

    uv run --with playwright python scripts/topic_dialog_probe.py [--browser NAME] [--self-test]

This script must never be collected by pytest: it needs Playwright's browsers, which makes
it a reviewer-run gate rather than a test. Importing it is safe, and
`tests/test_topic_dialog_probe.py` does so to check its fixture and its sabotages.
"""

import argparse
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import digest_probe

from cyris.domain.models import DigestContent, DigestItem, DigestSection

ORIGIN = "https://digest.probe.test"
PAGE_URL = ORIGIN + digest_probe.PAGE
BROWSERS = ("chromium", "firefox", "webkit")

# The spec's numbers (ui-language.md §2, literal exceptions; §4 topic dialog).
ZOOM_MS = 650
ZOOM_EASING = "cubic-bezier(.2,.8,.2,1)"
PULL_PX = 160

# The long topic opens the Topics layer, so its card is #topic-1 and its dialog scrolls.
CARD = "#topic-1"
LONG_MEMBERS = 14
DESKTOP = {"viewport": {"width": 1440, "height": 900}}
PHONE = {"viewport": {"width": 390, "height": 844}, "has_touch": True}


def _excerpt(n: int) -> str:
    """A stored excerpt as the store keeps one: the article's own opening, at most 80 chars."""
    return f"Report {n} opens with its own first line, cut where the store cuts it."


def content() -> DigestContent:
    """`digest_probe`'s issue with a long topic first, each member with its own excerpt."""
    issue = digest_probe.content()
    members = [
        DigestItem(
            title=f"Report {n} of the long topic",
            summary=_excerpt(n),
            sources=[f"Wire {n}"],
            urls=[digest_probe.url_of(f"long-{n}")],
            passed_through=True,
        )
        for n in range(1, LONG_MEMBERS + 1)
    ]
    vote = DigestItem(
        title="A long topic",
        summary="",
        sources=[m.sources[0] for m in members],
        urls=[m.urls[0] for m in members],
    )
    long_topic = DigestSection(
        heading="A long topic",
        items=[vote],
        members=members,
        story_id=f"{digest_probe.DATE}-{digest_probe.PERIOD}-long",
    )
    issue.news_clusters.insert(0, long_topic)
    return issue


def render_page(sabotage: "Sabotage | None" = None) -> str:
    page = digest_probe.render_page(content())
    if sabotage:
        page = sabotage.apply(page)
    return page


@dataclass(frozen=True)
class Sabotage:
    """Text in the rendered page and what replaces it, to break what one check reads."""

    old: str
    new: str

    def apply(self, page: str) -> str:
        if page.count(self.old) != 1:
            raise ValueError(f"sabotage text is not on the page exactly once: {self.old!r}")
        return page.replace(self.old, self.new)


# ---- the page side ------------------------------------------------------------------

# Added to every page before its own scripts: a card's dialog is the one its button names.
HELPERS = """
window.__dialogOf = (card) => document.getElementById(
    document.querySelector(card + ' .topic-open').getAttribute('commandfor'));
window.__dialogAnims = (d) =>
    document.getAnimations().filter((x) => x.effect && x.effect.target === d);
"""

STATE = """(card) => {
    const opener = document.querySelector(card + ' .topic-open'), d = __dialogOf(card);
    const a = document.activeElement;
    const layered = [...document.querySelectorAll('main > .section')]
        .filter((s) => s.querySelector('.featured-grid, .sand-grid, .headlines-compact'));
    return {
        open: d.open,
        focusIsOpener: a === opener,
        focusIsClose: a === d.querySelector('.topic-dialog-close'),
        active: a ? (a.className || a.tagName) : null,
        layer: layered.findIndex((s) => !s.hidden),
        anims: __dialogAnims(d).length,
        running: __dialogAnims(d).filter((x) => x.playState === 'running').length,
        scrollY: Math.round(scrollY),
        dialogTop: Math.round(d.scrollTop),
    };
}"""

TIMINGS = """(card) => __dialogAnims(__dialogOf(card)).map((x) => {
    const t = x.effect.getTiming();
    return {duration: t.duration, easing: t.easing};
})"""

# The backdrop's computed opacity with the dialog's running animations held at `ms` (as
# they stand when null).
BACKDROP_AT = """([card, ms]) => {
    const d = __dialogOf(card);
    const anims = __dialogAnims(d).filter((x) => x.playState !== 'finished');
    if (ms !== null) anims.forEach((x) => { x.pause(); x.currentTime = ms; });
    const opacity = Number(getComputedStyle(d, '::backdrop').opacity);
    if (ms !== null) anims.forEach((x) => x.play());
    return opacity;
}"""

# Pauses the dialog's running animations at `ms` (none when null), returns its box and the
# card's, and lets them run on.
FRAME = """([card, ms]) => {
    const c = document.querySelector(card), d = __dialogOf(card);
    const anims = __dialogAnims(d).filter((x) => x.playState !== 'finished');
    if (ms !== null) anims.forEach((x) => { x.pause(); x.currentTime = ms; });
    const box = (r) => [r.left, r.top, r.width, r.height].map((v) => Math.round(v * 10) / 10);
    const at = box(d.getBoundingClientRect()), from = box(c.getBoundingClientRect());
    if (ms !== null) anims.forEach((x) => x.play());
    return {at, card: from, n: anims.length};
}"""

# Records, every frame, whether focus sat on <body>; read back after a case.
FOCUS_LOG = (
    "window.__bodySeen = false; (function tick() {"
    " if (document.activeElement === document.body) window.__bodySeen = true;"
    " window.__tick = requestAnimationFrame(tick); })();"
)
FOCUS_LOG_READ = "cancelAnimationFrame(window.__tick), window.__bodySeen"

# Each group's topic, pressed and disabled state, and which group's button holds focus.
VOTES = """(card) => {
    const c = document.querySelector(card), d = __dialogOf(card);
    const buttons = (g) => [...g.querySelectorAll('.promote-btn')];
    const read = (g) => ({
        story: g.dataset.storyId, urls: g.dataset.urls,
        pressed: buttons(g).map((b) => b.getAttribute('aria-pressed')),
        disabled: buttons(g).map((b) => b.disabled),
    });
    const a = document.activeElement;
    const where = a.closest('dialog') ? 'dialog' : a.closest('.news-cluster') ? 'card' : a.tagName;
    return {
        card: read(c.querySelector('.vote-group')),
        dialog: read(d.querySelector('.vote-group')),
        focus: `${where} ${a.dataset.vote}`,
    };
}"""

MEMBERS = """(card) => {
    const d = __dialogOf(card), seen = (e) => !!e && e.checkVisibility();
    const line = d.querySelector('.topic-dialog-line');
    return {
        line: seen(line) ? line.textContent : null,
        rows: [...d.querySelectorAll('.topic-member')].map((li) => {
            const a = li.querySelector('a'), s = li.querySelector('.label');
            const x = li.querySelector('.topic-member-excerpt');
            return [seen(a) && a.textContent, seen(a) && a.getAttribute('href'),
                seen(s) && s.textContent, seen(x) && x.textContent];
        }),
    };
}"""

# Synthetic touch where there is no CDP: a TouchEvent where the engine can make one,
# else an Event carrying a touches list. It scrolls nothing, so the probe scrolls by hand.
SYNTH_TOUCH = """([type, x, y]) => {
    const target = document.elementFromPoint(x, y) || document.body;
    const touches = (t) => (type === 'touchend' ? [] : [t]);
    let ev, how;
    try {
        const t = new Touch({identifier: 1, target, clientX: x, clientY: y});
        const init = {touches: touches(t), changedTouches: [t], bubbles: true, cancelable: true};
        ev = new TouchEvent(type, init);
        how = 'synthetic TouchEvent';
    } catch {
        ev = new Event(type, {bubbles: true, cancelable: true});
        Object.defineProperty(ev, 'touches', {value: touches({clientX: x, clientY: y})});
        how = 'synthetic Event with a touches list';
    }
    target.dispatchEvent(ev);
    return how;
}"""

TO_BOTTOM = "(card) => { const d = __dialogOf(card); d.scrollTop = d.scrollHeight; }"
TO_TOP = "(card) => { __dialogOf(card).scrollTop = 0; }"
HINT = """(card) => {
    const h = __dialogOf(card).querySelector('.topic-dialog-pull-inner');
    return [h.hidden, h.querySelector('b').style.width, h.textContent];
}"""
SIZE = """(card) => {
    const r = __dialogOf(card).getBoundingClientRect();
    return [Math.round(r.width), Math.round(r.height)];
}"""
TRANSFORM = "(card) => getComputedStyle(__dialogOf(card)).transform"
CLOSED_AT_ONCE = """(card) => {
    const d = __dialogOf(card);
    d.querySelector('.topic-dialog-close').click();
    return !d.open;
}"""


# ---- the site ---------------------------------------------------------------------


class Site:
    """The page and `/api/vote`, answered by routing; a vote can be held to see it in flight."""

    def __init__(self, page: str) -> None:
        self.page = page
        self.posts: list[dict] = []
        self.hold = False
        self.held: list[Any] = []

    def handle(self, route: Any, request: Any) -> None:
        url = request.url.split("#", 1)[0]
        if url == PAGE_URL:
            return route.fulfill(status=200, content_type="text/html", body=self.page)
        if url == f"{ORIGIN}/api/vote" and request.method == "GET":
            return route.fulfill(status=200, json={"authorized": True})
        if url == f"{ORIGIN}/api/vote" and request.method == "POST":
            self.posts.append(json.loads(request.post_data or "{}"))
            if self.hold:
                return self.held.append(route)
            return route.fulfill(status=200, json={"ok": True})
        # Fonts and anything else: the probe runs offline.
        return route.abort()

    def release(self) -> None:
        for route in self.held:
            route.fulfill(status=200, json={"ok": True})
        self.held.clear()


@dataclass
class Run:
    """One fresh page in one browser, with its site."""

    kind: str
    page: Any
    site: Site

    def state(self, card: str = CARD) -> dict:
        return self.page.evaluate(STATE, card)

    def wait(self, ms: int) -> None:
        self.page.wait_for_timeout(ms)

    def settle(self) -> None:
        self.wait(ZOOM_MS + 250)

    def opener(self, card: str = CARD) -> Any:
        return self.page.locator(f"{card} .topic-open")

    def close_button(self) -> Any:
        return self.page.locator(f"{CARD}-dialog .topic-dialog-close")

    def js_click(self, selector: str) -> None:
        # No actionability wait: the button may be moving mid-zoom.
        self.page.evaluate("(s) => document.querySelector(s).click()", selector)


Outcome = tuple[bool, str]


@dataclass(frozen=True)
class Check:
    id: str
    criterion: str
    run: Callable[[Run], Outcome]
    sabotage: Sabotage
    context: str = "desktop"  # desktop, reduced, phone or noscript


# ---- checks: opening (criterion 1) ------------------------------------------------


def _members_shown(r: Run) -> Outcome:
    shown = r.page.evaluate(MEMBERS, CARD)
    long_topic = content().news_clusters[0]
    want = [[m.title, m.urls[0], m.sources[0], m.summary] for m in long_topic.members]
    line = long_topic.members[0].summary
    ok = shown["line"] == line and shown["rows"] == want
    return ok, f"line={shown['line']!r} rows={len(shown['rows'])}/{len(want)}"


def open_by_button_click(r: Run) -> Outcome:
    r.opener().click()
    s = r.state()
    ok, detail = _members_shown(r)
    return s["open"] and s["focusIsClose"] and ok, f"open={s['open']} active={s['active']} {detail}"


def open_by_enter(r: Run) -> Outcome:
    r.opener().focus()
    r.page.keyboard.press("Enter")
    s = r.state()
    return s["open"] and s["focusIsClose"], f"open={s['open']} active={s['active']}"


def open_by_space(r: Run) -> Outcome:
    r.opener().focus()
    r.page.keyboard.press(" ")
    r.wait(100)
    s = r.state()
    return s["open"] and s["focusIsClose"], f"open={s['open']} active={s['active']}"


def open_by_blank_card(r: Run) -> Outcome:
    # The card's padding, clear of its heading, line, tags and votes.
    r.page.locator(CARD).click(position={"x": 6, "y": 6})
    s = r.state()
    return s["open"], f"open={s['open']} active={s['active']}"


def vote_does_not_open(r: Run) -> Outcome:
    r.page.locator(f"{CARD} > .meta .vote-group [data-vote='up']").click()
    r.wait(300)
    s = r.state()
    return not s["open"] and len(r.site.posts) == LONG_MEMBERS, (
        f"open={s['open']} posts={len(r.site.posts)}"
    )


def link_does_not_open(r: Run) -> Outcome:
    # A card holds no link of its own today; one is added to exercise the guard.
    r.page.evaluate(
        "(card) => document.querySelector(card + ' .summary').insertAdjacentHTML('beforeend',"
        ' \' <a id="probe-link" href="#probe-link">a link</a>\')',
        CARD,
    )
    r.page.locator("#probe-link").click()
    r.wait(200)
    s = r.state()
    return not s["open"], f"open={s['open']}"


# ---- checks: closing and focus (criteria 2 and 6) ---------------------------------


def _close_after_open(how: Callable[[Run], None]) -> Callable[[Run], Outcome]:
    def check(r: Run) -> Outcome:
        r.opener().click()
        r.settle()
        how(r)
        mid = r.state()
        r.settle()
        s = r.state()
        # The opening's finished animations hold their last frame until the `close` event
        # cancels them, so only a running one shows that the close zoomed. A close that skips
        # the zoom may still start one (a refused Esc hold runs `requestClose` and then the
        # browser closes), and the queued `close` event cancels it later: the dialog must
        # also still be open, which is what holding the close for the zoom means.
        zoomed = mid["open"] and mid["running"] > 0
        ok = zoomed and not s["open"] and s["focusIsOpener"] and s["anims"] == 0
        return ok, (
            f"zooming={mid['running']} openWhileZooming={mid['open']} open={s['open']} "
            f"active={s['active']}"
        )

    return check


def _press(key: str) -> Callable[[Run], None]:
    return lambda r: r.page.keyboard.press(key)


def _backdrop(r: Run) -> None:
    r.page.mouse.click(8, 450)


def _interrupted(steps: list[str], wait_first: bool) -> Callable[[Run], Outcome]:
    def check(r: Run) -> Outcome:
        r.opener().click()
        if wait_first:
            r.settle()
        r.page.evaluate(FOCUS_LOG)
        for step in steps:
            if step == "close":
                r.js_click(f"{CARD}-dialog .topic-dialog-close")
            else:
                r.page.keyboard.press(step)
            r.wait(40)
        r.settle()
        s = r.state()
        body = r.page.evaluate(FOCUS_LOG_READ)
        ok = not s["open"] and s["focusIsOpener"] and not body and s["anims"] == 0
        return ok, f"open={s['open']} active={s['active']} focusOnBodyAtSomeFrame={body}"

    return check


# ---- checks: the zoom (criterion 6) -----------------------------------------------


def _timing_ok(timings: list[dict]) -> bool:
    easings = {re.sub(r"\s|(?<!\d)0(?=\.)", "", t["easing"]) for t in timings}
    return sorted(t["duration"] for t in timings) == [ZOOM_MS, ZOOM_MS] and easings == {ZOOM_EASING}


def zoom_timing(r: Run) -> Outcome:
    r.opener().click()
    opening = r.page.evaluate(TIMINGS, CARD)
    r.settle()
    r.page.keyboard.press("Escape")
    closing = r.page.evaluate(TIMINGS, CARD)
    r.settle()
    ok = _timing_ok(opening) and _timing_ok(closing) and not r.state()["open"]
    return ok, f"open={opening} close={closing}"


def backdrop_fades(r: Run) -> Outcome:
    # Read as the backdrop's computed opacity with the zoom held, not as the animation's
    # `pseudoElement`: WebKit 27.2 animates the backdrop but reports that as ''.
    r.opener().click()
    opening = [r.page.evaluate(BACKDROP_AT, [CARD, ms]) for ms in (0, ZOOM_MS // 2)]
    r.settle()
    shown = r.page.evaluate(BACKDROP_AT, [CARD, None])
    r.page.keyboard.press("Escape")
    closing = [r.page.evaluate(BACKDROP_AT, [CARD, ms]) for ms in (0, ZOOM_MS // 2)]
    r.settle()
    ok = (
        opening[0] == 0
        and 0 < opening[1] < 1
        and shown == 1
        and closing[0] == 1
        and 0 < closing[1] < 1
        and not r.state()["open"]
    )
    return ok, f"backdrop opacity opening={opening} open={shown} closing={closing}"


def _near(a: list[float], b: list[float], tolerance: float = 1.5) -> bool:
    return all(abs(x - y) <= tolerance for x, y in zip(a, b, strict=True))


def zoom_from_and_to_the_card(r: Run) -> Outcome:
    # By keyboard, with the pointer off every card, so no hover lift moves the card.
    r.page.mouse.move(2, 2)
    r.opener().focus()
    r.page.keyboard.press("Enter")
    start = r.page.evaluate(FRAME, [CARD, 0])
    r.settle()
    end = r.page.evaluate(FRAME, [CARD, None])
    r.page.keyboard.press("Escape")
    # One millisecond short of the end, so playing on finishes rather than restarts it.
    back = r.page.evaluate(FRAME, [CARD, ZOOM_MS - 1])
    r.settle()
    ok = (
        start["n"] == 2
        and end["n"] == 0
        and back["n"] == 2
        and _near(start["at"], start["card"])
        and _near(back["at"], back["card"])
        and not _near(end["at"], end["card"])
    )
    return ok, f"open starts at {start['at']} (card {start['card']}), close ends at {back['at']}"


def reduced_motion_open_and_close(r: Run) -> Outcome:
    r.opener().click()
    s = r.state()
    transform = r.page.evaluate(TRANSFORM, CARD)
    closed_at_once = r.page.evaluate(CLOSED_AT_ONCE, CARD)
    r.wait(100)
    after = r.state()
    ok = (
        s["open"]
        and s["anims"] == 0
        and transform == "none"
        and closed_at_once
        and after["focusIsOpener"]
        and after["anims"] == 0
    )
    return ok, (
        f"anims={s['anims']} transform={transform} closedAtOnce={closed_at_once} "
        f"active={after['active']}"
    )


# ---- checks: votes (criterion 3) --------------------------------------------------


def _vote_in(where: str) -> Callable[[Run], Outcome]:
    def check(r: Run) -> Outcome:
        r.opener().click()
        r.settle()
        if where == "card":
            r.page.keyboard.press("Escape")
            r.settle()
        group = f"{CARD}-dialog .vote-group" if where == "dialog" else f"{CARD} > .meta .vote-group"
        r.site.hold = True
        # By keyboard: a click focuses no button in WebKit, and focus after a vote is checked.
        r.page.locator(f"{group} [data-vote='down']").focus()
        r.page.keyboard.press("Enter")
        for _ in range(40):
            if len(r.site.posts) >= LONG_MEMBERS:
                break
            r.wait(50)
        flight = r.page.evaluate(VOTES, CARD)
        r.site.hold = False
        r.site.release()
        r.page.wait_for_function(
            "(card) => !document.querySelector(card + ' .promote-btn').disabled", arg=CARD
        )
        r.wait(100)
        done = r.page.evaluate(VOTES, CARD)
        same_topic = (
            done["card"]["story"] == done["dialog"]["story"]
            and done["card"]["urls"] == done["dialog"]["urls"]
        )
        in_flight = all(flight["card"]["disabled"]) and all(flight["dialog"]["disabled"])
        agree = done["card"]["pressed"] == done["dialog"]["pressed"] == ["false", "true"]
        focus = done["focus"] == f"{where} down"
        still = r.state()["open"] == (where == "dialog")
        ok = same_topic and in_flight and agree and focus and still
        return ok, (
            f"sameTopic={same_topic} bothDisabledInFlight={in_flight} "
            f"pressed card={done['card']['pressed']} dialog={done['dialog']['pressed']} "
            f"focus={done['focus']}"
        )

    return check


# ---- checks: layers held (criterion 4) --------------------------------------------

LAYER_KEYS = ["ArrowDown"] * 40 + ["End", "End", "n", "N", "PageDown", "PageDown", " ", " "]


def layer_held_while_open(r: Run) -> Outcome:
    r.opener().click()
    r.settle()
    # Focus starts on Close, where Space would press it; a click on the line moves it off.
    r.page.locator(f"{CARD}-dialog .topic-dialog-line").click()
    before = r.state()
    for key in LAYER_KEYS:
        r.page.keyboard.press(key)
    r.page.mouse.move(720, 450)
    for _ in range(30):
        r.page.mouse.wheel(0, 400)
        r.wait(20)
    r.wait(500)
    after = r.state()
    ok = (
        after["open"]
        and after["layer"] == before["layer"]
        and after["scrollY"] == before["scrollY"]
        and after["dialogTop"] > 0
    )
    return ok, (
        f"layer {before['layer']}->{after['layer']} pageScroll {before['scrollY']}->"
        f"{after['scrollY']} dialogScroll={after['dialogTop']} open={after['open']} "
        "(wheel at the dialog's bottom left it open)"
    )


def layer_keys_switch_without_the_dialog(r: Run) -> Outcome:
    # The control: the same keys do switch the layer with no dialog open.
    before = r.state()["layer"]
    for _ in range(3):
        r.page.keyboard.press("End")
        r.wait(400)
    after = r.state()["layer"]
    return after != before, f"layer {before}->{after}"


# ---- checks: touch (criterion 7) --------------------------------------------------


def _touch_driver(r: Run) -> Callable[[str, float, float], str]:
    if r.kind == "chromium":
        cdp = r.page.context.new_cdp_session(r.page)
        names = {"touchstart": "touchStart", "touchmove": "touchMove", "touchend": "touchEnd"}

        def send(kind: str, x: float, y: float) -> str:
            points = [] if kind == "touchend" else [{"x": x, "y": y}]
            cdp.send("Input.dispatchTouchEvent", {"type": names[kind], "touchPoints": points})
            return "CDP Input.dispatchTouchEvent"

        return send
    return lambda kind, x, y: r.page.evaluate(SYNTH_TOUCH, [kind, x, y])


def _gesture(r: Run, y0: float, y1: float, steps: int = 10, after_start=None) -> str:
    send = _touch_driver(r)
    how = send("touchstart", 200, y0)
    if after_start:
        after_start()
    for i in range(1, steps + 1):
        send("touchmove", 200, y0 + (y1 - y0) * i / steps)
        r.wait(16)
    send("touchend", 200, y1)
    return how


def _open_on_phone(r: Run) -> None:
    r.opener().click()
    r.settle()


def phone_full_screen(r: Run) -> Outcome:
    _open_on_phone(r)
    size = r.page.evaluate(SIZE, CARD)
    return size == [390, 844], f"dialog={size}"


def swipe_not_at_bottom_stays_open(r: Run) -> Outcome:
    _open_on_phone(r)
    r.page.evaluate(TO_TOP, CARD)
    how = _gesture(r, 700, 300)
    r.settle()
    s = r.state()
    return s["open"], f"open={s['open']} driver={how}"


def gesture_that_reached_bottom_stays_open(r: Run) -> Outcome:
    _open_on_phone(r)
    r.page.evaluate(TO_TOP, CARD)
    how = _gesture(r, 750, 150, steps=12, after_start=lambda: r.page.evaluate(TO_BOTTOM, CARD))
    r.settle()
    s = r.state()
    return s["open"], f"open={s['open']} driver={how}"


def short_fresh_pull_shows_hint_and_stays_open(r: Run) -> Outcome:
    _open_on_phone(r)
    r.page.evaluate(TO_BOTTOM, CARD)
    r.wait(100)
    send = _touch_driver(r)
    how = send("touchstart", 200, 600)
    for i in range(1, 5):
        send("touchmove", 200, 600 - 20 * i)
        r.wait(16)
    hidden, fill, text = r.page.evaluate(HINT, CARD)
    send("touchend", 200, 520)
    r.settle()
    after = r.page.evaluate(HINT, CARD)
    s = r.state()
    ok = s["open"] and hidden is False and fill == "50%" and "Keep pulling to close" in text
    ok = ok and after[0] is True
    return (
        ok,
        f"hint hidden={hidden} fill={fill} resetOnRelease={after[0]} open={s['open']} driver={how}",
    )


def fresh_pull_past_threshold_closes(r: Run) -> Outcome:
    _open_on_phone(r)
    r.page.evaluate(TO_BOTTOM, CARD)
    r.wait(100)
    how = _gesture(r, 600, 600 - PULL_PX - 20, steps=6)
    r.settle()
    s = r.state()
    ok = not s["open"] and s["focusIsOpener"]
    return ok, f"open={s['open']} active={s['active']} driver={how}"


# ---- checks: no script ------------------------------------------------------------


def no_script_command_opens_and_closes(r: Run) -> Outcome:
    r.opener().click()
    opened = r.page.locator(f"{CARD}-dialog").is_visible()
    r.close_button().click()
    closed = not r.page.locator(f"{CARD}-dialog").is_visible()
    return opened and closed, f"opened={opened} closed={closed}"


# ---- the registry -----------------------------------------------------------------

NO_GUARD = Sabotage(
    "const control = e.target.closest("
    "'a, button, summary, input, select, textarea, label, .vote-group');",
    "const control = null;",
)
# A browser restores focus itself on close, to wherever it was; blurring after that shows
# whether the script, not the browser, is what puts it on the heading button.
NO_FOCUS_RETURN = Sabotage("opener?.focus();", "document.activeElement?.blur();")
NO_HELD_ESC = Sabotage(
    "if (!e.cancelable) return;\n                e.preventDefault();", "if (!e.cancelable) return;"
)
NO_REVERSE = Sabotage(
    "if (running.length) running.forEach((a) => a.reverse());", "if (running.length) return;"
)
NO_TWINS = Sabotage(
    "const twinsOf = (group) => [...document.querySelectorAll('.vote-group')]"
    ".filter((g) => g.dataset.urls === group.dataset.urls);",
    "const twinsOf = (group) => [group];",
)
NO_LAYER_HOLD = Sabotage(
    "const modal = () => !!document.querySelector('dialog[open]');", "const modal = () => false;"
)
ANY_TOUCH_PULLS = Sabotage(
    "if (!closing && e.touches.length === 1 && atBottom()) pullFrom",
    "if (!closing && e.touches.length === 1) pullFrom",
)
NO_PULL_PREVENT = Sabotage(
    "if (pulled >= PULL) { endPull(); requestClose(); }", "if (pulled >= PULL) endPull();"
)

CHECKS = [
    Check(
        "open-by-button-click",
        "1",
        open_by_button_click,
        Sabotage(".topic-member-excerpt {", ".topic-member-excerpt { display: none;"),
    ),
    Check(
        "open-by-enter",
        "1",
        open_by_enter,
        Sabotage("e.preventDefault();\n            open(from);", "e.preventDefault();"),
    ),
    Check(
        "open-by-space",
        "1",
        open_by_space,
        Sabotage("e.preventDefault();\n            open(from);", "e.preventDefault();"),
    ),
    Check(
        "open-by-blank-card",
        "1",
        open_by_blank_card,
        Sabotage("if (!control && String(getSelection()).length) return;", "if (!control) return;"),
    ),
    Check("vote-does-not-open", "1", vote_does_not_open, NO_GUARD),
    Check("link-does-not-open", "1", link_does_not_open, NO_GUARD),
    Check("close-by-esc", "2", _close_after_open(_press("Escape")), NO_HELD_ESC),
    Check(
        "close-by-close-button",
        "2",
        _close_after_open(lambda r: r.close_button().click()),
        Sabotage(
            "else { anims.forEach((a) => a.cancel()); anims = zoom(false); }",
            "else return dialog.close();",
        ),
    ),
    Check(
        "close-by-backdrop",
        "2",
        _close_after_open(_backdrop),
        Sabotage("if (downOutside && outside(e)) requestClose();", "if (false) requestClose();"),
    ),
    Check("focus-returns-after-close", "2", _close_after_open(_press("Escape")), NO_FOCUS_RETURN),
    Check("close-during-opening", "6", _interrupted(["close"], wait_first=False), NO_REVERSE),
    Check("esc-during-opening", "6", _interrupted(["Escape"], wait_first=False), NO_REVERSE),
    Check("esc-x4", "6", _interrupted(["Escape"] * 4, wait_first=True), NO_FOCUS_RETURN),
    Check(
        "close-then-esc-x3",
        "6",
        _interrupted(["close", "Escape", "Escape", "Escape"], wait_first=True),
        NO_FOCUS_RETURN,
    ),
    Check("zoom-timing", "6", zoom_timing, Sabotage("duration: 650", "duration: 400")),
    Check(
        "backdrop-fades",
        "6",
        backdrop_fades,
        Sabotage("{...ZOOM, pseudoElement: '::backdrop'}", "ZOOM"),
    ),
    Check(
        "zoom-from-and-to-the-card",
        "6",
        zoom_from_and_to_the_card,
        Sabotage("translate(${c.left - d.left}px, ${c.top - d.top}px) ", "translate(0px, 0px) "),
    ),
    Check(
        "reduced-motion",
        "6",
        reduced_motion_open_and_close,
        Sabotage("if (!reduced.matches) anims = zoom(true);", "anims = zoom(true);"),
        "reduced",
    ),
    Check("votes-agree-after-a-dialog-vote", "3", _vote_in("dialog"), NO_TWINS),
    Check("votes-agree-after-a-card-vote", "3", _vote_in("card"), NO_TWINS),
    Check("layer-held-while-open", "4", layer_held_while_open, NO_LAYER_HOLD),
    Check(
        "layer-keys-switch-without-dialog",
        "4",
        layer_keys_switch_without_the_dialog,
        Sabotage(
            "const KEYS_DOWN = ['ArrowDown', 'PageDown', 'End']",
            "const KEYS_DOWN = ['ArrowDown', 'PageDown']",
        ),
    ),
    Check(
        "phone-full-screen",
        "7",
        phone_full_screen,
        Sabotage(
            ".topic-dialog { width: 100%; max-width: none;"
            " height: 100%; max-height: none; border: 0; }",
            "",
        ),
        "phone",
    ),
    Check(
        "touch-swipe-not-at-bottom",
        "7",
        swipe_not_at_bottom_stays_open,
        Sabotage(
            "const atBottom = () =>"
            " dialog.scrollTop + dialog.clientHeight >= dialog.scrollHeight - 1;",
            "const atBottom = () => true;",
        ),
        "phone",
    ),
    Check(
        "touch-gesture-that-reached-bottom",
        "7",
        gesture_that_reached_bottom_stays_open,
        ANY_TOUCH_PULLS,
        "phone",
    ),
    Check(
        "touch-short-pull-hint",
        "7",
        short_fresh_pull_shows_hint_and_stays_open,
        Sabotage("hint().hidden = !pulled;", "hint().hidden = true;"),
        "phone",
    ),
    Check("touch-pull-closes", "7", fresh_pull_past_threshold_closes, NO_PULL_PREVENT, "phone"),
    Check(
        "no-script-command",
        "1",
        no_script_command_opens_and_closes,
        Sabotage(
            'commandfor="topic-1-dialog" command="show-modal"',
            'commandfor="topic-1-dialog" command="toggle-popover"',
        ),
        "noscript",
    ),
]


# ---- the runner -------------------------------------------------------------------


def _context(browser: Any, kind: str, which: str) -> Any:
    if which == "phone":
        options = dict(PHONE)
        # Firefox has no mobile emulation; it still takes touch.
        if kind != "firefox":
            options["is_mobile"] = True
        return browser.new_context(**options)
    if which == "reduced":
        return browser.new_context(**DESKTOP, reduced_motion="reduce")
    if which == "noscript":
        return browser.new_context(**DESKTOP, java_script_enabled=False)
    return browser.new_context(**DESKTOP, reduced_motion="no-preference")


def _invoker_commands(browser: Any) -> bool:
    context = browser.new_context()
    try:
        page = context.new_page()
        return page.evaluate("'commandForElement' in HTMLButtonElement.prototype")
    finally:
        context.close()


def run_check(browser: Any, kind: str, check: Check, sabotaged: bool) -> Outcome:
    site = Site(render_page(check.sabotage if sabotaged else None))
    context = _context(browser, kind, check.context)
    try:
        context.route("**/*", site.handle)
        context.add_init_script(HELPERS)
        page = context.new_page()
        page.goto(f"{PAGE_URL}{CARD}")
        if check.context != "noscript":
            # The vote probe has answered and the layers have landed on the Topics layer.
            page.wait_for_function(
                "document.querySelector('.vote-group').style.display === 'inline-flex'"
            )
        page.wait_for_timeout(300)
        page.mouse.move(2, 400)
        return check.run(Run(kind, page, site))
    except Exception as e:  # a check that cannot run is a failure, reported with why
        return False, f"could not run: {str(e).splitlines()[0]}"
    finally:
        context.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--browser", action="append", choices=BROWSERS, help="run in one engine (repeatable)"
    )
    parser.add_argument("--only", action="append", default=[], metavar="ID", help="run one check")
    parser.add_argument(
        "--self-test", action="store_true", help="also run every check sabotaged; each must fail"
    )
    args = parser.parse_args()

    known = {check.id for check in CHECKS}
    if unknown := sorted(set(args.only) - known):
        parser.error(f"unknown check: {', '.join(unknown)}")
    checks = [check for check in CHECKS if not args.only or check.id in args.only]

    from playwright.sync_api import sync_playwright

    results: list[bool] = []
    with sync_playwright() as pw:
        for kind in args.browser or BROWSERS:
            browser = getattr(pw, kind).launch()
            print(f"# {kind} {browser.version}")
            commands = _invoker_commands(browser)
            for check in checks:
                name = f"c{check.criterion} {check.id}"
                if check.context == "noscript" and not commands:
                    print(f"SKIP  {kind:8} {name}  this engine has no invoker commands")
                    continue
                ok, detail = run_check(browser, kind, check, sabotaged=False)
                results.append(ok)
                print(
                    f"{'PASS' if ok else 'FAIL'}  {kind:8} c{check.criterion} {check.id}  {detail}"
                )
                if args.self_test:
                    broke, detail = run_check(browser, kind, check, sabotaged=True)
                    results.append(not broke)
                    verdict = "PASS" if not broke else "FAIL"
                    print(f"{verdict}  {kind:8} {name} sabotaged must fail  {detail}")
            browser.close()
    print(f"{results.count(True)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
