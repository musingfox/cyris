#!/usr/bin/env python3
"""Drive /onboarding in headless Chromium and check what a reader actually gets.

pytest holds the wizard's markup, its redirects and the shape of the bodies it
sends, but not what its script does in a browser: which step a press opens, what
a Next on a changed step writes, that a Skip writes nothing, and whether the page
fits a phone. This runs those checks against a real `TriageServer` whose settings
home, `sources` table and article store are in memory, so what a step wrote can
be read back afterwards, with the Discord probe, the embedder probe and the mail
send answered offline. The votes step's cards come from the real card reader
and draw over a fake RSS edge, which answers each feed in the table with the
same articles every time, as a buffer read again would. The first-issue step
starts its run through a stand-in for the app Worker's `POST /run`, which records
the period it was given and writes the run's row to an in-memory `digest_runs`,
and the step's clock is pinned, so the period it picks is known.

Every check carries a sabotage that breaks the thing it reads. `--self-test`
runs each check clean, where it must pass, and sabotaged, where it must fail,
so a check that cannot fail is reported rather than trusted.

The browser half, which knows no page, is `cdp_probe.py`, shared with the
settings, raw, digest and labels probes.

This script must never be collected by pytest: it needs Chromium and Node,
which makes it a reviewer-run gate rather than a test. Importing it is safe,
and `tests/test_css_receipt.py` does so to check its fixtures and registry.
"""

import argparse
import asyncio
import contextlib
import json
import os
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

import httpx
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

from cyris.adapters.fetch.feed_check import FeedChecker
from cyris.adapters.fetch.onboarding_cards import OnboardingArticles, draw_cards
from cyris.adapters.fetch.rss_worker_source import POLL_CONCURRENCY, POLL_FEED_TIMEOUT_SECONDS
from cyris.config import GRADE_D_KEYS
from cyris.diagnostics.doctor import Check as DoctorCheck
from cyris.domain.models import Article, ArticleState, Lane, SourceConfig
from cyris.entrypoints.triage_server import (
    TriageServer,
    load_onboarding_feeds,
    load_onboarding_topics,
)
from cyris.service_layer.prompts import _language_names

PAGE = "/onboarding"
SETTINGS = "/settings"

KINDS = (
    "empty",
    "empty-largest",
    "refuses-one",
    "has-sources",
    "json",
    "cards",
    "cards-largest",
    "cards-slow",
    "own-feed-slow",
    "first-issue",
    "first-issue-slow",
    "first-issue-fails",
    "first-issue-early",
    "no-timezone",
    "unlisted",
)

# What the reader types, and what each step must then store.
NEW_LANGUAGE = "en"
NEW_TIMEZONE = "Europe/Lisbon"
NEW_WEBHOOK = "https://discord.com/api/webhooks/9/NEWTOKEN"
NEW_MAIL_TO = "me@example.org"
NEW_MAIL_FROM = "digest@example.org"
UNKNOWN_TIMEZONE = "Mars/Base"
# The zone the probe's browser reports as its own (`PIN_ZONE`), so a check knows
# what a field with no stored zone opens on: not the probe's stored Asia/Taipei.
BROWSER_ZONE = "America/Halifax"
# What the `unlisted` deployment stores: a language tag languages.json does not
# list, which the model is still told as written, and a zone no browser lists.
UNLISTED_LANGUAGE = "yue-Hant"
UNLISTED_TIMEZONE = "Cyris/Probe"
OFFLINE_DETAIL = "cyris-probe: answered offline"

# A deployment whose settings are all stored and whose notifications are not set
# yet: what the wizard opens on after a first boot. A key the registry adds
# without a value here fails at import.
_VALUES = {
    "general.digest_schedule": ["08:00", "20:00"],
    "general.timezone": "Asia/Taipei",
    "general.digest_window_hours": 24,
    "llm_provider.provider": "gemini",
    "llm_provider.model": "",
    "llm_provider.reasoning_effort": "default",
    "notify.discord_webhook_url": "",
    "notify.email_to": "",
    "notify.email_from": "",
    "digest.max_articles_per_digest": 200,
    "digest.max_articles_per_digest_output": 15,
    "digest.max_featured": 5,
    "digest.units_per_paragraph": 5,
    "digest.scoring_snippet_length": 1000,
    "digest.summarize_snippet_length": 1000,
    "digest.filter_snippet_length": 500,
    "digest.output_language": "zh-Hant",
    "digest.style_prompt": "",
    "digest.type_scale": 1,
    "digest.theme": "default",
    "digest.rank_by_preference": False,
    "digest.preference_source": "cosine",
    "routing.score_threshold": 70,
    "routing.summarize_score_threshold": 70,
    "vote_similarity.enabled": False,
    "vote_similarity.provider": "workers_ai",
    "vote_similarity.model": "",
    "vote_similarity.max_seeds": 200,
}
PROBE_VALUES = {key: _VALUES[key] for key in GRADE_D_KEYS}
# Each fixture's stored values where they are not PROBE_VALUES.
FIXTURE_VALUES = {
    "no-timezone": {k: v for k, v in PROBE_VALUES.items() if k != "general.timezone"},
    "unlisted": PROBE_VALUES
    | {"digest.output_language": UNLISTED_LANGUAGE, "general.timezone": UNLISTED_TIMEZONE},
}
# languages.json's languages, in its order, as the language field must offer them.
LANGUAGES = list(_language_names().items())

A_SOURCE = SourceConfig(name="Simon Willison", url="https://simonwillison.net/atom/everything/")

# The feed list the server offers, and the two the reader picks: those with the
# most tags, so a body that lost one, or its order, is told from the list's own.
# Ranked rather than filtered, so a list with one multi-tag entry still gives two,
# and kept in the list's order, which is the order the page posts them in.
FEEDS = load_onboarding_feeds()
TOPICS = load_onboarding_topics()
LABELS = {topic["id"]: topic["label"] for topic in TOPICS}
_MOST_TAGS = sorted(FEEDS, key=lambda entry: -len(entry["tags"]))[:2]
PICKS = [entry for entry in FEEDS if entry in _MOST_TAGS]
# The feed the `refuses-one` table turns down once, and the one it takes beside it.
REFUSED, TAKEN = FEEDS[1], FEEDS[0]
REFUSAL = "D1 is busy; try again"
MISNAMED = f"Added 1 of 2 feeds. {TAKEN['name']}: {REFUSAL}"


def as_posted(entry: dict) -> dict:
    """What the page posts for a picked entry: the entry without the list's own `topics`."""
    return {key: value for key, value in entry.items() if key != "topics"}


def shown_feeds(chosen: list[str]) -> list[str]:
    """The feed names the step shows with `chosen` topics, in its order.

    One checklist per chosen topic, in the topic list's order; a feed sits under
    the first of its own topics that is chosen, so it shows once.
    """
    order = [topic["id"] for topic in TOPICS if topic["id"] in chosen]
    home = {e["name"]: next((t for t in e["topics"] if t in chosen), None) for e in FEEDS}
    return [e["name"] for topic in order for e in FEEDS if home[e["name"]] == topic]


# The topic checks: one topic alone, and the first two topics in the list's order
# that share a feed, which must then show once.
ONE_TOPIC = TOPICS[0]["id"]
OTHER_TOPIC = next(
    t["id"] for t in TOPICS if set(shown_feeds([t["id"]])) - set(shown_feeds([ONE_TOPIC]))
)
TWO_TOPICS, SHARED = next(
    ([a["id"], b["id"]], e["name"])
    for i, a in enumerate(TOPICS)
    for b in TOPICS[i + 1 :]
    for e in FEEDS
    if {a["id"], b["id"]} <= set(e["topics"])
)
# The pick kept when its topic is dropped: one feed with a single topic.
KEPT_PICK = next(entry for entry in FEEDS if len(entry["topics"]) == 1)
# The step's heading over such picks (onboarding.js KEPT), and the key the
# chosen topics are kept under for a reload (onboarding.js TOPICS_KEY).
KEPT = "Picked, outside the chosen topics"
TOPICS_KEY = "cyris-onboarding-topics"
# How many picks the Votes step's poll fetches within a minute (onboarding.js
# SLOW_PICKS): past it the feeds screen warns of the wait.
SLOW_PICKS = POLL_CONCURRENCY * (60 // POLL_FEED_TIMEOUT_SECONDS)

# The `cards` table: the two picks, which the fake RSS edge answers, and a
# third feed it answers with nothing, which the step must name.
QUIET = next(entry for entry in FEEDS if entry not in PICKS)
CARDS_TABLE = {entry["name"]: SourceConfig.model_validate(entry) for entry in [*PICKS, QUIET]}
ARTICLES_PER_FEED = 6
# How long the `cards-slow` read takes: long enough to look at the wait.
SLOW_SECONDS = 3
_NOW = datetime.now(UTC)

# The first-issue fixtures' clock: 10:30 in the probe's Asia/Taipei, between its
# 08:00 and 20:00, so the step takes today's morning; `first-issue-early` is
# 06:30, before both, so it takes yesterday's evening.
TAIPEI = ZoneInfo(PROBE_VALUES["general.timezone"])
ISSUE_DATE = "2026-10-20"
YESTERDAY = "2026-10-19"
BETWEEN = datetime(2026, 10, 20, 10, 30, tzinfo=TAIPEI)
EARLY = datetime(2026, 10, 20, 6, 30, tzinfo=TAIPEI)
ISSUE_PERIOD = "morning"
# What `POST /run` is given, as (period, date), for each fixture's first issue.
ISSUE_RUN = (ISSUE_PERIOD, ISSUE_DATE)
EARLY_RUN = ("evening", YESTERDAY)
DIGEST_URL = f"https://cyris.example/{ISSUE_DATE}-{ISSUE_PERIOD}"
ARCHIVE_URL = "https://cyris.example/"
FAILURE = "RuntimeError: the Gemini key was refused"
# What each first-issue fixture's runs end as, one per POST /run in order; a POST
# past the end starts a run that never records a row, as one still going.
RUN_ENDS = {
    "first-issue": ["ok"],
    "first-issue-slow": [],
    "first-issue-fails": ["error", "ok"],
    "first-issue-early": ["ok"],
}
# How long a check waits for the page to read a run's row: the page reads every
# 5 seconds (POLL_MS in onboarding.js).
RUN_READ_MS = 15000


# "Add your own feed": a home page that links its feed, the feed itself, and a
# page that links none, each answered by `FakeHttp`; any other address is
# unreachable. The feed's title is no name the list holds.
OWN_HOME = "https://own.test/"
OWN_FEED = "https://own.test/feed.xml"
OWN_NAME = "Own Probe Notes"
BARE_HOME = "https://bare.test/"
# What Next posts for the own feed: its name and address, no tags, the lane left
# to the source's default.
OWN_BODY = {"name": OWN_NAME, "url": OWN_FEED, "tags": []}
# A listed feed outside ONE_TOPIC: pasting its address checks its row instead.
LISTED = next(entry for entry in FEEDS if ONE_TOPIC not in entry["topics"])
NO_FEED_LINK = json.loads(
    (files("cyris.adapters.fetch") / "feed_check.json").read_text(encoding="utf-8")
)["messages"]["no_feed_link"].format(url=BARE_HOME)
_OWN_ANSWERS = {
    OWN_HOME: (
        "text/html",
        '<html><head><link rel="alternate" type="application/rss+xml" href="/feed.xml">'
        f"</head><body>{OWN_NAME}</body></html>",
    ),
    OWN_FEED: (
        "application/rss+xml",
        f'<?xml version="1.0"?><rss version="2.0"><channel><title>{OWN_NAME}</title>'
        "<item><title>One</title><link>https://own.test/1</link></item></channel></rss>",
    ),
    BARE_HOME: ("text/html", "<html><head><title>Bare</title></head><body>Hi</body></html>"),
}


class FakeHttp:
    """Stands in for `HttpClient` under the feed check: `_OWN_ANSWERS`, else unreachable."""

    def __init__(self, delay: float = 0) -> None:
        self.delay = delay

    async def get(self, url: str) -> httpx.Response:
        await asyncio.sleep(self.delay)
        if url not in _OWN_ANSWERS:
            raise httpx.ConnectError("cyris-probe: offline")
        kind, text = _OWN_ANSWERS[url]
        return httpx.Response(
            200, text=text, headers={"Content-Type": kind}, request=httpx.Request("GET", url)
        )

    async def close(self) -> None:
        pass


def feed_articles(name: str) -> list[Article]:
    """What the fake RSS edge answers for one feed: the n-th article (n + 1) * 6 hours old."""
    slug = "".join(char for char in name.lower() if char.isalnum()) or "feed"
    return [
        Article(
            id=f"{slug}-{n}",
            title=f"{name}: story {n}",
            url=f"https://cards.test/{slug}/{n}",
            content=f"<p>What story {n} is about.</p>",
            published_at=_NOW - timedelta(hours=6 * (n + 1)),
            source_name=name,
            source_lane=Lane.SKIM,
        )
        for n in range(ARTICLES_PER_FEED)
    ]


def _answered(sources: dict[str, SourceConfig]) -> list[Article]:
    return [a for name in sources if name != QUIET["name"] for a in feed_articles(name)]


# The cards the `cards` fixture deals, in the order the page shows them.
CARDS = draw_cards(_answered(CARDS_TABLE), CARDS_TABLE).cards
VOTE_SIMILARITY_ON = {
    "vote_similarity.enabled": True,
    "vote_similarity.provider": PROBE_VALUES["vote_similarity.provider"],
    "vote_similarity.model": PROBE_VALUES["vote_similarity.model"],
    "vote_similarity.max_seeds": PROBE_VALUES["vote_similarity.max_seeds"],
}


# What the first-issue step writes on a deployment with a source, voted or not.
ON = (VOTE_SIMILARITY_ON,)


class FakeSettings:
    """Stands in for `D1Settings` and records every write a step makes."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def set(self, values: dict) -> None:
        self.calls.append(dict(values))


class FakeSourceStore:
    """Stands in for the D1 `sources` table, empty or not, and records every row it stored.

    A name in `refuse_once` is turned down the first time it is written, as a
    busy D1 would, and taken the next.
    """

    def __init__(self, sources: dict[str, SourceConfig], refuse_once: set[str] = frozenset()):
        self.sources = sources
        self.refuse = set(refuse_once)
        self.writes: list[SourceConfig] = []

    def list_sources(self) -> dict[str, SourceConfig]:
        return dict(self.sources)

    def upsert(self, source: SourceConfig) -> None:
        if source.name in self.refuse:
            self.refuse.discard(source.name)
            raise RuntimeError(REFUSAL)
        self.writes.append(source)
        self.sources[source.name] = source


class FakeArticleStore:
    """Stands in for `D1ArticleStore`, keeping each row's state and stamp by URL.

    As D1 does, `save` inserts only a URL not held yet, and a verdict or a stamp
    lands only on a row that exists, so a vote sent before its save leaves no trace.
    """

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def save(self, articles: list[Article], now: datetime | None = None) -> None:
        for article in articles:
            self.rows.setdefault(
                article.url, {"state": ArticleState.PENDING.value, "triaged_at": None}
            )

    def _set(self, urls: list[str], **values) -> int:
        held = [url for url in urls if url in self.rows]
        for url in held:
            self.rows[url].update(values)
        return len(held)

    def accept(self, urls: list[str]) -> int:
        return self._set(urls, state=ArticleState.ACCEPTED.value)

    def reject(self, urls: list[str], reason: str) -> int:
        return self._set(urls, state=ArticleState.REJECTED.value)

    def update_triage_timestamp(self, urls: list[str], triaged_at: datetime) -> int:
        return self._set(urls, triaged_at=triaged_at.isoformat())


class FakeRunLog:
    """Stands in for `D1RunLog`: rows in the order they were recorded, the newest read back.

    It opens with one earlier run, so the page has to tell its own run's row
    from one already there.
    """

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.record("ok", "evening", digest_url="https://cyris.example/2026-10-18-evening")

    def record(self, status: str, period: str, **summary) -> None:
        self.rows.append(
            {
                "id": len(self.rows) + 1,
                "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "status": status,
                "period": period,
                "digest_url": summary.get("digest_url"),
                "error": summary.get("error"),
                "failed_sources": [],
            }
        )

    def newest(self) -> dict | None:
        return self.rows[-1] if self.rows else None


class FakeDigestStore:
    """Stands in for `D1DigestStore`'s list of stored issues; none for the first issue's key."""

    def issues(self) -> list[tuple[str, str]]:
        return [("2026-10-18", "evening")]


class FakeRssEdge:
    """The run's RSS `FetchSource`, answering each feed it is asked for but `QUIET`."""

    def __init__(self, delay: float = 0) -> None:
        self.delay = delay

    async def fetch_articles(self, after, before, sources) -> list[Article]:
        await asyncio.sleep(self.delay)
        return _answered(sources)

    async def health_check(self) -> bool:
        return True


@dataclass
class Fixture:
    """One server and the in-memory homes a check reads its receipt from."""

    app: web.Application
    settings: FakeSettings | None
    calls: list[dict] = field(default_factory=list)
    # Every source the table stored, in order; None on a `json` deployment.
    sources: FakeSourceStore | None = None
    # The article rows the votes step wrote; None on a `json` deployment.
    articles: FakeArticleStore | None = None
    # The period of each POST /run, in order.
    runs: list[str] = field(default_factory=list)
    # The body of each POST /api/sources, as the page sent it, in order.
    posted: list[dict] = field(default_factory=list)


def build_fixture(kind: str) -> Fixture:
    """A deployment the wizard is for, or one of the two it is not.

    `empty` has a writable, empty source table, which is what waiting for
    onboarding is; `empty-largest` is that page at the largest type size, and
    `refuses-one` is that table turning `REFUSED` down once. `has-sources` has
    one source, so the wizard opens without the feed list, and `json` has no
    writable table at all, which is a `backend = "json"` deployment: it has no
    wizard. `cards` holds `CARDS_TABLE`, so the votes step deals `CARDS` and
    names `QUIET`; `cards-largest` is it at the largest type size, and
    `cards-slow` takes `SLOW_SECONDS` to read its feeds, as `own-feed-slow`
    takes them to check a pasted address. The `first-issue`
    ones hold `A_SOURCE` and a clock pinned at `BETWEEN`, and their runs end as
    `RUN_ENDS` says; `first-issue-early`'s clock is at `EARLY`. `no-timezone`
    is `empty` with no zone stored, and `unlisted` stores a language and a zone
    neither list holds (`FIXTURE_VALUES`).
    """
    if kind not in KINDS:
        raise ValueError(f"unknown fixture {kind!r}")
    settings = FakeSettings()
    table = {"Simon Willison": A_SOURCE} if kind == "has-sources" else {}
    if kind.startswith("cards"):
        table = dict(CARDS_TABLE)
    if kind.startswith("first-issue"):
        table = {A_SOURCE.name: A_SOURCE}
    run_log = None if kind == "json" else FakeRunLog()
    refuse = {REFUSED["name"]} if kind == "refuses-one" else set()
    store = None if kind == "json" else FakeSourceStore(table, refuse)
    articles = None if kind == "json" else FakeArticleStore()
    edge = FakeRssEdge(SLOW_SECONDS if kind == "cards-slow" else 0)
    server = TriageServer(
        settings=settings,
        values=dict(FIXTURE_VALUES.get(kind, PROBE_VALUES)),
        source_store=store,
        article_store=articles,
        onboarding_articles=None if kind == "json" else OnboardingArticles(edge),
        feed_checker=None
        if kind == "json"
        else FeedChecker(FakeHttp(SLOW_SECONDS if kind == "own-feed-slow" else 0)),
        digest_store=None if kind == "json" else FakeDigestStore(),
        run_log=run_log,
        clock=(lambda: EARLY) if kind == "first-issue-early" else (lambda: BETWEEN),
    )
    if kind.endswith("-largest"):
        server._onboarding_page = at_largest_type_scale(server._onboarding_page)

    # The app Worker's own route, which the container does not serve: without it
    # the site bar's probe fails closed and its links stay hidden.
    async def signed_in(_: web.Request) -> web.Response:
        return web.json_response({"authorized": True})

    server._app.router.add_get("/api/vote", signed_in)
    fixture = Fixture(server._app, settings, settings.calls, store, articles)

    # Each source body as it arrived, before the route reads it as a
    # `SourceConfig`, which would drop a field the page should not have sent.
    @web.middleware
    async def record_posts(request: web.Request, handler):
        if request.method == "POST" and request.path == "/api/sources":
            fixture.posted.append(await request.json())
        return await handler(request)

    server._app.middlewares.append(record_posts)
    ends = list(RUN_ENDS.get(kind, []))

    # The app Worker's `POST /run?period=&date=`, which the container does not
    # serve either: it starts the run and answers at once, and the run records
    # its row. As the Worker does, it takes a date only for today or yesterday
    # in the deployment's time zone, here on the fixture's own clock.
    today = server._clock().date()
    known = {today.isoformat(), (today - timedelta(days=1)).isoformat()}

    async def start_run(request: web.Request) -> web.Response:
        period = request.query.get("period")
        date = request.query.get("date")
        if period not in ("morning", "evening"):
            return web.json_response(
                {"error": "period must be one of morning, evening"}, status=400
            )
        if date is not None and date not in known:
            return web.json_response({"error": "date must be today or yesterday"}, status=400)
        fixture.runs.append((period, date))
        if ends:
            status = ends.pop(0)
            dated = date or today.isoformat()
            url = f"https://cyris.example/{dated}-{period}" if status == "ok" else None
            error = FAILURE if status == "error" else None
            run_log.record(status, period, digest_url=url, error=error)
        return web.json_response({"started": "run", "at": datetime.now(UTC).isoformat()})

    server._app.router.add_post("/run", start_run)
    return fixture


@contextlib.contextmanager
def probe_environment() -> Iterator[SimpleNamespace]:
    """Answer the Discord and embedder probes and the mail send offline, from an empty directory.

    Each route reaches a third party on a real save, and the mail route also
    needs this deployment's Cloudflare credentials to get that far. Yields the
    patched edges so a caller can see they were the ones answering.
    """
    offline = DoctorCheck(name="probe", status="ok", detail=OFFLINE_DETAIL)
    discord = mock.AsyncMock(return_value=offline)
    embedder = mock.AsyncMock(return_value=offline)
    mail = mock.AsyncMock(return_value="delivered")
    present = {
        "CLOUDFLARE_ACCOUNT_ID": "cyris-probe-account",
        "CLOUDFLARE_API_TOKEN": "cyris-probe-token",
    }
    with (
        tempfile.TemporaryDirectory(prefix="onboarding-probe-") as home,
        contextlib.chdir(home),
        mock.patch.dict(os.environ, present),
        mock.patch("cyris.entrypoints.triage_server.probe_discord", discord),
        mock.patch("cyris.entrypoints.triage_server.probe_embedder", embedder),
        mock.patch("cyris.adapters.mail.send_mail", mail),
    ):
        yield SimpleNamespace(discord=discord, embedder=embedder, mail=mail)


def rewrite_post(path: str, mutation: str) -> str:
    """A preload that changes the body of the page's POST to `path` before it leaves."""
    return f"""
{{
const realFetch = window.fetch;
window.fetch = (input, init) => {{
  if (String(input).endsWith({json.dumps(path)}) && init && init.method === "POST") {{
    const body = JSON.parse(init.body);
    {mutation}
    init = {{...init, body: JSON.stringify(body)}};
  }}
  return realFetch(input, init);
}};
}}
"""


# The second card dealt carries a URL no feed holds, as one a feed rotated out
# after the cards were dealt: the votes route then finds no such article.
GONE_CARD = """{
const realFetch = window.fetch;
window.fetch = async (input, init) => {
  const response = await realFetch(input, init);
  if (!String(input).endsWith("/api/onboarding/cards")) return response;
  const body = await response.json();
  body.cards[1].url = "https://gone.test/rolled-off";
  return new Response(JSON.stringify(body), {status: response.status});
};
}"""


# The browser reports `BROWSER_ZONE` as its own zone, whatever the machine's is.
PIN_ZONE = f"""{{
const realOptions = Intl.DateTimeFormat.prototype.resolvedOptions;
Intl.DateTimeFormat.prototype.resolvedOptions = function () {{
  return {{...realOptions.call(this), timeZone: {json.dumps(BROWSER_ZONE)}}};
}};
}}"""

# A sabotage: Skip presses Next, so a step the reader skipped is saved after all.
SKIP_SAVES = """{
document.addEventListener("DOMContentLoaded", () => {
  const skip = document.getElementById("o-skip");
  skip.addEventListener("click", (e) => e.stopImmediatePropagation(), true);
  skip.addEventListener("click", () => document.getElementById("o-next").click(), true);
});
}"""


# The wizard's way out, kept on the page: a press on Finish records where it leads
# in `window.__left` instead of leaving, so the check can read it afterwards.
# Storage the browser refuses, as with site data blocked: every write throws.
NO_STORAGE = """{
Storage.prototype.setItem = () => { throw new DOMException("blocked", "SecurityError"); };
}"""

STAY = """{
document.addEventListener("click", (e) => {
  const finish = e.target.closest && e.target.closest("#o-finish");
  if (!finish) return;
  e.preventDefault();
  window.__left = finish.getAttribute("href");
}, true);
}"""

# A sabotage: Skip on the last step presses Start, so a skipped first issue runs after all.
SKIP_STARTS = """{
document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("o-skip").addEventListener("click", (e) => {
    e.stopImmediatePropagation();
    document.getElementById("o-start").click();
  }, true);
});
}"""

# A sabotage: the first issue before the morning is dated today, a key the
# 20:00 tick stores over.
RUN_TODAY = """{
const realFetch = window.fetch;
window.fetch = (input, init) => {
  if (String(input).startsWith("/run?")) {
    input = String(input).replace("date=2026-10-19", "date=2026-10-20");
  }
  return realFetch(input, init);
};
}"""

# A sabotage: the run is started for the evening, which the 20:00 tick stores over.
RUN_EVENING = """{
const realFetch = window.fetch;
window.fetch = (input, init) => {
  if (String(input).startsWith("/run?")) {
    input = String(input).replace("period=morning", "period=evening");
  }
  return realFetch(input, init);
};
}"""


def dead(button: str) -> str:
    """A sabotage: the button is swapped for a copy, which carries none of its listeners."""
    return f"""$("#{button}").replaceWith($("#{button}").cloneNode(true));"""


PRELUDE = (
    base_prelude()
    + """
const setValue = (el, value) => {
  el.value = value;
  el.dispatchEvent(new Event("input", {bubbles: true}));
  el.dispatchEvent(new Event("change", {bubbles: true}));
};
// A select's option, added first when it lists none: a zone the browser offers
// that the server's own zone database may not know.
const offer = (el, value) => {
  if (![...el.options].some((option) => option.value === value)) el.add(new Option(value, value));
  setValue(el, value);
};
const options = (el) => [...el.options].map((option) => [option.value, option.textContent]);
const shownSteps = () => $$(".step").filter(visible).map((panel) => panel.dataset.step);
const marked = () => $$('#o-steps li[aria-current="step"]').map((item) => item.dataset.step);
const progress = () => $("#o-progress").textContent;
const notice = () => $("#o-result");
// A notice showing a save's result, not what the save is still doing.
const settled = () => visible(notice()) && notice().getAttribute("aria-busy") !== "true";
const onStep = (step) => waitFor(() => same(shownSteps(), [step]), `the ${step} step`);
const press = (id) => $(`#${id}`).click();
// The stored settings arrived: the step's own values are in its fields.
const loaded = () => waitFor(() => $("#o-timezone").value && !$("[data-loading]"), "the settings");
const skipTo = async (step) => {
  for (let left = 6; left > 0 && shownSteps()[0] !== step; left--) {
    press("o-skip");
    await sleep(25);
  }
  return onStep(step);
};
const marks = () => $$('.step:not([hidden]) [aria-invalid="true"]').map((el) => el.id);
// The sources step, drawn: its topic tiles, or the line saying the table has
// sources. A feed row shows only on the feeds screen, once one of its topics
// is chosen.
const topicRows = () => $$("#o-topics .tile");
const feedRows = () => $$("#o-feeds .choice");
const feedsDrawn = () => waitFor(
  () => topicRows().length || visible($("#o-feeds-had")), "the feed list");
const rowIn = (rows, name) => rows.find((row) => $(".name", row).textContent === name);
const rowNamed = (name) => rowIn(feedRows(), name);
const shownFeeds = () => feedRows().filter(visible).map((row) => $(".name", row).textContent);
const groupsShown = () => $$("#o-feeds fieldset").filter(visible)
  .map((set) => $("legend", set).textContent);
const topicTile = (label) => rowIn(topicRows(), label);
const topicChosen = (label) => topicTile(label).getAttribute("aria-pressed") === "true";
// Idempotent: a topic already chosen stays chosen.
const choose = (label) => { if (!topicChosen(label)) topicTile(label).click(); };
const unchoose = (label) => { if (topicChosen(label)) topicTile(label).click(); };
// The step's two screens: the topics, then their feeds, each with its own hash.
const topicsScreen = () => visible($("#o-topics-screen"));
const feedsScreen = () => visible($("#o-feeds-screen"));
const toFeeds = async () => {
  if (!feedsScreen()) press("o-next");
  await waitFor(feedsScreen, "the feeds screen");
};
const toTopics = async () => {
  if (!topicsScreen()) press("o-back");
  await waitFor(topicsScreen, "the topics screen");
};
const picked = () => $("#o-picked").textContent;
// "Add your own feed": its rows under Your feeds, and its own notice.
const ownRows = () => $$("#o-own .choice");
const ownNotice = () => $("#o-own-result");
const ownSettled = () => visible(ownNotice()) && ownNotice().getAttribute("aria-busy") !== "true";
const addOwn = async (url) => {
  setValue($("#o-own-url"), url);
  press("o-own-add");
  await waitFor(ownSettled, "the feed check");
};
const pick = (name) => { rowNamed(name).querySelector("input").click(); };
// The votes step, dealt: its cards, or the line saying there is nothing to vote on.
const cardRows = () => $$("#o-cards .vote-card");
const cardsDrawn = () => waitFor(
  () => cardRows().length || visible($("#o-cards-none")), "the cards");
const cardTitle = (row) => $(".title", row).textContent;
const cardSource = (row) => $(".label", row).textContent;
const vote = (index, answer) => { $(`button[data-vote="${answer}"]`, cardRows()[index]).click(); };
// The first-issue step, read: its plan, or the reason it could not be read.
const issueRead = () => waitFor(
  () => !visible($("#o-issue-loading")) && (visible($("#o-issue-plan")) || visible(notice())),
  "the first issue's plan");
const plan = () => $("#o-issue-plan").textContent;
// The run's outcome, read from its row: the page reads one every few seconds.
const runEnded = () => waitFor(settled, "the run's outcome", RUN_READ_MS);
""".replace("RUN_READ_MS", str(RUN_READ_MS))
)


def _calls(*wanted: dict) -> Callable[[Fixture], str | None]:
    """A receipt: the settings home took exactly these writes, in order."""
    return lambda fixture: None if fixture.calls == list(wanted) else f"writes: {fixture.calls}"


def _sources(*wanted: dict) -> Callable[[Fixture], str | None]:
    """A receipt: the table stored exactly these list entries, field for field, in order.

    Each stored row is read back in the entry's own fields but `topics`, which
    is the list's and not a source's, so a tag or a lane the page changed shows,
    and with no area, which the list cannot know.
    """
    wanted = tuple(as_posted(entry) for entry in wanted)

    def receipt(fixture: Fixture) -> str | None:
        rows = fixture.sources.writes
        if len(rows) == len(wanted) and fixture.calls == []:
            pairs = zip(rows, wanted, strict=True)
            if all(
                row.model_dump(mode="json", include=set(entry)) == entry
                and row.area_id is None
                and row.type == "rss"
                for row, entry in pairs
            ):
                return None
        dumped = [row.model_dump(mode="json") for row in rows]
        return f"sources: {dumped}, settings: {fixture.calls}"

    return receipt


def _posted(*entries: dict) -> Callable[[Fixture], str | None]:
    """A receipt: each POST /api/sources body was its entry less `topics`, and the table took them.

    Read from the bodies as sent: the route reads a body as a `SourceConfig`,
    which drops `topics`, so the stored rows alone could not tell.
    """
    stored = _sources(*entries)

    def receipt(fixture: Fixture) -> str | None:
        if fixture.posted != [as_posted(entry) for entry in entries]:
            return f"posted: {fixture.posted}"
        return stored(fixture)

    return receipt


def _own_posted(fixture: Fixture) -> str | None:
    """A receipt: Next posted the own feed alone, as `OWN_BODY`, stored in the default lane."""
    rows = [row.model_dump(mode="json") for row in fixture.sources.writes]
    wanted = SourceConfig.model_validate(OWN_BODY).model_dump(mode="json")
    if fixture.posted == [OWN_BODY] and rows == [wanted] and fixture.calls == []:
        return None
    return f"posted: {fixture.posted}, sources: {rows}, settings: {fixture.calls}"


# A sabotage: the feed check answers that a page with no feed link is a feed.
CHECK_PASSES = f"""{{
const realFetch = window.fetch;
window.fetch = (input, init) => {{
  if (String(input).endsWith("/api/onboarding/feed-check")) {{
    const body = {{ok: true, name: "Bare", url: {json.dumps(BARE_HOME)}}};
    return Promise.resolve(new Response(JSON.stringify(body), {{status: 200}}));
  }}
  return realFetch(input, init);
}};
}}"""


def _both_untouched(fixture: Fixture) -> str | None:
    """A receipt: neither the settings home nor the table took a write."""
    return _sources()(fixture)


def _votes(*, up: tuple[int, ...] = (), down: tuple[int, ...] = ()) -> Callable:
    """A receipt: exactly these `CARDS` were stored, each with its verdict and a stamp.

    The step writes no setting: vote similarity is the first-issue step's.
    """
    wanted = {CARDS[i].url: ArticleState.ACCEPTED.value for i in up}
    wanted |= {CARDS[i].url: ArticleState.REJECTED.value for i in down}
    settings = []

    def receipt(fixture: Fixture) -> str | None:
        rows = fixture.articles.rows
        stored = {url: row["state"] for url, row in rows.items()}
        stamped = all(row["triaged_at"] for row in rows.values())
        no_source = fixture.sources.writes == []
        if stored == wanted and stamped and fixture.calls == settings and no_source:
            return None
        return f"articles: {rows}, settings: {fixture.calls}, sources: {fixture.sources.writes}"

    return receipt


_NOTHING_VOTED = _votes()


def _runs(
    *keys: tuple[str, str | None], settings: tuple[dict, ...] = ()
) -> Callable[[Fixture], str | None]:
    """A receipt: `POST /run` took exactly these (period, date), in order, and the
    settings home only these writes; no source was written."""

    def receipt(fixture: Fixture) -> str | None:
        ok = fixture.runs == list(keys) and fixture.calls == list(settings)
        if ok and not fixture.sources.writes:
            return None
        return f"runs: {fixture.runs}, settings: {fixture.calls}, sources: {fixture.sources.writes}"

    return receipt


def _choose(*topics: str) -> str:
    return "\n".join(f"choose({json.dumps(LABELS[topic])});" for topic in topics)


def _feeds_of(*topics: str) -> str:
    """Choose `topics` on the topics screen, then open the feeds screen."""
    return _choose(*topics) + "\nawait toFeeds();"


def _pick(*entries: dict) -> str:
    """Choose each entry's first topic, open the feeds screen, so its row shows, then check it."""
    topics = list(dict.fromkeys(entry["topics"][0] for entry in entries))
    return "\n".join(
        [_feeds_of(*topics), *(f"pick({json.dumps(entry['name'])});" for entry in entries)]
    )


# Typing into one step, by the step it belongs to.
TYPE_LANGUAGE = f"""
setValue($("#o-output-language"), {json.dumps(NEW_LANGUAGE)});
setValue($("#o-timezone"), {json.dumps(NEW_TIMEZONE)});
"""
TYPE_HOURS = """
setValue($("#o-morning"), "7");
setValue($("#o-evening"), "19");
"""
TYPE_WEBHOOK = f"""setValue($("#o-discord-webhook"), {json.dumps(NEW_WEBHOOK)});"""
TYPE_MAIL = f"""
setValue($("#o-email-to"), {json.dumps(NEW_MAIL_TO)});
setValue($("#o-email-from"), {json.dumps(NEW_MAIL_FROM)});
"""

LANGUAGE_STORED = {"digest.output_language": NEW_LANGUAGE, "general.timezone": NEW_TIMEZONE}
HOURS_STORED = {"general.digest_schedule": ["07:00", "19:00"]}
WEBHOOK_STORED = {"notify.discord_webhook_url": NEW_WEBHOOK}
MAIL_STORED = {"notify.email_to": NEW_MAIL_TO, "notify.email_from": NEW_MAIL_FROM}

_CHECKS: list[Check] = [
    Check(
        id="opens-on-the-first-step",
        fixture="empty",
        path=PAGE,
        act="await loaded();",
        script="""
            expect(same(shownSteps(), ["language"]), `shown: ${shownSteps()}`);
            expect(same(marked(), ["language"]), `marked: ${marked()}`);
            expect(progress() === "Step 1 of 6", `progress: ${progress()}`);
            expect(!visible($("#o-back")), "Back shows on the first step");
            expect(visible($("#o-next")) && visible($("#o-skip")), "Next or Skip is hidden");
            expect(!visible($("#o-finish")), "Finish shows before the last step");
        """,
        sabotage="""$('.step[data-step="schedule"]').hidden = false;""",
    ),
    Check(
        id="the-step-list-names-every-step",
        fixture="empty",
        path=PAGE,
        act="await loaded();",
        script="""
            const names = $$("#o-steps li").filter(visible).map((li) => li.dataset.step);
            const wanted = ["language", "schedule", "sources", "votes", "notify", "first-issue"];
            expect(same(names, wanted), `listed: ${names}`);
            const words = $$("#o-steps li").map((li) => li.textContent.trim());
            expect(words.every(Boolean), `an unnamed step: ${words.join("|")}`);
        """,
        sabotage="""$('#o-steps li[data-step="sources"]').hidden = true;""",
    ),
    Check(
        id="first-issue-starts-a-run-no-later-tick-replaces",
        fixture="first-issue",
        path=f"{PAGE}#first-issue",
        act="""
            await loaded();
            await issueRead();
            press("o-start");
            await runEnded();
        """,
        script=f"""
            expect(plan().includes("{ISSUE_DATE} {ISSUE_PERIOD} issue"), `plan: ${{plan()}}`);
            const text = notice().textContent;
            expect(!notice().classList.contains("err"), `an error: ${{text}}`);
            expect(text.includes("the {ISSUE_DATE} {ISSUE_PERIOD} issue, is published."),
              `notice: ${{text}}`);
            expect(!visible($("#o-start")), "Start still shows once the issue is out");
            expect(visible($("#o-finish")), "no way out once the issue is out");
        """,
        sabotage_preload=RUN_EVENING,
        receipt=_runs(ISSUE_RUN, settings=ON),
    ),
    Check(
        id="first-issue-says-where-to-read-it",
        fixture="first-issue",
        path=f"{PAGE}#first-issue",
        act="""
            await loaded();
            await issueRead();
            press("o-start");
            await runEnded();
        """,
        script=f"""
            const links = $$("#o-result a").map((a) => [a.textContent, a.getAttribute("href")]);
            const wanted = [["Read it", "{DIGEST_URL}"], ["archive", "{ARCHIVE_URL}"]];
            expect(same(links, wanted), `links: ${{JSON.stringify(links)}}`);
        """,
        sabotage="""$("#o-result a").removeAttribute("href");""",
        receipt=_runs(ISSUE_RUN, settings=ON),
    ),
    Check(
        id="first-issue-shows-the-run-while-it-is-out",
        fixture="first-issue-slow",
        path=f"{PAGE}#first-issue",
        act="""
            await loaded();
            await issueRead();
            press("o-start");
            await waitFor(() => notice().textContent.startsWith("Building"), "the wait");
        """,
        script=f"""
            const text = notice().textContent;
            expect(visible(notice()), "nothing says the run is out");
            expect(notice().getAttribute("aria-busy") === "true", "the wait is not busy");
            expect(text.includes("Building the {ISSUE_DATE} {ISSUE_PERIOD} issue"),
              `notice: ${{text}}`);
            expect(!visible($("#o-start")), "Start shows while the run is out");
            expect(!visible($("#o-back")) && !visible($("#o-skip")),
              "Back or Skip shows while the run is out");
            expect(visible($("#o-finish")), "no way out while the run is out");
        """,
        sabotage="""notice().removeAttribute("aria-busy");""",
        receipt=_runs(ISSUE_RUN, settings=ON),
    ),
    Check(
        id="first-issue-says-why-its-run-failed",
        fixture="first-issue-fails",
        path=f"{PAGE}#first-issue",
        act="""
            await loaded();
            await issueRead();
            press("o-start");
            await runEnded();
        """,
        script=f"""
            const text = notice().textContent;
            expect(notice().classList.contains("err"), `not an error: ${{text}}`);
            expect(text.includes("The run stopped with an error."), `notice: ${{text}}`);
            expect(text.includes({json.dumps(FAILURE)}), `notice: ${{text}}`);
            const again = $("#o-start");
            expect(visible(again) && again.textContent === "Try again",
              `no Try again: ${{again.textContent}}`);
            expect(visible($("#o-finish")), "no way out after the failure");
        """,
        sabotage="""$("#o-start").hidden = true;""",
        receipt=_runs(ISSUE_RUN, settings=ON),
    ),
    Check(
        id="first-issue-tries-again-after-a-failure",
        fixture="first-issue-fails",
        path=f"{PAGE}#first-issue",
        act="""
            await loaded();
            await issueRead();
            press("o-start");
            await runEnded();
        """,
        # After the act, so the failed run's Try again is the one swapped.
        sabotage=dead("o-start"),
        gestures=({"press": "#o-start", "pointer": "mouse"}, {"release": True}),
        script="""
            await waitFor(() => notice().textContent.includes("published"), "the retry",
              RUN_READ_MS);
            expect(!notice().classList.contains("err"), `an error: ${notice().textContent}`);
        """.replace("RUN_READ_MS", str(RUN_READ_MS)),
        receipt=_runs(ISSUE_RUN, ISSUE_RUN, settings=ON),
    ),
    Check(
        id="first-issue-skipped-starts-no-run",
        fixture="first-issue",
        path=f"{PAGE}#first-issue",
        preload=STAY,
        act="""
            await loaded();
            await issueRead();
            await waitFor(() => visible($("#o-skip")) && visible($("#o-start")), "Skip and Start");
        """,
        gestures=({"press": "#o-skip", "pointer": "mouse"}, {"release": True}),
        script="""
            await waitFor(() => window.__left, "the way out");
            expect(window.__left === "/settings?from=onboarding", `left for ${window.__left}`);
            // Long enough for a run the press started to reach the server.
            await sleep(500);
        """,
        sabotage_preload=SKIP_STARTS,
        receipt=_runs(settings=ON),
    ),
    Check(
        id="first-issue-before-the-morning-takes-yesterdays-evening",
        fixture="first-issue-early",
        path=f"{PAGE}#first-issue",
        act="""
            await loaded();
            await issueRead();
            press("o-start");
            await runEnded();
        """,
        script=f"""
            const text = notice().textContent;
            expect(plan().includes("{YESTERDAY} evening issue now, dated yesterday"),
              `plan: ${{plan()}}`);
            expect(plan().includes("08:00 and 20:00 Asia/Taipei"), `plan: ${{plan()}}`);
            expect(!notice().classList.contains("err"), `an error: ${{text}}`);
            expect(text.includes("the {YESTERDAY} evening issue, is published"),
              `notice: ${{text}}`);
            const read = $("#o-result a").getAttribute("href");
            expect(read === "https://cyris.example/{YESTERDAY}-evening", `Read it: ${{read}}`);
        """,
        # The run is dated today instead: the 20:00 tick would store over it.
        sabotage_preload=RUN_TODAY,
        receipt=_runs(EARLY_RUN, settings=ON),
    ),
    Check(
        id="first-issue-turns-on-vote-similarity-with-no-vote",
        fixture="first-issue",
        path=f"{PAGE}#first-issue",
        act="""
            await loaded();
            await issueRead();
            await voteSimilarity;
        """,
        script="""
            const line = $("#o-vote-similarity");
            expect(!visible(line), `refused: ${line.textContent}`);
        """,
        sabotage_preload=rewrite_post("/api/settings/vote-similarity", "body.enabled = false;"),
        receipt=_calls(*ON),
    ),
    Check(
        id="first-issue-starts-though-vote-similarity-is-refused",
        fixture="first-issue",
        path=f"{PAGE}#first-issue",
        preload=rewrite_post("/api/settings/vote-similarity", 'body.max_seeds = "many";'),
        act="""
            await loaded();
            await issueRead();
            await waitFor(() => visible($("#o-vote-similarity")), "the refusal");
            press("o-start");
            await runEnded();
        """,
        script=f"""
            const line = $("#o-vote-similarity").textContent;
            expect(line.startsWith("Vote similarity is still off"), `line: ${{line}}`);
            expect(line.includes("under Model in Settings"), `line: ${{line}}`);
            const text = notice().textContent;
            expect(text.includes("the {ISSUE_DATE} {ISSUE_PERIOD} issue, is published."),
              `notice: ${{text}}`);
        """,
        sabotage="""$("#o-vote-similarity").hidden = true;""",
        receipt=_runs(ISSUE_RUN),
    ),
    Check(
        id="first-issue-with-no-source-says-so",
        fixture="empty",
        path=f"{PAGE}#first-issue",
        act="""
            await loaded();
            await issueRead();
        """,
        script="""
            expect(visible($("#o-issue-plan")), "nothing says there is no source");
            expect(plan().includes("no sources yet"), `plan: ${plan()}`);
            expect(!visible($("#o-start")), "Start shows with no source to read");
        """,
        sabotage="""$("#o-issue-plan").hidden = true;""",
        receipt=_runs(),
    ),
    Check(
        id="own-feed-joins-your-feeds-checked",
        fixture="empty",
        path=f"{PAGE}#sources",
        act=f"""
            await loaded();
            await feedsDrawn();
            {_feeds_of(ONE_TOPIC)}
            setValue($("#o-own-url"), {json.dumps(OWN_HOME)});
        """,
        # After the act: Add is a copy, which carries none of its listeners.
        sabotage=dead("o-own-add"),
        gestures=({"press": "#o-own-add", "pointer": "mouse"}, {"release": True}),
        script=f"""
            await waitFor(() => ownRows().length, "the own feed's row");
            const row = ownRows()[0];
            const name = $(".name", row).textContent;
            expect(name === {json.dumps(OWN_NAME)}, `named ${{name}}`);
            expect($("input", row).checked, "the own feed starts unchecked");
            const legend = $("legend", row.closest("fieldset")).textContent;
            expect(legend === "Your feeds", `it sits under ${{legend}}`);
            expect(picked() === "1 feed picked.", `count: ${{picked()}}`);
            const text = ownNotice().textContent;
            expect(!ownNotice().classList.contains("err"), `an error: ${{text}}`);
            expect(!$("#o-own-url").value, "the address is still in the field");
        """,
        receipt=_both_untouched,
    ),
    Check(
        id="own-feed-needs-no-topic",
        fixture="empty",
        path=f"{PAGE}#sources",
        act=f"""
            await loaded();
            await feedsDrawn();
            press("o-next");
            await waitFor(() => visible($("#o-own-url")), "the feeds screen");
            await addOwn({json.dumps(OWN_HOME)});
        """,
        script=f"""
            expect(location.hash === "#sources/feeds", `on ${{location.hash}}`);
            expect(!shownFeeds().length, `listed feeds with no topic: ${{shownFeeds().join("|")}}`);
            const names = ownRows().map((row) => $(".name", row).textContent);
            expect(same(names, [{json.dumps(OWN_NAME)}]), `own feeds: ${{names.join("|")}}`);
        """,
        # After the act: the own feed's row is gone, as if the screen had turned it away.
        sabotage='$("#o-own").replaceChildren();',
        receipt=_both_untouched,
    ),
    Check(
        id="own-feed-is-posted-by-next",
        fixture="empty",
        path=f"{PAGE}#sources",
        act=f"""
            await loaded();
            await feedsDrawn();
            {_feeds_of(ONE_TOPIC)}
            await addOwn({json.dumps(OWN_HOME)});
            press("o-next");
            await onStep("votes");
            await waitFor(settled, "the result notice");
        """,
        script=f"""
            const text = notice().textContent;
            expect(!notice().classList.contains("err"), `an error: ${{text}}`);
            expect(text.includes("Added 1 feed: {OWN_NAME}."), `notice: ${{text}}`);
        """,
        # The home page pasted is posted in place of the feed it links.
        sabotage_preload=rewrite_post("/api/sources", f"body.url = {json.dumps(OWN_HOME)};"),
        receipt=_own_posted,
    ),
    Check(
        id="own-feed-pressed-next-during-the-check-is-posted",
        fixture="own-feed-slow",
        path=f"{PAGE}#sources",
        act=f"""
            await loaded();
            await feedsDrawn();
            {_feeds_of(ONE_TOPIC)}
            setValue($("#o-own-url"), {json.dumps(OWN_HOME)});
            press("o-own-add");
            press("o-next");
            // Two slow fetches, the home page and its feed, before Next can save.
            await waitFor(() => same(shownSteps(), ["votes"]), "the votes step", 15000);
            await waitFor(settled, "the result notice");
        """,
        script=f"""
            const text = notice().textContent;
            expect(text.includes("Added 1 feed: {OWN_NAME}."), `notice: ${{text}}`);
        """,
        sabotage_preload=rewrite_post("/api/sources", f"body.url = {json.dumps(OWN_HOME)};"),
        receipt=_own_posted,
    ),
    Check(
        id="own-feed-refused-says-why-and-adds-nothing",
        fixture="empty",
        path=f"{PAGE}#sources",
        act=f"""
            await loaded();
            await feedsDrawn();
            {_feeds_of(ONE_TOPIC)}
            await addOwn({json.dumps(BARE_HOME)});
        """,
        script=f"""
            const text = ownNotice().textContent;
            expect(ownNotice().classList.contains("err"), `not an error: ${{text}}`);
            expect(text === {json.dumps(NO_FEED_LINK)}, `notice: ${{text}}`);
            expect(!ownRows().length, `${{ownRows().length}} own feeds listed`);
            expect(picked() === "0 feeds picked.", `count: ${{picked()}}`);
            const invalid = $("#o-own-url").getAttribute("aria-invalid") === "true";
            expect(invalid, "the field is not marked");
        """,
        sabotage_preload=CHECK_PASSES,
        receipt=_both_untouched,
    ),
    Check(
        id="own-feed-shows-the-check-while-it-is-out",
        fixture="own-feed-slow",
        path=f"{PAGE}#sources",
        act=f"""
            await loaded();
            await feedsDrawn();
            {_feeds_of(ONE_TOPIC)}
            setValue($("#o-own-url"), {json.dumps(OWN_HOME)});
            press("o-own-add");
            await waitFor(() => visible(ownNotice()), "the check");
        """,
        script=f"""
            const text = ownNotice().textContent;
            expect(ownNotice().getAttribute("aria-busy") === "true", "the check is not busy");
            expect(text === "Checking {OWN_HOME}…", `notice: ${{text}}`);
            expect($("#o-own-add").disabled, "Add is live while the check is out");
        """,
        sabotage="""ownNotice().removeAttribute("aria-busy");""",
        receipt=_both_untouched,
    ),
    Check(
        id="own-feed-already-listed-checks-its-row",
        fixture="empty",
        path=f"{PAGE}#sources",
        act=f"""
            await loaded();
            await feedsDrawn();
            {_feeds_of(ONE_TOPIC)}
            await addOwn({json.dumps(LISTED["url"])});
        """,
        script=f"""
            const row = rowNamed({json.dumps(LISTED["name"], ensure_ascii=False)});
            expect(row && visible(row), "the listed feed is not shown");
            expect($("input", row).checked, "the listed feed is not checked");
            const legend = $("legend", row.closest("fieldset")).textContent;
            expect(legend === {json.dumps(KEPT)}, `it sits under ${{legend}}`);
            expect(!ownRows().length, "it was added a second time, as an own feed");
            expect(picked() === "1 feed picked.", `count: ${{picked()}}`);
        """,
        # After the act: the row is unchecked again, and leaves with its pick.
        sabotage=f"""pick({json.dumps(LISTED["name"], ensure_ascii=False)});""",
        receipt=_both_untouched,
    ),
    Check(
        id="sources-step-shows-no-feed-before-a-topic",
        fixture="empty",
        path=PAGE,
        act="""
            await loaded();
            await skipTo("sources");
            await feedsDrawn();
        """,
        script=f"""
            const topics = topicRows().filter(visible).map((row) => $(".name", row).textContent);
            const wanted = {json.dumps([topic["label"] for topic in TOPICS])};
            expect(same(topics, wanted), `topics: ${{topics.join("|")}}`);
            const chosen = topicRows().filter((row) => row.getAttribute("aria-pressed") === "true");
            expect(chosen.length === 0, `${{chosen.length}} topics start chosen`);
            expect(!shownFeeds().length, `feeds before a topic: ${{shownFeeds().join("|")}}`);
            expect(visible($("#o-feeds-hint")), "nothing says to pick a topic");
            // The feeds screen also takes the reader's own feed, which needs no topic.
            expect(!$("#o-next").disabled, "Next waits for a topic");
        """,
        sabotage=_choose(ONE_TOPIC),
        receipt=_both_untouched,
    ),
    Check(
        id="sources-step-one-topic-lists-its-feeds",
        fixture="empty",
        path=f"{PAGE}#sources",
        act=f"""
            await loaded();
            await feedsDrawn();
            {_feeds_of(ONE_TOPIC)}
        """,
        script=f"""
            const wanted = {json.dumps(shown_feeds([ONE_TOPIC]), ensure_ascii=False)};
            expect(same(shownFeeds(), wanted), `listed: ${{shownFeeds().join("|")}}`);
            expect(same(groupsShown(), [{json.dumps(LABELS[ONE_TOPIC])}]),
              `groups: ${{groupsShown().join("|")}}`);
            expect(!visible($("#o-feeds-hint")), "the pick-a-topic line outlived the choice");
        """,
        # After the act: a second topic is chosen, and its own feeds show too.
        sabotage=_choose(OTHER_TOPIC),
        receipt=_both_untouched,
    ),
    Check(
        id="sources-step-lists-feeds-when-storage-refuses-the-topics",
        fixture="empty",
        path=f"{PAGE}#sources",
        preload=NO_STORAGE,
        act=f"""
            await loaded();
            await feedsDrawn();
            {_feeds_of(ONE_TOPIC)}
        """,
        script=f"""
            const wanted = {json.dumps(shown_feeds([ONE_TOPIC]), ensure_ascii=False)};
            expect(same(shownFeeds(), wanted), `listed: ${{shownFeeds().join("|")}}`);
        """,
        # After the act: the redraw a throwing save would have stopped is undone.
        sabotage='$("#o-feeds").replaceChildren();',
        receipt=_both_untouched,
    ),
    Check(
        id="sources-step-two-topics-list-a-shared-feed-once",
        fixture="empty",
        path=f"{PAGE}#sources",
        act=f"""
            await loaded();
            await feedsDrawn();
            {_feeds_of(*TWO_TOPICS)}
        """,
        script=f"""
            const wanted = {json.dumps(shown_feeds(TWO_TOPICS), ensure_ascii=False)};
            expect(same(shownFeeds(), wanted), `listed: ${{shownFeeds().join("|")}}`);
            const shared = {json.dumps(SHARED, ensure_ascii=False)};
            const times = shownFeeds().filter((name) => name === shared).length;
            expect(times === 1, `${{shared}} shows ${{times}} times`);
            const groups = {json.dumps([LABELS[topic] for topic in TWO_TOPICS])};
            expect(same(groupsShown(), groups), `groups: ${{groupsShown().join("|")}}`);
        """,
        # The shared feed shows under its second topic as well.
        sabotage=f"""
            $$("#o-feeds fieldset").pop()
              .append(rowNamed({json.dumps(SHARED, ensure_ascii=False)}).cloneNode(true));
        """,
        receipt=_both_untouched,
    ),
    Check(
        id="sources-step-keeps-a-pick-when-its-topic-is-dropped",
        fixture="empty",
        path=f"{PAGE}#sources",
        act=f"""
            await loaded();
            await feedsDrawn();
            {_pick(KEPT_PICK)}
            await toTopics();
            unchoose({json.dumps(LABELS[KEPT_PICK["topics"][0]])});
            // No topic is chosen now, yet Next opens the feeds: a pick is still checked.
            await toFeeds();
        """,
        script=f"""
            const row = rowNamed({json.dumps(KEPT_PICK["name"], ensure_ascii=False)});
            expect(row && visible(row), "the pick left the page with its topic");
            expect($("input", row).checked, "the pick was unchecked with its topic");
            const legend = $("legend", row.closest("fieldset")).textContent;
            expect(legend === {json.dumps(KEPT)}, `it sits under ${{legend}}`);
            expect(same(shownFeeds(), [{json.dumps(KEPT_PICK["name"], ensure_ascii=False)}]),
              `listed: ${{shownFeeds().join("|")}}`);
        """,
        sabotage=f"""rowNamed({json.dumps(KEPT_PICK["name"], ensure_ascii=False)}).remove();""",
        receipt=_both_untouched,
    ),
    Check(
        id="sources-step-a-reload-keeps-the-topics",
        fixture="empty",
        # The same tab twice: the second load is the reload, with this tab's
        # sessionStorage, on the feeds screen the first one left open. Only the
        # first one chooses.
        path=(f"{PAGE}#sources", f"{PAGE}#sources/feeds"),
        act=f"""
            await loaded();
            await feedsDrawn();
            if (!sessionStorage.getItem("probe-chose")) {{
              sessionStorage.setItem("probe-chose", "1");
              {_feeds_of(ONE_TOPIC)}
            }}
        """,
        script=f"""
            expect(feedsScreen(), "the reload left the feeds screen");
            expect(topicChosen({json.dumps(LABELS[ONE_TOPIC])}), "the topic is not chosen");
            const wanted = {json.dumps(shown_feeds([ONE_TOPIC]), ensure_ascii=False)};
            expect(same(shownFeeds(), wanted), `listed: ${{shownFeeds().join("|")}}`);
        """,
        # After the first load's choice: what the reload reads is gone.
        sabotage=f"""sessionStorage.removeItem({json.dumps(TOPICS_KEY)});""",
        receipt=_both_untouched,
    ),
    Check(
        id="sources-step-topics-are-toggle-buttons",
        fixture="empty",
        path=f"{PAGE}#sources",
        act=f"""
            await loaded();
            await feedsDrawn();
            {_choose(ONE_TOPIC)}
        """,
        script=f"""
            const group = $("#o-topics");
            expect(group.getAttribute("role") === "group", "the tiles are not a group");
            const name = document.getElementById(group.getAttribute("aria-labelledby"));
            expect(name && name.textContent === "Topics", "the group is not named Topics");
            const odd = topicRows().filter((tile) => tile.tagName !== "BUTTON"
              || tile.type !== "button" || !tile.hasAttribute("aria-pressed"));
            expect(!odd.length, `${{odd.length}} tiles are not toggle buttons`);
            const pressed = topicRows()
              .filter((tile) => tile.getAttribute("aria-pressed") === "true")
              .map((tile) => $(".name", tile).textContent);
            expect(same(pressed, [{json.dumps(LABELS[ONE_TOPIC])}]), `pressed: ${{pressed}}`);
            expect(!$("#o-topics input"), "a topic is still a checkbox");
        """,
        # After the act: the chosen tile no longer says it is pressed.
        sabotage=f"""topicTile({json.dumps(LABELS[ONE_TOPIC])}).removeAttribute("aria-pressed");""",
        receipt=_both_untouched,
    ),
    Check(
        id="sources-step-a-pressed-topic-shows-it",
        fixture="empty",
        path=f"{PAGE}#sources",
        act=f"""
            await loaded();
            await feedsDrawn();
            {_choose(ONE_TOPIC)}
            // The colours transition; read them once that has settled.
            await sleep(400);
            const swatch = document.createElement("i");
            swatch.style.color = "var(--accent)";
            document.body.append(swatch);
            ctx.accent = getComputedStyle(swatch).color;
            swatch.remove();
        """,
        script=f"""
            const on = topicTile({json.dumps(LABELS[ONE_TOPIC])});
            const off = topicRows().find((tile) => tile !== on);
            const border = getComputedStyle(on).borderTopColor;
            expect(border === ctx.accent, `pressed border: ${{border}}, accent: ${{ctx.accent}}`);
            // The mark, which says it without colour: filled when pressed, empty when not.
            const mark = (tile) => getComputedStyle(tile, "::after").backgroundColor;
            expect(mark(on) === ctx.accent, `pressed mark: ${{mark(on)}}`);
            expect(mark(off) !== ctx.accent, `unpressed mark: ${{mark(off)}}`);
        """,
        sabotage=restyle(
            '.tile[aria-pressed="true"], .tile[aria-pressed="true"]::after'
            " { border-color: var(--border-strong) !important; background: none !important;"
            # The tile's own transition would still be fading when the script reads.
            " transition: none !important; }"
        ),
        receipt=_both_untouched,
    ),
    Check(
        id="sources-step-a-topic-answers-the-keyboard",
        fixture="empty",
        path=f"{PAGE}#sources",
        focused=True,
        act="""
            await loaded();
            await feedsDrawn();
            topicRows()[0].focus();
        """,
        # After the act: the focused tile is a copy, which carries none of its listeners.
        sabotage="""
            const tile = topicRows()[0], copy = tile.cloneNode(true);
            tile.replaceWith(copy);
            copy.focus();
        """,
        gestures=({"key": " "},),
        script=f"""
            await waitFor(() => topicChosen({json.dumps(TOPICS[0]["label"])}), "the topic chosen");
            expect(!$("#o-next").disabled, "Next still waits for a topic");
        """,
        receipt=_both_untouched,
    ),
    Check(
        id="sources-step-next-opens-the-feeds-screen",
        fixture="empty",
        path=f"{PAGE}#sources",
        act=f"""
            await loaded();
            await feedsDrawn();
            {_choose(ONE_TOPIC)}
        """,
        sabotage=dead("o-next"),
        gestures=({"press": "#o-next", "pointer": "mouse"}, {"release": True}),
        script=f"""
            await waitFor(feedsScreen, "the feeds screen");
            expect(!topicsScreen(), "the topics screen is still shown");
            expect(location.hash === "#sources/feeds", `hash: ${{location.hash}}`);
            expect(same(marked(), ["sources"]), `marked: ${{marked()}}`);
            expect(progress() === "Step 3 of 6", `progress: ${{progress()}}`);
            const wanted = {json.dumps(shown_feeds([ONE_TOPIC]), ensure_ascii=False)};
            expect(same(shownFeeds(), wanted), `listed: ${{shownFeeds().join("|")}}`);
        """,
        receipt=_both_untouched,
    ),
    Check(
        id="sources-step-back-returns-to-the-topics",
        fixture="empty",
        path=f"{PAGE}#sources",
        act=f"""
            await loaded();
            await feedsDrawn();
            {_pick(KEPT_PICK)}
        """,
        sabotage=dead("o-back"),
        gestures=({"press": "#o-back", "pointer": "mouse"}, {"release": True}),
        script=f"""
            await waitFor(topicsScreen, "the topics screen");
            expect(!feedsScreen(), "the feeds screen is still shown");
            expect(same(marked(), ["sources"]), `marked: ${{marked()}}`);
            expect(topicChosen({json.dumps(LABELS[KEPT_PICK["topics"][0]])}),
              "the topic was unchosen");
            const row = rowNamed({json.dumps(KEPT_PICK["name"], ensure_ascii=False)});
            expect($("input", row).checked, "the pick was unchecked");
        """,
        receipt=_both_untouched,
    ),
    Check(
        id="sources-step-skip-on-the-topics-screen-leaves-the-step",
        fixture="empty",
        path=f"{PAGE}#sources",
        act=f"""
            await loaded();
            await feedsDrawn();
            {_choose(ONE_TOPIC)}
        """,
        sabotage=dead("o-skip"),
        gestures=({"press": "#o-skip", "pointer": "mouse"}, {"release": True}),
        script="""
            await onStep("votes");
            expect(!visible(notice()), `a notice: ${notice().textContent}`);
        """,
        receipt=_both_untouched,
    ),
    Check(
        id="sources-step-counts-the-picked-feeds",
        fixture="empty",
        path=f"{PAGE}#sources",
        act=f"""
            await loaded();
            await feedsDrawn();
            {_pick(*PICKS)}
        """,
        script="""
            const line = $("#o-picked");
            expect(visible(line), "the count is hidden");
            expect(line.getAttribute("aria-live") === "polite", "the count is not announced");
            expect(picked() === "2 feeds picked.", `count: ${picked()}`);
        """,
        # After the act: one pick is taken back, and the count must follow.
        sabotage=f"""pick({json.dumps(PICKS[0]["name"], ensure_ascii=False)});""",
        receipt=_both_untouched,
    ),
    Check(
        id="sources-step-warns-past-twelve-picks",
        fixture="empty",
        path=f"{PAGE}#sources",
        act=f"""
            await loaded();
            await feedsDrawn();
            {_feeds_of(*LABELS)}
            feedRows().slice(0, {SLOW_PICKS + 1}).forEach((row) => $("input", row).click());
        """,
        script=f"""
            const text = picked();
            expect(text.startsWith("{SLOW_PICKS + 1} feeds picked."), `count: ${{text}}`);
            expect(text.includes("about a minute") && text.includes("longer"), `count: ${{text}}`);
            expect(!$("#o-next").disabled, "the warning blocks Next");
        """,
        # After the act: back to the most the poll fetches within a minute.
        sabotage="""$("input", feedRows()[0]).click();""",
        receipt=_both_untouched,
    ),
    Check(
        id="sources-step-twelve-picks-carry-no-warning",
        fixture="empty",
        path=f"{PAGE}#sources",
        act=f"""
            await loaded();
            await feedsDrawn();
            {_feeds_of(*LABELS)}
            feedRows().slice(0, {SLOW_PICKS}).forEach((row) => $("input", row).click());
        """,
        script=f"""expect(picked() === "{SLOW_PICKS} feeds picked.", `count: ${{picked()}}`);""",
        # After the act: one more pick, past what the poll fetches within a minute.
        sabotage=f"""$("input", feedRows()[{SLOW_PICKS}]).click();""",
        receipt=_both_untouched,
    ),
    Check(
        id="sources-step-posts-no-topics",
        fixture="empty",
        path=f"{PAGE}#sources",
        act=f"""
            await loaded();
            await feedsDrawn();
            {_pick(*PICKS)}
            press("o-next");
            await onStep("votes");
            await waitFor(settled, "the result notice");
        """,
        script="""
            expect(!notice().classList.contains("err"), `an error: ${notice().textContent}`);
        """,
        sabotage_preload=rewrite_post(
            "/api/sources", f"body.topics = {json.dumps(PICKS[0]['topics'])};"
        ),
        receipt=_posted(*PICKS),
    ),
    Check(
        id="sources-step-lists-every-entry",
        fixture="empty",
        path=PAGE,
        act=f"""
            await loaded();
            await skipTo("sources");
            await feedsDrawn();
            {_feeds_of(*LABELS)}
        """,
        script=f"""
            const names = shownFeeds();
            const wanted = {json.dumps(shown_feeds(list(LABELS)), ensure_ascii=False)};
            expect(same(names, wanted), `listed: ${{names.join("|")}}`);
            expect(new Set(names).size === {len(FEEDS)}, `${{new Set(names).size}} feeds listed`);
            const picked = feedRows().filter((row) => $("input", row).checked).length;
            expect(picked === 0, `${{picked}} feeds start picked`);
            expect(!visible($("#o-feeds-had")), "the has-sources line shows over the list");
        """,
        sabotage="""feedRows()[3].remove();""",
        receipt=_both_untouched,
    ),
    Check(
        id="sources-step-stores-the-picked-entries",
        fixture="empty",
        path=f"{PAGE}#sources",
        act=f"""
            await loaded();
            await feedsDrawn();
            {_pick(*PICKS)}
            press("o-next");
            await onStep("votes");
            await waitFor(settled, "the result notice");
        """,
        script=f"""
            const text = notice().textContent;
            expect(!notice().classList.contains("err"), `an error: ${{text}}`);
            const added = {
            json.dumps("Added 2 feeds: " + ", ".join(p["name"] for p in PICKS), ensure_ascii=False)
        };
            expect(text.includes(added), `notice: ${{text}}`);
        """,
        sabotage_preload=rewrite_post("/api/sources", "body.tags = body.tags.slice(1);"),
        receipt=_sources(*PICKS),
    ),
    Check(
        id="sources-step-stores-the-listed-lane",
        fixture="empty",
        path=f"{PAGE}#sources",
        act=f"""
            await loaded();
            await feedsDrawn();
            {_pick(PICKS[0])}
            press("o-next");
            await onStep("votes");
        """,
        script="""await waitFor(settled, "the result notice");""",
        sabotage_preload=rewrite_post("/api/sources", 'body.lane = "read";'),
        receipt=_sources(PICKS[0]),
    ),
    Check(
        id="sources-step-skipped-writes-no-source",
        fixture="empty",
        path=f"{PAGE}#sources",
        act=f"""
            await loaded();
            await feedsDrawn();
            {_pick(*PICKS)}
            press("o-skip");
            await onStep("votes");
        """,
        script="""
            expect(same(marked(), ["votes"]), `marked: ${marked()}`);
            expect(!visible(notice()), `a notice: ${notice().textContent}`);
        """,
        sabotage_preload=SKIP_SAVES,
        receipt=_both_untouched,
    ),
    Check(
        id="sources-step-with-nothing-picked-writes-nothing",
        fixture="empty",
        path=f"{PAGE}#sources",
        act=f"""
            await loaded();
            await feedsDrawn();
            {_feeds_of(ONE_TOPIC)}
        """,
        # After the act, so the pick is made on the drawn list.
        sabotage=_pick(PICKS[0]),
        gestures=({"press": "#o-next", "pointer": "mouse"}, {"release": True}),
        script="""await onStep("votes");""",
        receipt=_both_untouched,
    ),
    Check(
        id="sources-step-names-a-feed-it-could-not-add",
        fixture="refuses-one",
        path=f"{PAGE}#sources",
        act=f"""
            await loaded();
            await feedsDrawn();
            {_pick(TAKEN, REFUSED)}
            press("o-next");
            await waitFor(settled, "the refusal");
        """,
        script=f"""
            const text = notice().textContent;
            expect(notice().classList.contains("err"), `not an error: ${{text}}`);
            expect(text.includes("Added 1 of 2 feeds"), `notice: ${{text}}`);
            const refused = {json.dumps(f"{REFUSED['name']}: {REFUSAL}", ensure_ascii=False)};
            expect(text.includes(refused), `notice: ${{text}}`);
            expect(text.includes("Press Next to try again"), `notice: ${{text}}`);
            expect(same(shownSteps(), ["sources"]), `shown: ${{shownSteps()}}`);
            const failed = feedRows().filter((row) => $('[aria-invalid="true"]', row))
              .map((row) => $(".name", row).textContent);
            expect(same(failed, [{json.dumps(REFUSED["name"], ensure_ascii=False)}]),
              `marked: ${{failed}}`);
        """,
        # The notice names the wrong feed: the one that was added.
        sabotage=f"notice().textContent = {json.dumps(MISNAMED)};",
        receipt=_sources(TAKEN),
    ),
    Check(
        id="sources-step-retries-only-what-it-could-not-add",
        fixture="refuses-one",
        path=f"{PAGE}#sources",
        act=f"""
            await loaded();
            await feedsDrawn();
            {_pick(TAKEN, REFUSED)}
            press("o-next");
            await waitFor(settled, "the refusal");
        """,
        # After the first try: the added feed is offered again, so a retry
        # that reads the rows sends it twice.
        sabotage=f"""$("input", rowNamed({json.dumps(TAKEN["name"])})).disabled = false;""",
        gestures=({"press": "#o-next", "pointer": "mouse"}, {"release": True}),
        script=f"""
            await onStep("votes");
            await waitFor(settled, "the result notice");
            const text = notice().textContent;
            const added = {json.dumps(f"Added 1 feed: {REFUSED['name']}", ensure_ascii=False)};
            expect(text.includes(added), `notice: ${{text}}`);
        """,
        receipt=_sources(TAKEN, REFUSED),
    ),
    Check(
        id="votes-step-deals-the-drawn-cards",
        fixture="cards",
        path=f"{PAGE}#votes",
        act="""
            await loaded();
            await cardsDrawn();
        """,
        script=f"""
            const titles = cardRows().filter(visible).map(cardTitle);
            const wanted = {json.dumps([card.title for card in CARDS], ensure_ascii=False)};
            expect(same(titles, wanted), `cards: ${{titles.join("|")}}`);
            const links = cardRows().map((row) => $(".title", row).getAttribute("href"));
            const urls = {json.dumps([card.url for card in CARDS])};
            expect(same(links, urls), `links: ${{links.join("|")}}`);
            for (const row of cardRows()) {{
              const answers = $$("button", row)
                .map((b) => [b.textContent, b.getAttribute("aria-pressed")]);
              expect(same(answers, [["Interested", "false"], ["Not interested", "false"]]),
                `a card's answers: ${{JSON.stringify(answers)}}`);
            }}
            expect(!visible($("#o-cards-loading")), "the loading line outlived the cards");
        """,
        sabotage="""cardRows()[1].remove();""",
        receipt=_NOTHING_VOTED,
    ),
    Check(
        id="votes-step-names-a-feed-with-no-article",
        fixture="cards",
        path=f"{PAGE}#votes",
        act="""
            await loaded();
            await cardsDrawn();
        """,
        script=f"""
            const hint = $("#o-cards-empty");
            expect(visible(hint), "no line names the feed that gave no article");
            const quiet = {json.dumps(QUIET["name"], ensure_ascii=False)};
            expect(hint.textContent.includes(`this feed: ${{quiet}}.`),
              `it says: ${{hint.textContent}}`);
            for (const name of {json.dumps([p["name"] for p in PICKS], ensure_ascii=False)}) {{
              expect(!hint.textContent.includes(name), `it names ${{name}}, which gave cards`);
            }}
        """,
        sabotage="""$("#o-cards-empty").hidden = true;""",
        receipt=_NOTHING_VOTED,
    ),
    Check(
        id="votes-step-shows-the-wait",
        fixture="cards-slow",
        path=f"{PAGE}#votes",
        act="""
            await loaded();
            await waitFor(() => visible($("#o-cards-loading")), "the loading line");
        """,
        script="""
            const line = $("#o-cards-loading");
            expect(visible(line), "nothing says the cards are on their way");
            expect(line.getAttribute("aria-busy") === "true", "the loading line is not busy");
            expect(line.textContent.includes("a minute or two"), `it says: ${line.textContent}`);
            expect(!cardRows().length, "cards before the read answered");
        """,
        sabotage="""$("#o-cards-loading").hidden = true;""",
        receipt=_NOTHING_VOTED,
    ),
    Check(
        id="votes-step-deals-the-feeds-just-added",
        fixture="empty",
        path=f"{PAGE}#sources",
        act=f"""
            await loaded();
            await feedsDrawn();
            {_pick(*PICKS)}
            press("o-next");
            await onStep("votes");
            await cardsDrawn();
        """,
        script=f"""
            const sources = [...new Set(cardRows().map(cardSource))];
            const wanted = {json.dumps([p["name"] for p in PICKS], ensure_ascii=False)};
            expect(same(sources, wanted), `cards from: ${{sources.join("|")}}`);
        """,
        sabotage=f"""
            cardRows().filter((row) => cardSource(row) === {json.dumps(PICKS[1]["name"])})
              .forEach((row) => row.remove());
        """,
        receipt=_sources(*PICKS),
    ),
    Check(
        id="votes-step-records-the-marked-cards",
        fixture="cards",
        path=f"{PAGE}#votes",
        act="""
            await loaded();
            await cardsDrawn();
            vote(0, "up");
            vote(1, "down");
            press("o-next");
            await onStep("notify");
            await waitFor(settled, "the result notice");
        """,
        script="""
            const text = notice().textContent;
            expect(!notice().classList.contains("err"), `an error: ${text}`);
            expect(text.includes("Recorded 2 votes: 1 interested, 1 not interested."),
              `notice: ${text}`);
        """,
        # Each vote sent as its opposite: the rows say so, whatever the notice counts.
        sabotage_preload=rewrite_post(
            "/api/onboarding/votes",
            'body.votes.forEach((v) => { v.vote = v.vote === "up" ? "down" : "up"; });',
        ),
        receipt=_votes(up=(0,), down=(1,)),
    ),
    Check(
        id="votes-step-keeps-the-votes-when-one-article-is-gone",
        fixture="cards",
        path=f"{PAGE}#votes",
        # The second card's article leaves its feed before Next: the server finds
        # no such URL, as when a feed rotates it out after the cards were dealt.
        preload=GONE_CARD,
        act="""
            await loaded();
            await cardsDrawn();
            vote(0, "up");
            vote(1, "down");
            press("o-next");
            await onStep("notify");
            await waitFor(settled, "the result notice");
        """,
        script="""
            const text = notice().textContent;
            expect(!notice().classList.contains("err"), `an error: ${text}`);
            expect(text.includes("Recorded 1 vote: 1 interested, 0 not interested."),
              `notice: ${text}`);
            expect(text.includes("left its feed"), `notice: ${text}`);
            const state = [...$$(".label", cardRows()[1])].pop().textContent;
            expect(state.startsWith("No longer in the feed"), `the gone card says: ${state}`);
        """,
        sabotage="""
            [...$$(".label", cardRows()[1])].pop().textContent = "Recorded · Not interested";
        """,
        receipt=_votes(up=(0,)),
    ),
    Check(
        id="votes-step-skipped-writes-nothing",
        fixture="cards",
        path=f"{PAGE}#votes",
        act="""
            await loaded();
            await cardsDrawn();
            vote(0, "up");
            vote(1, "down");
            press("o-skip");
            await onStep("notify");
        """,
        script="""
            expect(same(marked(), ["notify"]), `marked: ${marked()}`);
            expect(!visible(notice()), `a notice: ${notice().textContent}`);
        """,
        sabotage_preload=SKIP_SAVES,
        receipt=_NOTHING_VOTED,
    ),
    Check(
        id="votes-step-with-no-vote-writes-nothing",
        fixture="cards",
        path=f"{PAGE}#votes",
        act="""
            await loaded();
            await cardsDrawn();
        """,
        # After the act, so the press marks a card the page dealt.
        sabotage="""vote(0, "down");""",
        gestures=({"press": "#o-next", "pointer": "mouse"}, {"release": True}),
        script="""await onStep("notify");""",
        receipt=_NOTHING_VOTED,
    ),
    Check(
        id="votes-step-a-second-press-takes-the-vote-back",
        fixture="cards",
        path=f"{PAGE}#votes",
        act="""
            await loaded();
            await cardsDrawn();
            vote(0, "up");
            vote(0, "up");
        """,
        # After the act: the card is marked again, so Next has a vote to send.
        sabotage="""vote(0, "up");""",
        gestures=({"press": "#o-next", "pointer": "mouse"}, {"release": True}),
        script="""await onStep("notify");""",
        receipt=_NOTHING_VOTED,
    ),
    Check(
        id="votes-step-with-no-feed-says-so",
        fixture="empty",
        path=f"{PAGE}#votes",
        act="""
            await loaded();
            await cardsDrawn();
        """,
        script="""
            const line = $("#o-cards-none");
            expect(visible(line), "nothing says there is no feed to draw from");
            expect(line.textContent.includes("no feeds to draw from"), line.textContent);
            expect(!cardRows().length, `${cardRows().length} cards`);
        """,
        sabotage="""$("#o-cards-none").hidden = true;""",
        receipt=_NOTHING_VOTED,
    ),
    Check(
        id="next-walks-to-the-next-step",
        fixture="empty",
        path=PAGE,
        act="""
            await loaded();
            press("o-next");
            await onStep("schedule");
        """,
        script="""
            expect(same(marked(), ["schedule"]), `marked: ${marked()}`);
            expect(progress() === "Step 2 of 6", `progress: ${progress()}`);
            expect(location.hash === "#schedule", `hash: ${location.hash}`);
            expect(visible($("#o-back")), "Back is hidden off the first step");
            const done = $$("#o-steps li.done").map((li) => li.dataset.step);
            expect(same(done, ["language"]), `done: ${done}`);
        """,
        sabotage="""location.hash = "#language";""",
        # Nothing was typed, so walking past a step stores nothing.
        receipt=_calls(),
    ),
    Check(
        id="back-returns-to-the-previous-step",
        fixture="empty",
        path=f"{PAGE}#schedule",
        act="""
            await loaded();
            press("o-back");
            await onStep("language");
        """,
        script="""
            expect(progress() === "Step 1 of 6", `progress: ${progress()}`);
            expect(!visible($("#o-back")), "Back shows on the first step");
        """,
        sabotage="""$("#o-progress").textContent = "Step 2 of 6";""",
        receipt=_calls(),
    ),
    Check(
        id="skip-walks-to-the-last-step",
        fixture="first-issue",
        path=PAGE,
        act="""
            await loaded();
            await skipTo("first-issue");
            await issueRead();
            await voteSimilarity;
        """,
        script="""
            expect(progress() === "Step 6 of 6", `progress: ${progress()}`);
            expect(!visible($("#o-next")), "Next still shows on the last step");
            expect(visible($("#o-start")) && visible($("#o-skip")),
              "Start or Skip is hidden on the last step");
            expect(!visible($("#o-finish")), "Finish shows before the run is started or skipped");
        """,
        sabotage="""location.hash = "#notify";""",
        receipt=_calls(*ON),
    ),
    Check(
        id="skip-writes-nothing",
        fixture="empty",
        path=PAGE,
        act=f"""
            await loaded();
            {TYPE_LANGUAGE}
            press("o-skip");
            await onStep("schedule");
        """,
        script="""expect(!visible(notice()), `a notice: ${notice().textContent}`);""",
        sabotage_preload=SKIP_SAVES,
        receipt=_calls(),
    ),
    Check(
        id="language-step-saves-both-values",
        fixture="empty",
        path=PAGE,
        act=f"""
            await loaded();
            {TYPE_LANGUAGE}
            press("o-next");
            await onStep("schedule");
        """,
        script="""
            await waitFor(settled, "the result notice");
            const text = notice().textContent;
            expect(!notice().classList.contains("err"), `an error: ${text}`);
            expect(text.includes("Output language: en"), `notice: ${text}`);
            expect(text.includes("Timezone: Europe/Lisbon"), `notice: ${text}`);
        """,
        sabotage_preload=rewrite_post(
            "/api/settings/values", 'body.values["general.timezone"] = "UTC";'
        ),
        receipt=_calls(LANGUAGE_STORED),
    ),
    Check(
        id="language-step-sends-only-what-changed",
        fixture="empty",
        path=PAGE,
        act=f"""
            await loaded();
            setValue($("#o-timezone"), {json.dumps(NEW_TIMEZONE)});
            press("o-next");
            await onStep("schedule");
            await waitFor(settled, "the result notice");
        """,
        script="""
            const text = notice().textContent;
            expect(!text.includes("Output language"), `notice names the language: ${text}`);
        """,
        sabotage_preload=rewrite_post(
            "/api/settings/values", 'body.values["digest.output_language"] = "en";'
        ),
        receipt=_calls({"general.timezone": NEW_TIMEZONE}),
    ),
    Check(
        id="an-untouched-step-writes-nothing",
        fixture="empty",
        path=PAGE,
        act="await loaded();",
        # After the act, so the value is not one the page loaded with.
        sabotage=f"""setValue($("#o-timezone"), {json.dumps(NEW_TIMEZONE)});""",
        gestures=({"press": "#o-next", "pointer": "mouse"}, {"release": True}),
        script="""await onStep("schedule");""",
        receipt=_calls(),
    ),
    Check(
        id="hours-step-saves-the-schedule",
        fixture="empty",
        path=f"{PAGE}#schedule",
        act=f"""
            await loaded();
            {TYPE_HOURS}
            press("o-next");
            await onStep("sources");
            await waitFor(settled, "the result notice");
        """,
        script="""
            const text = notice().textContent;
            expect(!notice().classList.contains("err"), `an error: ${text}`);
            expect(text.includes("07:00 and 19:00"), `notice: ${text}`);
        """,
        sabotage_preload=rewrite_post("/api/settings/schedule", 'body.times = ["08:00", "20:00"];'),
        receipt=_calls(HOURS_STORED),
    ),
    Check(
        id="hours-step-refuses-an-empty-hour",
        fixture="empty",
        path=f"{PAGE}#schedule",
        act="""
            await loaded();
            setValue($("#o-morning"), "");
            press("o-next");
            await waitFor(settled, "the refusal");
        """,
        script="""
            const text = notice().textContent;
            expect(visible(notice()), "the refusal is not shown");
            expect(notice().classList.contains("err"), `not an error: ${text}`);
            expect(text.includes("whole hour from 0 to 23"), `notice: ${text}`);
            expect(same(shownSteps(), ["schedule"]), `shown: ${shownSteps()}`);
            expect(same(marks(), ["o-morning"]), `marked: ${marks()}`);
        """,
        sabotage="""notice().hidden = true;""",
        # Refused on the page: the server was never asked to store an 00:00 morning.
        receipt=_calls(),
    ),
    Check(
        id="a-refused-value-is-marked-in-its-own-field",
        fixture="empty",
        path=PAGE,
        act=f"""
            await loaded();
            offer($("#o-timezone"), {json.dumps(UNKNOWN_TIMEZONE)});
            press("o-next");
            await waitFor(settled, "the refusal");
        """,
        script="""
            const text = notice().textContent;
            expect(notice().classList.contains("err"), `not an error: ${text}`);
            expect(text.includes("is not a timezone this server knows"), `notice: ${text}`);
            expect(same(shownSteps(), ["language"]), `shown: ${shownSteps()}`);
            expect(same(marks(), ["o-timezone"]), `marked: ${marks()}`);
        """,
        sabotage="""$("#o-timezone").removeAttribute("aria-invalid");""",
        receipt=_calls(),
    ),
    Check(
        id="language-step-offers-the-listed-languages-by-name",
        fixture="empty",
        path=PAGE,
        act="await loaded();",
        script=f"""
            const field = $("#o-output-language");
            expect(field.tagName === "SELECT", `the field is a ${{field.tagName}}`);
            const wanted = {json.dumps([list(pair) for pair in LANGUAGES], ensure_ascii=False)};
            expect(same(options(field), wanted), `options: ${{JSON.stringify(options(field))}}`);
            expect(field.value === {json.dumps(PROBE_VALUES["digest.output_language"])},
              `selected: ${{field.value}}`);
        """,
        # After the act: the options lose the file's order.
        sabotage="""
            const field = $("#o-output-language");
            field.append(field.options[0]);
        """,
        receipt=_calls(),
    ),
    Check(
        id="language-step-keeps-an-unlisted-stored-value",
        fixture="unlisted",
        path=PAGE,
        act="await loaded();",
        script=f"""
            const language = $("#o-output-language"), zone = $("#o-timezone");
            const tag = {json.dumps(UNLISTED_LANGUAGE)}, other = {json.dumps(UNLISTED_TIMEZONE)};
            expect(language.value === tag, `language: ${{language.value}}`);
            expect(language.selectedOptions[0].textContent === tag,
              `shown as ${{language.selectedOptions[0].textContent}}`);
            expect(zone.value === other, `timezone: ${{zone.value}}`);
            expect(!visible($("#o-timezone-detected")), "a stored zone is marked detected");
        """,
        # After the act: the stored language's option is gone, as a list-only select's would be.
        sabotage=f"""
            $("#o-output-language").querySelector(
              'option[value={json.dumps(UNLISTED_LANGUAGE)}]').remove();
        """,
        receipt=_calls(),
    ),
    Check(
        id="language-step-leaves-an-unlisted-value-it-did-not-change",
        fixture="unlisted",
        path=PAGE,
        act=f"""
            await loaded();
            setValue($("#o-timezone"), {json.dumps(NEW_TIMEZONE)});
            press("o-next");
            await onStep("schedule");
            await waitFor(settled, "the result notice");
        """,
        script="""
            expect(!notice().classList.contains("err"), `an error: ${notice().textContent}`);
        """,
        # The page sends the language too, as one that lost the unlisted value would.
        sabotage_preload=rewrite_post(
            "/api/settings/values", 'body.values["digest.output_language"] = "en";'
        ),
        receipt=_calls({"general.timezone": NEW_TIMEZONE}),
    ),
    Check(
        id="timezone-lists-the-browsers-zones-and-utc",
        fixture="empty",
        path=PAGE,
        act="await loaded();",
        script="""
            const field = $("#o-timezone");
            expect(field.tagName === "SELECT", `the field is a ${field.tagName}`);
            const zones = options(field).map(([value]) => value);
            const known = Intl.supportedValuesOf("timeZone");
            const missing = known.filter((zone) => !zones.includes(zone));
            expect(!missing.length, `missing: ${missing.slice(0, 5)}`);
            expect(zones.includes("UTC"), "UTC is not offered");
            expect(same(zones, [...zones].sort()), "the zones are not in order");
        """,
        sabotage="""$('#o-timezone option[value="UTC"]').remove();""",
        receipt=_calls(),
    ),
    Check(
        id="timezone-opens-on-the-stored-zone-not-the-browsers",
        fixture="empty",
        path=PAGE,
        preload=PIN_ZONE,
        act="await loaded();",
        script=f"""
            const field = $("#o-timezone");
            expect(field.value === {json.dumps(PROBE_VALUES["general.timezone"])},
              `selected: ${{field.value}}`);
            expect(!visible($("#o-timezone-detected")), "a stored zone is marked detected");
        """,
        sabotage="""$("#o-timezone-detected").hidden = false;""",
        receipt=_calls(),
    ),
    Check(
        id="timezone-with-none-stored-opens-on-the-browsers-marked-detected",
        fixture="no-timezone",
        path=PAGE,
        preload=PIN_ZONE,
        act="await loaded();",
        script=f"""
            const field = $("#o-timezone");
            expect(field.value === {json.dumps(BROWSER_ZONE)}, `selected: ${{field.value}}`);
            const line = $("#o-timezone-detected");
            expect(visible(line) && line.textContent.startsWith("Detected from this browser"),
              "the zone is not marked detected");
        """,
        sabotage="""$("#o-timezone-detected").hidden = true;""",
        receipt=_calls(),
    ),
    Check(
        id="timezone-detected-is-saved-by-next",
        fixture="no-timezone",
        path=PAGE,
        preload=PIN_ZONE,
        act="""
            await loaded();
            press("o-next");
            await onStep("schedule");
            await waitFor(settled, "the result notice");
        """,
        script=f"""
            const text = notice().textContent;
            expect(text.includes("Timezone: {BROWSER_ZONE}"), `notice: ${{text}}`);
        """,
        sabotage_preload=rewrite_post(
            "/api/settings/values", 'body.values["general.timezone"] = "UTC";'
        ),
        receipt=_calls({"general.timezone": BROWSER_ZONE}),
    ),
    Check(
        id="timezone-detected-is-not-saved-by-skip",
        fixture="no-timezone",
        path=PAGE,
        preload=PIN_ZONE,
        act="""
            await loaded();
            press("o-skip");
            await onStep("schedule");
        """,
        script="""expect(!visible(notice()), `a notice: ${notice().textContent}`);""",
        sabotage_preload=SKIP_SAVES,
        receipt=_calls(),
    ),
    Check(
        id="notify-step-saves-the-webhook",
        fixture="empty",
        path=f"{PAGE}#notify",
        act=f"""
            await loaded();
            {TYPE_WEBHOOK}
            press("o-next");
            await onStep("first-issue");
            await waitFor(settled, "the result notice");
        """,
        script="""
            const text = notice().textContent;
            expect(!notice().classList.contains("err"), `an error: ${text}`);
            expect(text.includes("answered offline"), `notice: ${text}`);
            expect(!document.body.textContent.includes("NEWTOKEN"), "the token is on the page");
        """,
        sabotage_preload=rewrite_post(
            "/api/settings/notify", 'body.discord_webhook_url += "-sabotaged";'
        ),
        receipt=_calls(WEBHOOK_STORED),
    ),
    Check(
        id="notify-step-saves-the-mail-pair",
        fixture="empty",
        path=f"{PAGE}#notify",
        act=f"""
            await loaded();
            {TYPE_MAIL}
            press("o-next");
            await onStep("first-issue");
            await waitFor(settled, "the result notice");
        """,
        script="""
            const text = notice().textContent;
            expect(!notice().classList.contains("err"), `an error: ${text}`);
            expect(text.includes("me@example.org"), `notice: ${text}`);
        """,
        sabotage_preload=rewrite_post("/api/settings/email", 'body.email_to = "x@example.test";'),
        receipt=_calls(MAIL_STORED),
    ),
    Check(
        id="notify-step-saves-both-parts",
        fixture="empty",
        path=f"{PAGE}#notify",
        act=f"""
            await loaded();
            {TYPE_WEBHOOK}
            {TYPE_MAIL}
            press("o-next");
            await onStep("first-issue");
            await waitFor(settled, "the result notice");
        """,
        script="""
            expect(!notice().classList.contains("err"), `an error: ${notice().textContent}`);
        """,
        sabotage_preload=rewrite_post("/api/settings/email", "body.email_to = body.email_from;"),
        # The webhook first, as the settings form sends them.
        receipt=_calls(WEBHOOK_STORED, MAIL_STORED),
    ),
    Check(
        id="notify-step-refuses-a-sender-without-a-recipient",
        fixture="empty",
        path=f"{PAGE}#notify",
        act=f"""
            await loaded();
            setValue($("#o-email-from"), {json.dumps(NEW_MAIL_FROM)});
            press("o-next");
            await waitFor(settled, "the refusal");
        """,
        script="""
            const text = notice().textContent;
            expect(visible(notice()), "the refusal is not shown");
            expect(notice().classList.contains("err"), `not an error: ${text}`);
            expect(text.includes("sent to, too"), `notice: ${text}`);
            expect(same(shownSteps(), ["notify"]), `shown: ${shownSteps()}`);
            expect(same(marks(), ["o-email-to"]), `marked: ${marks()}`);
        """,
        sabotage="""notice().hidden = true;""",
        receipt=_calls(),
    ),
    Check(
        id="notify-step-left-empty-writes-nothing",
        fixture="empty",
        path=f"{PAGE}#notify",
        act="await loaded();",
        sabotage=TYPE_WEBHOOK,
        gestures=({"press": "#o-next", "pointer": "mouse"}, {"release": True}),
        script="""await onStep("first-issue");""",
        receipt=_calls(),
    ),
    Check(
        id="finish-leaves-for-settings",
        fixture="empty",
        path=f"{PAGE}#first-issue",
        act="""
            await loaded();
            await issueRead();
        """,
        script="""
            const finish = $("#o-finish");
            expect(visible(finish), "Finish is hidden on the last step");
            expect(finish.getAttribute("href") === "/settings?from=onboarding",
              `Finish points at ${finish.getAttribute("href")}`);
            const bar = $(".site-nav .settings-link");
            expect(bar.getAttribute("href") === "/settings?from=onboarding",
              `the site bar points at ${bar.getAttribute("href")}`);
        """,
        sabotage="""$("#o-finish").setAttribute("href", "/settings");""",
    ),
    Check(
        id="an-empty-deployment-is-sent-from-settings-to-the-wizard",
        fixture="empty",
        path=SETTINGS,
        act="await loaded();",
        script="""
            expect(location.pathname === "/onboarding", `landed on ${location.pathname}`);
            expect(visible($("#o-steps")), "no step list");
        """,
        sabotage="""$("#o-steps").hidden = true;""",
    ),
    Check(
        id="has-sources-opens-the-wizard-without-the-list",
        fixture="has-sources",
        path=PAGE,
        act="""
            await loaded();
            await skipTo("sources");
            await feedsDrawn();
        """,
        script="""
            expect(location.pathname === "/onboarding", `landed on ${location.pathname}`);
            expect(visible($("#o-steps")), "no step list");
            const line = $("#o-feeds-had");
            expect(visible(line), "the line saying this deployment has sources is hidden");
            expect(line.textContent.includes("under Sources in Settings"), line.textContent);
            expect(!feedRows().filter(visible).length, `${feedRows().length} feeds listed`);
            expect(!topicRows().filter(visible).length, `${topicRows().length} topics listed`);
            expect(!$("#o-next").disabled, "Next waits for a topic no one can choose");
        """,
        sabotage="""$("#o-feeds-had").hidden = true;""",
        receipt=_both_untouched,
    ),
    Check(
        id="has-sources-keeps-the-step-a-reload-opens",
        fixture="has-sources",
        path=f"{PAGE}#votes",
        act="await loaded();",
        script="""
            expect(location.pathname === "/onboarding", `landed on ${location.pathname}`);
            expect(same(shownSteps(), ["votes"]), `shown: ${shownSteps()}`);
        """,
        sabotage="""$('.step[data-step="votes"]').hidden = true;""",
    ),
    Check(
        id="json-has-no-wizard",
        fixture="json",
        path=PAGE,
        act="""await waitFor(() => $(".settings-nav"), "the settings page");""",
        script="""
            expect(location.pathname === "/settings", `landed on ${location.pathname}`);
            expect(!$("#o-steps"), "the wizard is on the page");
            expect($(".settings-nav"), "not the settings page");
        """,
        sabotage="""$(".settings-nav").remove();""",
    ),
    Check(
        id="fits-375",
        fixture="empty",
        path=PAGE,
        width=375,
        act="await loaded();",
        script="""
            const problem = overflow("onboarding");
            expect(!problem, problem);
        """,
        sabotage="""$(".steps").style.minWidth = "600px";""",
    ),
    Check(
        id="fits-sources-375",
        fixture="empty",
        path=f"{PAGE}#sources",
        width=375,
        act=f"""
            await loaded();
            await feedsDrawn();
            {_feeds_of(*LABELS)}
        """,
        script="""
            const problem = overflow("onboarding");
            expect(!problem, problem);
        """,
        sabotage="""feedRows()[0].style.minWidth = "600px";""",
    ),
    Check(
        id="fits-topics-375",
        fixture="empty",
        path=f"{PAGE}#sources",
        width=375,
        act=f"""
            await loaded();
            await feedsDrawn();
            {_choose(*LABELS)}
        """,
        script="""
            expect(topicsScreen(), "the topics screen is hidden");
            const problem = overflow("onboarding");
            expect(!problem, problem);
        """,
        sabotage="""topicRows()[0].style.minWidth = "600px";""",
    ),
    Check(
        id="fits-votes-375",
        fixture="cards",
        path=f"{PAGE}#votes",
        width=375,
        act="""
            await loaded();
            await cardsDrawn();
        """,
        script="""
            const problem = overflow("onboarding");
            expect(!problem, problem);
        """,
        sabotage="""cardRows()[0].style.minWidth = "600px";""",
    ),
    Check(
        id="fits-first-issue-375",
        fixture="first-issue",
        path=f"{PAGE}#first-issue",
        width=375,
        act="""
            await loaded();
            await issueRead();
            press("o-start");
            await runEnded();
        """,
        script="""
            const problem = overflow("onboarding");
            expect(!problem, problem);
        """,
        sabotage="""notice().style.minWidth = "600px";""",
    ),
    Check(
        id="fits-own-feed-375",
        fixture="empty",
        path=f"{PAGE}#sources",
        width=375,
        act=f"""
            await loaded();
            await feedsDrawn();
            {_feeds_of(ONE_TOPIC)}
            await addOwn({json.dumps(OWN_HOME)});
            await addOwn({json.dumps(BARE_HOME)});
        """,
        script="""
            expect(ownRows().length === 1 && ownNotice().classList.contains("err"),
              "the own feed or the refusal is missing");
            const problem = overflow("onboarding");
            expect(!problem, problem);
        """,
        sabotage="""$(".add-feed").style.minWidth = "600px";""",
    ),
    Check(
        id="tap-own-feed-375",
        fixture="empty",
        path=f"{PAGE}#sources",
        width=375,
        act=f"""
            await loaded();
            await feedsDrawn();
            {_feeds_of(ONE_TOPIC)}
            await addOwn({json.dumps(OWN_HOME)});
        """,
        script="""expectTapTargets("#o-own-url, #o-own-add, #o-own .choice");""",
        sabotage=restyle(
            "#o-own-add, #o-own .choice { height: 30px !important; min-height: 0 !important;"
            " padding: 0 !important; line-height: 1 !important; }"
        ),
    ),
    Check(
        id="tap-votes-375",
        fixture="cards",
        path=f"{PAGE}#votes",
        width=375,
        act="""
            await loaded();
            await cardsDrawn();
        """,
        # The control is tapped as one: `.seg` clips its buttons' outer corners to
        # its own radius, which the per-element sampling would read as a lost tap.
        script="""
            expectTapTargets("#o-cards .seg");
            const small = $$("#o-cards .seg button").filter((b) => {
              const r = b.getBoundingClientRect();
              return r.width < TAP || r.height < TAP;
            });
            expect(!small.length, `under ${TAP}px: ${small.map(named).join(", ")}`);
        """,
        sabotage=restyle("#o-cards .seg > * { height: 30px !important; }"),
    ),
    Check(
        id="tap-feeds-375",
        fixture="empty",
        path=f"{PAGE}#sources",
        width=375,
        act=f"""
            await loaded();
            await feedsDrawn();
            {_feeds_of(*LABELS)}
        """,
        script="""expectTapTargets("#o-feeds .choice");""",
        sabotage=restyle("#o-feeds .choice { padding: 0 !important; line-height: 1 !important; }"),
    ),
    Check(
        id="tap-topics-375",
        fixture="empty",
        path=f"{PAGE}#sources",
        width=375,
        act="""
            await loaded();
            await feedsDrawn();
        """,
        script="""expectTapTargets("#o-topics .tile");""",
        sabotage=restyle(
            "#o-topics .tile { min-height: 0 !important; padding: 0 !important; }"
            " #o-topics .tile .label { display: none !important; }"
        ),
    ),
    Check(
        id="tap-steps-375",
        fixture="empty",
        path=f"{PAGE}#schedule",
        width=375,
        act="await loaded();",
        script="""expectTapTargets(".step-nav .btn");""",
        sabotage=restyle(".step-nav .btn { height: 30px !important; }"),
    ),
    Check(
        id="tap-site-bar-375",
        fixture="empty",
        path=PAGE,
        width=375,
        act="""await waitFor(() => visible($(".site-nav a.gated")), "the archive link");""",
        script="""expectTapTargets(".brand, .site-nav a");""",
        sabotage=NO_SITE_BAR_AREAS,
    ),
]
# The page never scrolls sideways at the largest type size either.
_CHECKS += [
    largest_twin(check, check.id.replace("fits-", "fits-largest-"), f"{check.fixture}-largest")
    for check in _CHECKS
    if check.id
    in ("fits-375", "fits-topics-375", "fits-sources-375", "fits-votes-375", "fits-own-feed-375")
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
    # Chromium first: probe_environment() moves into a temporary home, where a
    # relative --browser path would no longer resolve.
    with chromium(browser) as socket_url, probe_environment():
        good = asyncio.run(run_all(checks, socket_url, args.self_test, build_fixture, PRELUDE))
    raise SystemExit(0 if good else 1)


if __name__ == "__main__":
    main()
