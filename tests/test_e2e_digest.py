"""One scheduled digest run, end to end, against fakes of every edge it reaches.

`cyris run` is a subprocess whose every outgoing request goes through a local
mitmproxy that answers it from `tests/e2e/fakes.py` and records it. Each test
decides from that record and from the fake D1's sqlite file afterwards, never from
the exit code alone (docs/spec/e2e-asserts-what-the-fakes-received.md).

One run carries all five scripted LLM cases, each paired with the receipt it must
change: a grouped Top story, an excerpt in place of a missing summary, the cap
leaving an article pending, a newsletter through the email parser, and ids the
model rewrote. `TestSelfChecks` proves the checks can fail.
"""

import copy
import hashlib
import json
import mimetypes
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from e2e import harness
from e2e.receipts import (
    Node,
    assert_no_unclaimed,
    assert_route_counts,
    by_route,
    d1_statements,
    json_body,
    only,
    pages_site,
    parse_html,
    statement_kind,
    unclaimed,
)

from cyris.bootstrap import default_model
from cyris.domain.models import ArticleState, SourceConfig, StoredArticle, Tier, UsageStats
from cyris.domain.triage import RejectReason
from cyris.service_layer.prompts import language_wording

pytestmark = pytest.mark.e2e

TIMEZONE = "Asia/Taipei"
WINDOW_HOURS = 24
MODEL = default_model("gemini")

DEEP = SourceConfig(
    name="E2E Deep", url="https://deep.e2e.test/feed", tier=Tier.SUMMARIZE, tags=["ai"]
)
WIRE = SourceConfig(
    name="E2E Wire", url="https://wire.e2e.test/feed", tier=Tier.FILTER, tags=["news"]
)
BLOG = SourceConfig(
    name="E2E Blog", url="https://blog.e2e.test/feed", tier=Tier.FILTER, tags=["tech"]
)
LETTERS = SourceConfig(
    name="E2E Letters",
    type="newsletter",
    tier=Tier.SUMMARIZE,
    tags=["letters"],
    email_match="from:editor@letters.e2e.test",
    homepage="https://letters.e2e.test",
)


def _article(key: str, source: SourceConfig, title: str, content: str) -> dict:
    host = source.url.split("/")[2]
    return {
        "key": key,
        "source": source.name,
        "title": title,
        "url": f"https://{host}/posts/{key}",
        "content": content,
    }


ARTICLES = {
    a["key"]: a
    for a in [
        _article("a1", DEEP, "Agents learn to read receipts", "Receipts tell a run apart."),
        _article("a2", DEEP, "Receipts beat exit codes", "An exit code of 0 proves little."),
        _article(
            "b1",
            DEEP,
            "A card without its summary",
            "The model wrote nothing for this one, so the reader gets its first lines.",
        ),
        _article("b2", DEEP, "Positions over ids", "Short integers echo back reliably."),
        _article("n1", WIRE, "Proxy outage hits region east", "Region east lost its proxy."),
        _article("n2", WIRE, "Region east proxy restored", "The proxy in region east is back."),
        _article("n3", WIRE, "Unrelated wire item", "Something else happened elsewhere."),
        _article("f1", BLOG, "Blog picks this headline", "A headline worth one line."),
        _article("f2", BLOG, "Blog filler the filter drops", "Nothing worth a line."),
    ]
}
AUTHORS = {"a1": "E2E Author"}
LETTER_EML = Path(__file__).parent / "e2e" / "letter.eml"
LETTER_TITLE = "Issue 12: what the fakes received"
# The one sender-owned post link in the letter's HTML, with its utm_ parameters stripped.
LETTER_URL = "https://letters.e2e.test/p/issue-12"
# A letter with no link at all: stored under its synthetic URL, the homepage its reader link.
UNLINKED_EML = Path(__file__).parent / "e2e" / "letter-unlinked.eml"
UNLINKED_TITLE = "Issue 13: no link this week"
LETTER_ITEM_IDS = [f"nl:{eml.stem}" for eml in (LETTER_EML, UNLINKED_EML)]
LETTER_TEXT = {
    "l1": "This week's letter is about end-to-end receipts: count every request, read every body.",
    "l2": "This issue has no web version, so the reader is pointed at the letters' homepage.",
}
LETTER_DATES = {"l1": "2026-09-30T07:00:00.000000+00:00", "l2": "2026-10-01T07:00:00.000000+00:00"}


def _letter_id(subject: str) -> str:
    """newsletter.py's article id: sha256 of the source name and the subject."""
    return hashlib.sha256(f"{LETTERS.name}{subject}".encode()).hexdigest()


UNLINKED_URL = f"newsletter:{_letter_id(UNLINKED_TITLE)}"

TITLE = {key: a["title"] for key, a in ARTICLES.items()} | {
    "l1": LETTER_TITLE,
    "l2": UNLINKED_TITLE,
}
URL = {key: a["url"] for key, a in ARTICLES.items()} | {"l1": LETTER_URL, "l2": UNLINKED_URL}

# l2 scores under the summarize threshold, so it is listed On the Radar unsummarised.
SCORES = {"a1": 92, "a2": 88, "b1": 75, "b2": 70, "l1": 72, "l2": 45, "f1": 60, "f2": 40}
TOP_GROUP = {"heading": "Receipts over exit codes", "summary": "Both say: read the receipt."}
CLUSTER = {
    "titles": [TITLE["n1"], TITLE["n2"]],
    "heading": "Region east proxy outage and recovery",
    "summary": "The proxy went down and came back.",
    "tags": ["outage"],
}
OWN_SUMMARIES = {
    "a1": "A1 on its own.",
    "a2": "A2 on its own.",
    "b2": "B2 on its own.",
    "l1": "The letter on its own.",
}
FILTER_PICKS = {"f1": "F1 in one line.", "n3": "N3 in one line."}
LLM = {
    # A model that echoes each position zero-padded, as a string: "00", "01", ...
    "rewrite_id": "{:02d}",
    "scores": {TITLE[k]: score for k, score in SCORES.items()},
    "clusters": [CLUSTER],
    "filter_select": [TITLE[k] for k in FILTER_PICKS],
    "filter_summaries": {TITLE[k]: text for k, text in FILTER_PICKS.items()},
    "groups": {
        "ai": TOP_GROUP,
        "letters": {"heading": "Letters", "summary": "One letter this week."},
    },
    # b1 has none: the model left that Features article out of `summaries`.
    "own_summaries": {TITLE[k]: text for k, text in OWN_SUMMARIES.items()},
}
SETTINGS = harness.settings(
    **{
        "general.timezone": TIMEZONE,
        "general.digest_window_hours": WINDOW_HOURS,
        "llm_provider.provider": "gemini",
        "llm_provider.model": "",
        "notify.discord_webhook_url": harness.DISCORD_WEBHOOK,
        "notify.email_to": "reader@e2e.test",
        "notify.email_from": "digest@e2e.test",
        "digest.output_language": "zh-Hant",
        "digest.style_prompt": "E2E style: name the receipt.",
        # a1 and a2 clear 80 and form the Top story; b1, b2 and l1 are Features.
        "routing.score_threshold": 80,
        "routing.summarize_score_threshold": 50,
        "digest.max_featured": 5,
        # 2 Top story + 3 Features + 1 cluster + 1 On the Radar + 1 Wire row: n3, the
        # filter's second pick, is the one article the cap cuts.
        "digest.max_articles_per_digest_output": 8,
    }
)
SHOWN = {"a1", "a2", "b1", "b2", "l1", "l2", "n1", "n2", "f1"}
FEATURES = ("b1", "l1", "b2")
SOURCE_OF = {key: a["source"] for key, a in ARTICLES.items()} | {
    "l1": LETTERS.name,
    "l2": LETTERS.name,
}
SOURCES = {s.name: s for s in (DEEP, WIRE, BLOG, LETTERS)}


def _voted(now: datetime) -> StoredArticle:
    """An article an earlier run accepted three days ago, which the reader has voted down."""
    then = now - timedelta(days=3)
    return StoredArticle(
        url="https://deep.e2e.test/posts/v1",
        original_id="e2e-v1",
        title="An issue from three days ago",
        content="The reader voted this one down since.",
        published_at=then,
        source_name=DEEP.name,
        source_tier=Tier.SUMMARIZE,
        source_tags=["ai"],
        state=ArticleState.ACCEPTED,
        first_seen_at=then,
        digest_date=then.strftime("%Y-%m-%d"),
        score=81.0,
        language="en",
    )


def _rss_rows(now: datetime) -> list[dict]:
    return [
        {
            "guid": f"e2e-{a['key']}",
            "title": a["title"],
            "url": a["url"],
            "content": a["content"],
            "author": AUTHORS.get(a["key"]),
            # As the Worker stores it: JavaScript's toISOString().
            "published_at": (now - timedelta(minutes=10 + i))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "source_name": a["source"],
        }
        for i, a in enumerate(ARTICLES.values())
    ]


def _scenario(**overrides) -> harness.Scenario:
    now = datetime.now(UTC)
    voted = _voted(now)
    return harness.Scenario(
        settings=SETTINGS,
        sources=[DEEP, WIRE, BLOG, LETTERS],
        rss_rows=_rss_rows(now),
        newsletters=[LETTER_EML, UNLINKED_EML],
        llm=LLM,
        stored=[voted],
        promotions=[{"url": voted.url, "vote": "down", "digest_date": voted.digest_date}],
        **overrides,
    )


def _local_date(at: datetime) -> str:
    return at.astimezone(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d")


class Receipts:
    """What one run left behind, in the forms the checks read; copied before breaking."""

    def __init__(self, run: harness.Run) -> None:
        self.records = run.records
        self.routes = run.routes
        self.run = run
        self.site = pages_site(run.records)
        (page_path,) = [p for p in self.site if p.endswith("-morning.html")]
        self.slug = page_path.removeprefix("/").removesuffix(".html")
        self.date = self.slug.removesuffix("-morning")
        dates = {_local_date(run.started_at), _local_date(run.finished_at)}
        assert self.date in dates, f"digest dated {self.date}, run spanned {dates}"
        self.digest_url = f"https://{harness.WORKER_DOMAINS[0]}/{self.slug}"
        self.articles = {r["url"]: r for r in run.rows("SELECT * FROM stored_articles")}
        self.runs = run.rows("SELECT * FROM digest_runs")

    def page(self) -> Node:
        return parse_html(self.site[f"/{self.slug}.html"])

    def discord(self) -> dict:
        post = only(self.records, "discord_webhook")
        return json_body(post)

    def mail(self) -> dict:
        post = only(self.records, "email_send")
        return json_body(post)


@pytest.fixture(scope="module")
def run(tmp_path_factory) -> harness.Run:
    # Pages answers the new deployment as still active once, so publishing re-reads it.
    scenario = _scenario(pages_pending_reads=1)
    return harness.run_deployment(tmp_path_factory.mktemp("e2e-digest"), scenario)


@pytest.fixture(scope="module")
def receipts(run) -> Receipts:
    assert run.returncode == 0, run.diagnostics()
    assert_no_unclaimed(run.records)
    return Receipts(run)


# ---- the record as a whole ----------------------------------------------------


EXPECTED_ROUTES = {
    "d1_query": None,  # len(EXPECTED_D1)
    "pages_deployments_list": 1,
    "pages_upload_token": 1,
    "pages_check_missing": 1,
    "pages_upload": 1,
    "pages_upsert_hashes": 1,
    "pages_deployment_create": 1,
    "pages_deployment_get": 1,
    "pages_project_create": 0,
    "pages_site": 1,
    "workers_domains": 1,
    "email_send": 1,
    "discord_webhook": 1,
    "gemini_generate": 5,
    "rss_articles": 1,
    "newsletter_list": 1,
    "newsletter_ack": 1,
    "promote_list": 1,
    "promote_ack": 1,
}

# Derived from the code paths of one first publish, in the order they run:
# load_effective_config, the vote sync, then run_digest, publish_site and the run record.
EXPECTED_D1 = [
    "schema",
    "SELECT settings",
    "SELECT sources",
    # The vote: find the voted article, reject it, stamp it as a human verdict.
    "SELECT stored_articles",
    "UPDATE stored_articles SET state",
    "UPDATE stored_articles SET triaged_at",
    # The save: the newsletter dedup read, then 11 articles at 17 columns, five rows a
    # statement under D1's 100 bound parameters.
    "SELECT stored_articles",
    *["INSERT OR IGNORE INTO stored_articles"] * 3,
    "SELECT stored_articles",
    "UPDATE stored_articles SET score",
    "SELECT stored_articles",
    "INSERT OR IGNORE INTO tags",
    "INSERT OR REPLACE INTO article_tags",
    "INSERT OR REPLACE INTO stories",
    "INSERT OR IGNORE INTO story_members",
    "DELETE FROM story_members",
    "DELETE FROM stories",
    "INSERT INTO usage_log",
    # One per verdict: accepted, then rejected; then the window's collected articles.
    "UPDATE stored_articles SET state",
    "UPDATE stored_articles SET state",
    "SELECT stored_articles",
    "INSERT OR REPLACE INTO digests",
    # The archive's file list and counts for the index page, then the manifest the
    # deploy extends, the first-publish receipt, and the manifest of what landed.
    "SELECT pages_manifest",
    "SELECT usage_log",
    "SELECT pages_manifest",
    "SELECT pages_deploy_receipt",
    "INSERT OR IGNORE INTO pages_deploy_receipt",
    "DELETE FROM pages_manifest",
    "INSERT INTO pages_manifest",
    "INSERT INTO digest_runs",
]
EXPECTED_ROUTES["d1_query"] = len(EXPECTED_D1)


def test_the_run_exits_ok_and_reaches_only_the_fakes(run) -> None:
    assert run.returncode == 0, run.diagnostics()
    assert_no_unclaimed(run.records)


def test_every_route_receives_exactly_its_requests(receipts) -> None:
    assert_route_counts(receipts.records, receipts.routes, EXPECTED_ROUTES)


def test_d1_receives_exactly_these_statements_in_this_order(receipts) -> None:
    assert [statement_kind(s["sql"]) for s in d1_statements(receipts.records)] == EXPECTED_D1


def _within_run(r: "Receipts", iso: str) -> bool:
    return r.run.started_at <= datetime.fromisoformat(iso) <= r.run.finished_at


def _expected_row(r: "Receipts", key: str) -> dict:
    """The stored_articles row cyris should leave for one article, first_seen_at aside."""
    if key.startswith("l"):
        title = TITLE[key]
        original_id, content, author = _letter_id(title), LETTER_TEXT[key], None
        published = LETTER_DATES[key]
        ref_urls = [LETTERS.homepage] if key == "l2" else []
    else:
        row = next(x for x in r.run.scenario.rss_rows if x["guid"] == f"e2e-{key}")
        original_id, title, content, author = row["guid"], row["title"], row["content"], None
        author = AUTHORS.get(key)
        published = datetime.fromisoformat(row["published_at"]).isoformat(timespec="microseconds")
        ref_urls = []
    source = SOURCES[SOURCE_OF[key]]
    state = "accepted" if key in SHOWN else "rejected" if key == "f2" else "pending"
    return {
        "url": URL[key],
        "original_id": original_id,
        "title": title,
        "content": content,
        "author": author,
        "published_at": published,
        "source_name": source.name,
        "source_tier": str(source.tier),
        "source_tags": json.dumps(source.tags),
        "ref_urls": json.dumps(ref_urls),
        "state": state,
        "digest_date": r.date if state != "pending" else None,
        "rejection_reason": "filtered" if key == "f2" else None,
        "score": float(SCORES[key]) if key in SCORES else None,
        "language": "en" if key in SCORES else None,
        "triaged_at": None,
    }


def test_every_article_row_holds_what_cyris_was_given_and_decided(receipts) -> None:
    rows = copy.deepcopy(receipts.articles)
    voted = rows.pop(receipts.run.scenario.stored[0].url)
    first_seen = {row.pop("first_seen_at") for row in rows.values()}
    assert len(first_seen) == 1 and _within_run(receipts, first_seen.pop())
    assert rows == {URL[k]: _expected_row(receipts, k) for k in URL}

    # The vote: rejected, as not interested, and stamped as the reader's own verdict.
    seed = receipts.run.scenario.stored[0]
    assert _within_run(receipts, voted.pop("triaged_at"))
    assert voted == {
        "url": seed.url,
        "original_id": seed.original_id,
        "title": seed.title,
        "content": seed.content,
        "author": None,
        "published_at": seed.published_at.isoformat(timespec="microseconds"),
        "source_name": seed.source_name,
        "source_tier": "summarize",
        "source_tags": '["ai"]',
        "ref_urls": "[]",
        "state": "rejected",
        "first_seen_at": seed.first_seen_at.isoformat(timespec="microseconds"),
        "digest_date": date.today().isoformat(),
        "rejection_reason": str(RejectReason.NOT_INTERESTED),
        "score": seed.score,
        "language": seed.language,
    }
    ack = only(receipts.records, "promote_ack")
    assert json_body(ack) == {"urls": [seed.url]}


def test_the_run_records_its_spend_its_outcome_and_its_issue(receipts) -> None:
    run = receipts.run
    usage = UsageStats(model=MODEL)
    for _ in range(EXPECTED_ROUTES["gemini_generate"]):
        usage.add(1000, 200)
    (logged,) = run.rows("SELECT * FROM usage_log")
    assert _within_run(receipts, logged.pop("logged_at"))
    assert logged == {
        "digest_date": receipts.date,
        "period": "morning",
        "articles_received": 11,
        "articles_included": 8,
        "model": MODEL,
        "api_calls": 5,
        "input_tokens": 5000,
        "output_tokens": 1000,
        "cost_usd": round(usage.estimated_cost, 6),
    }

    (recorded,) = copy.deepcopy(receipts.runs)
    summary = json.loads(recorded.pop("summary"))
    assert _within_run(receipts, recorded.pop("finished_at"))
    assert recorded.pop("wall_seconds") == summary["wall_seconds"] > 0
    assert recorded == {
        "id": 1,
        "status": "ok",
        "period": "morning",
        "dry_run": 0,
        "fetched": 11,
        "failed_sources": "[]",
        "build_sha": harness.GIT_SHA,
        "degraded": 1,
    }
    assert {k: summary[k] for k in ("status", "fetched", "received", "included", "degraded")} == {
        "status": "ok",
        "fetched": 11,
        "received": 11,
        "included": 8,
        "degraded": True,
    }
    assert summary["digest_url"] == receipts.digest_url
    assert (summary["llm"]["model"], summary["llm"]["api_calls"]) == (MODEL, 5)

    (issue,) = run.rows("SELECT * FROM digests")
    content = json.loads(issue["content"])
    assert (issue["date"], issue["period"], issue["raw_page"]) == (receipts.date, "morning", 1)
    assert (content["articles_received"], content["articles_included"]) == (11, 8)

    members = sorted([URL["n1"], URL["n2"]])
    story_id = (
        f"{receipts.date}-morning-" + hashlib.sha1("\n".join(members).encode()).hexdigest()[:8]
    )
    assert [
        (s["id"], s["digest_date"], s["period"], s["heading"])
        for s in run.rows("SELECT * FROM stories")
    ] == [(story_id, receipts.date, "morning", CLUSTER["heading"])]
    assert run.rows("SELECT * FROM story_members ORDER BY article_url") == [
        {"story_id": story_id, "article_url": url} for url in members
    ]
    assert [t["name"] for t in run.rows("SELECT name FROM tags")] == CLUSTER["tags"]
    assert sorted((t["article_url"], t["tag"]) for t in run.rows("SELECT * FROM article_tags")) == [
        (url, "outage") for url in members
    ]
    receipt = run.rows("SELECT project FROM pages_deploy_receipt")
    assert receipt == [{"project": harness.PAGES_PROJECT}]


def test_the_settings_and_sources_are_read_and_never_written(receipts) -> None:
    for table, rows in receipts.run.seeded.items():
        assert receipts.run.rows(f"SELECT * FROM {table}") == rows, table


SECRETS = {
    harness.CF_TOKEN,
    harness.PAGES_UPLOAD_JWT,
    harness.WORKER_TOKEN,
    harness.PROMOTE_TOKEN,
    harness.GEMINI_KEY,
    harness.DISCORD_WEBHOOK.rsplit("/", 1)[1],
}
CREDENTIAL_HEADERS = {
    "authorization",
    "proxy-authorization",
    "x-goog-api-key",
    "x-api-key",
    "cookie",
}


def test_every_request_carries_its_own_credential_and_no_other(receipts) -> None:
    """The one header each service requires; no other service's secret anywhere in it."""
    for record in receipts.records:
        route = record["route"]
        expected = dict([harness.CREDENTIALS[route]]) if route in harness.CREDENTIALS else {}
        sent = {h: v for h, v in record["headers"].items() if h in CREDENTIAL_HEADERS}
        assert sent == expected, route
        own = {value.removeprefix("Bearer ") for value in expected.values()}
        if route == "discord_webhook":
            own.add(harness.DISCORD_WEBHOOK.rsplit("/", 1)[1])
        carried = json.dumps([record["headers"], record["query"], record["body"], record["path"]])
        assert {secret for secret in SECRETS if secret in carried} == own, route
    discord = only(receipts.records, "discord_webhook")
    assert "https://discord.com" + discord["path"] == harness.DISCORD_WEBHOOK


def test_each_request_names_its_account_database_and_project(receipts) -> None:
    account = f"/client/v4/accounts/{harness.ACCOUNT_ID}"
    project = f"{account}/pages/projects/{harness.PAGES_PROJECT}"
    paths = {
        "d1_query": f"{account}/d1/database/{harness.DATABASE_ID}/query",
        "pages_deployments_list": f"{project}/deployments",
        "pages_upload_token": f"{project}/upload-token",
        "pages_deployment_create": f"{project}/deployments",
        "workers_domains": f"{account}/workers/domains",
        "email_send": f"{account}/email/sending/send",
        "gemini_generate": f"/v1beta/models/{MODEL}:generateContent",
    }
    for record in receipts.records:
        if record["route"] in paths:
            assert record["path"] == paths[record["route"]], record["route"]
    listing = only(receipts.records, "pages_deployments_list")
    assert listing["query"] == [["per_page", "1"]]
    domains = only(receipts.records, "workers_domains")
    assert domains["query"] == [["service", harness.APP_WORKER]]
    live = only(receipts.records, "pages_site")
    assert (live["host"], live["path"]) == (harness.HOSTS["pages_host"], f"/{receipts.slug}")
    queried = {"pages_deployments_list", "workers_domains", "rss_articles"}
    for record in receipts.records:
        if record["route"] not in queried:
            assert record["query"] == [], record["route"]


def test_the_rss_buffer_is_read_for_the_configured_window(receipts) -> None:
    read = only(receipts.records, "rss_articles")
    query = dict(read["query"])
    assert set(query) == {"after", "before", "limit"}
    before = datetime.fromisoformat(query["before"])
    assert receipts.run.started_at <= before <= receipts.run.finished_at
    assert before - datetime.fromisoformat(query["after"]) == timedelta(hours=WINDOW_HOURS)
    assert query["limit"] == "2000"


def _llm_calls(records: list[dict]) -> list[tuple[str, list[str]]]:
    return [
        (r["llm"]["kind"], [title for _, _, title in r["llm"]["articles"]])
        for r in by_route(records, "gemini_generate")
    ]


def test_each_llm_call_names_its_kind_and_the_articles_it_was_given(receipts) -> None:
    calls = by_route(receipts.records, "gemini_generate")
    kinds = [c["llm"]["kind"] for c in calls]
    assert kinds == ["scoring", "cluster", "filter", "summarize", "summarize"]
    # Sorted, not sets: an article listed twice must show. The order within a prompt
    # is the store's, which no setting decides.
    given = [sorted(titles) for _, titles in _llm_calls(receipts.records)]
    assert given[:3] == [
        sorted(TITLE[k] for k in ("a1", "a2", "b1", "b2", "l1", "l2", "f1", "f2")),
        sorted(TITLE[k] for k in ("n1", "n2", "n3")),
        sorted(TITLE[k] for k in ("f1", "f2", "n3")),
    ]
    # One call per topic group, in the order the store returns the window.
    assert sorted(given[3:]) == sorted(
        [sorted(TITLE[k] for k in ("a1", "a2", "b1", "b2")), [TITLE["l1"]]]
    )
    for call in calls:
        ids = [article_id for article_id, _, _ in call["llm"]["articles"]]
        body = json_body(call)
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        if call["llm"]["kind"] == "scoring":
            # Scoring names each article by its stored id, not by position.
            pairs = sorted((title, article_id) for article_id, _, title in call["llm"]["articles"])
            assert pairs == sorted(
                (TITLE[k], receipts.articles[URL[k]]["original_id"]) for k in SCORES
            )
            assert ("Agents learn to read receipts", "e2e-a1") in pairs
            assert "temperature" not in body["generationConfig"]
        else:
            assert ids == [str(i) for i in range(len(ids))]
            assert body["generationConfig"]["temperature"] == 1.0
            # The two values cyris writes into every written prompt's instructions.
            system = body["system_instruction"]["parts"][0]["text"]
            assert language_wording(SETTINGS["digest.output_language"]) in system
            assert "<output_language>" not in system
            assert system.endswith(SETTINGS["digest.style_prompt"])


def test_pages_receives_every_file_of_the_site_once(receipts) -> None:
    site = receipts.site
    assert set(site) == {
        f"/{receipts.slug}.html",
        f"/{receipts.slug}-raw.html",
        "/index.html",
        "/favicon.svg",
    }
    deployment = only(receipts.records, "pages_deployment_create")
    assert deployment["form"]["branch"] == "main"
    manifest = json.loads(deployment["form"]["manifest"])
    check = only(receipts.records, "pages_check_missing")
    assert sorted(json_body(check)["hashes"]) == sorted(manifest.values())
    upload = only(receipts.records, "pages_upload")
    assert sorted(a["key"] for a in json_body(upload)) == sorted(manifest.values())
    path_of = {digest: path for path, digest in manifest.items()}
    for asset in json_body(upload):
        path = path_of[asset["key"]]
        assert asset["base64"] is True, path
        assert asset["metadata"] == {"contentType": mimetypes.guess_type(path)[0]}, path
    assert {mimetypes.guess_type(p)[0] for p in manifest} == {"text/html", "image/svg+xml"}
    upsert = only(receipts.records, "pages_upsert_hashes")
    assert sorted(json_body(upsert)["hashes"]) == sorted(manifest.values())
    # The manifest D1 keeps for the next deploy is the one this deploy named.
    (insert,) = [
        s
        for s in d1_statements(receipts.records)
        if statement_kind(s["sql"]) == "INSERT INTO pages_manifest"
    ]
    params = insert["params"]
    assert {params[i]: params[i + 1] for i in range(0, len(params), 3)} == manifest


def test_the_raw_page_lists_the_window_and_the_index_links_both_pages(receipts) -> None:
    raw = parse_html(receipts.site[f"/{receipts.slug}-raw.html"])
    assert {h for h in raw.hrefs() if h in set(URL.values())} == set(URL.values())
    index = parse_html(receipts.site["/index.html"]).hrefs()
    assert {f"{receipts.slug}.html", f"{receipts.slug}-raw.html"} <= set(index)


def test_the_run_writes_nothing_beside_its_config(receipts) -> None:
    """D1 and Pages are the run's only stores: no agent-vault/, no html/."""
    harness_files = {
        ".env", "cyris.toml", "d1.sqlite", "home", "mitmproxy", "proxy.log", "proxy.ready",
        "requests.jsonl", "routes.json", "script.json", "tmp",
    }  # fmt: skip
    assert {p.name for p in receipts.run.dir.iterdir()} == harness_files
    assert not any((receipts.run.dir / "home").iterdir())


# ---- scripted case 1: two high scorers on one topic are one Top story --------


def check_grouped_top_story(r: Receipts) -> None:
    lead = r.page().one("article", "lead-story")
    heading = lead.one("h2")
    assert "lang" not in heading.attrs  # a group's heading is the model's, in its language
    assert heading.text() == TOP_GROUP["heading"]
    assert lead.one("p", "summary").text() == TOP_GROUP["summary"]
    members = lead.find_all("article", "article-item")
    assert [m.one("h3").text() for m in members] == [TITLE["a1"], TITLE["a2"]]
    assert {h for m in members for h in m.hrefs()} == {URL["a1"], URL["a2"]}

    top = r.discord()["embeds"][0]["description"]
    assert TOP_GROUP["heading"] in top
    assert f"- [{TITLE['a1']}]({URL['a1']})" in top
    assert f"- [{TITLE['a2']}]({URL['a2']})" in top

    mail = r.mail()
    assert f"  - {TITLE['a1']} — {URL['a1']}" in mail["text"]
    assert f"  - {TITLE['a2']} — {URL['a2']}" in mail["text"]
    mail_lead = parse_html(mail["html"]).one("article", "lead")
    assert mail_lead.one("h3", "lead-title").text() == TOP_GROUP["heading"]


def test_two_high_scorers_on_one_topic_reach_the_page_as_one_top_story(receipts) -> None:
    check_grouped_top_story(receipts)


# ---- scripted case 2: a Features article the model wrote no summary for ------


def check_missing_summary_degrades(r: Receipts) -> None:
    cards = {c.one("h3").hrefs()[0]: c for c in r.page().find_all("article", "featured-item")}
    assert list(cards) == [URL[k] for k in FEATURES]
    fallback = cards[URL["b1"]]
    assert fallback.attrs.get("lang") == ""  # an excerpt is in the article's own language
    assert fallback.one("p", "summary").text() == ARTICLES["b1"]["content"]
    for key in ("l1", "b2"):
        assert "lang" not in cards[URL[key]].attrs
        assert cards[URL[key]].one("p", "summary").text() == OWN_SUMMARIES[key]

    assert len(r.page().find_all("p", "notice")) == 1
    assert MODEL in r.discord()["content"]
    mail = parse_html(r.mail()["html"])
    assert len(mail.find_all("p", "notice")) == 1
    mail_cards = {a.one("h3").text(): a for a in mail.find_all("article", "item")}
    assert mail_cards[TITLE["b1"]].attrs["lang"] == ""
    (row,) = r.runs
    assert row["degraded"] == 1
    assert json.loads(row["summary"])["degraded"] is True


def test_a_missing_summary_shows_the_excerpt_and_flags_the_run_degraded(receipts) -> None:
    check_missing_summary_degrades(receipts)


# ---- scripted case 3: the per-issue cap leaves what it cut pending -----------


def check_cap_leaves_cut_pending(r: Receipts) -> None:
    state = {key: r.articles[URL[key]] for key in URL}
    assert {k for k, row in state.items() if row["state"] == "accepted"} == SHOWN
    assert {k for k in SHOWN if state[k]["digest_date"] == r.date} == SHOWN
    assert state["n3"]["state"] == "pending"
    assert state["n3"]["digest_date"] is None
    assert (state["f2"]["state"], state["f2"]["rejection_reason"]) == ("rejected", "filtered")

    updates = [
        s["params"]
        for s in d1_statements(r.records)
        if statement_kind(s["sql"]) == "UPDATE stored_articles SET state"
    ][-2:]  # the first one is the vote's
    assert [(p[0], p[1], p[2], set(p[3:])) for p in updates] == [
        ("accepted", r.date, None, {URL[k] for k in SHOWN}),
        ("rejected", r.date, "filtered", {URL["f2"]}),
    ]
    page = r.page()
    shown_links = set(page.hrefs())
    assert URL["n3"] not in shown_links
    # l2's only link is the homepage; its synthetic URL is not one.
    assert {URL[k] for k in SHOWN - {"l2"}} <= shown_links


def test_an_article_the_cap_cut_stays_pending_while_every_shown_one_is_accepted(receipts) -> None:
    check_cap_leaves_cut_pending(receipts)


# ---- scripted case 4: a queued newsletter through the real email parser ------


def check_newsletter_reaches_the_digest(r: Receipts) -> None:
    pull = only(r.records, "newsletter_list")
    ack = only(r.records, "newsletter_ack")
    assert pull["seq"] < ack["seq"]
    assert json_body(ack) == {"ids": LETTER_ITEM_IDS}
    assert LETTER_URL in r.articles, "the letter was stored without its canonical link"
    assert r.articles[LETTER_URL]["state"] == "accepted"
    card = next(c for c in r.page().find_all("article", "featured-item") if LETTER_URL in c.hrefs())
    assert card.one("h3").text() == LETTER_TITLE
    assert LETTERS.name in card.text()
    # The unlinked letter is listed On the Radar, its reader link the letters' homepage.
    radar = r.page().one("article", "attention-item")
    assert (radar.one("h4").text(), radar.hrefs()) == (UNLINKED_TITLE, [LETTERS.homepage])


def test_a_queued_newsletter_reaches_the_digest_through_the_email_parser(receipts) -> None:
    check_newsletter_reaches_the_digest(receipts)


# ---- scripted case 5: ids the model rewrote still match by position ----------


def check_rewritten_ids_match(r: Receipts) -> None:
    page = r.page()
    cluster = page.one("div", "news-cluster")
    assert cluster.one("h3").text() == CLUSTER["heading"]
    assert set(cluster.hrefs()) == {URL["n1"], URL["n2"]}
    wire = page.find_all("div", "headline-item")
    assert [row.hrefs() for row in wire] == [[URL["f1"]]]
    assert wire[0].one("p", "summary").text() == FILTER_PICKS["f1"]


def test_ids_the_model_rewrote_still_match_by_position(receipts) -> None:
    check_rewritten_ids_match(receipts)


# ---- the notifications ---------------------------------------------------------


def test_discord_and_mail_each_receive_the_issue_once(receipts) -> None:
    # Top story, Features, In Focus, On the Radar, The Wire, then the stats: Following
    # has nothing in it this issue.
    top, features, focus, radar, wire, stats = receipts.discord()["embeds"]
    assert UNLINKED_TITLE in radar["description"]
    for key in FEATURES:
        assert f"[{TITLE[key]}]({URL[key]})" in features["description"], key
    assert CLUSTER["heading"] in focus["description"]
    assert f"[{TITLE['f1']}]({URL['f1']})" in wire["description"]
    assert TITLE["n3"] not in wire["description"]
    assert f"({receipts.digest_url})" in stats["description"]
    # 4 sources, 11 articles received, 8 items kept: the cluster is one item.
    for count in ("**4**", "**11**", "**8**"):
        assert count in stats["description"], count
    assert stats["title"].endswith(receipts.date)
    assert stats["title"].split()[0] == "Morning"
    # Each section keeps its own colour, as the reader knows them in the channel.
    assert [e["color"] for e in (top, features, focus, radar, wire, stats)] == [
        0xF1C40F, 0x5865F2, 0xFEE75C, 0x9B59B6, 0x95A5A6, 0x57F287,
    ]  # fmt: skip
    assert all(e["title"] for e in (top, features, focus, radar, wire, stats))
    mail = receipts.mail()
    assert (mail["to"], mail["from"]) == ("reader@e2e.test", "digest@e2e.test")
    words = mail["subject"].split()
    assert (words[0], words[-1]) == ("Morning", receipts.date)
    assert receipts.digest_url in mail["text"]
    assert receipts.digest_url in parse_html(mail["html"]).hrefs()
    for key in SHOWN - {"n1", "n2"}:
        assert TITLE[key] in mail["text"], key


# ---- self-tests: the checks above can fail ----------------------------------


class TestSelfChecks:
    """Binds the two prose specs: a missing fake and a broken receipt both fail."""

    def test_a_dropped_fake_is_reported_as_unclaimed(self, tmp_path) -> None:
        dropped = harness.run_deployment(tmp_path, _scenario(drop=["discord_webhook"]))
        stray = unclaimed(dropped.records)
        assert [(r["method"], r["host"], r["status"]) for r in stray] == [
            ("POST", "discord.com", 502)
        ], dropped.diagnostics()
        with pytest.raises(AssertionError, match="requests no fake claims"):
            assert_no_unclaimed(dropped.records)

    def test_a_doubled_request_fails_the_count_check(self, receipts) -> None:
        records = copy.deepcopy(receipts.records)
        records.append(by_route(records, "discord_webhook")[0])
        with pytest.raises(AssertionError):
            assert_route_counts(records, receipts.routes, EXPECTED_ROUTES)

    @pytest.mark.parametrize(
        ("check", "break_receipt"),
        [
            pytest.param(
                check_grouped_top_story,
                lambda r: r.site.update(
                    {f"/{r.slug}.html": r.site[f"/{r.slug}.html"].replace(
                        TOP_GROUP["heading"].encode(), b"another heading"
                    )}
                ),
                id="top-story",
            ),
            pytest.param(
                check_missing_summary_degrades,
                lambda r: r.runs[0].update(degraded=0),
                id="degraded",
            ),
            pytest.param(
                check_cap_leaves_cut_pending,
                lambda r: r.articles[URL["n3"]].update(state="accepted"),
                id="cap",
            ),
            pytest.param(
                check_newsletter_reaches_the_digest,
                lambda r: r.records.remove(by_route(r.records, "newsletter_ack")[0]),
                id="newsletter",
            ),
            pytest.param(
                check_rewritten_ids_match,
                lambda r: r.site.update(
                    {f"/{r.slug}.html": r.site[f"/{r.slug}.html"].replace(
                        URL["f1"].encode(), URL["f2"].encode()
                    )}
                ),
                id="rewritten-ids",
            ),
        ],
    )  # fmt: skip
    def test_a_broken_receipt_fails_its_check(self, receipts, check, break_receipt) -> None:
        broken = copy.deepcopy(receipts)
        break_receipt(broken)
        with pytest.raises(AssertionError):
            check(broken)


# ---- the harness ----------------------------------------------------------------


def test_a_proxy_that_dies_at_startup_fails_with_its_own_log(tmp_path, monkeypatch) -> None:
    """The proxy's log is the startup error, not a kill of a process already gone."""
    uvx = tmp_path / "uvx"
    uvx.write_text("#!/bin/sh\necho 'mitmdump died at startup'\nexit 3\n")
    uvx.chmod(0o755)
    monkeypatch.setattr(harness.shutil, "which", lambda _name: str(uvx))
    script = tmp_path / "script.json"
    script.write_text(json.dumps({"ready": str(tmp_path / "proxy.ready")}))
    with (
        pytest.raises(AssertionError, match="mitmdump died at startup"),
        harness._proxy(tmp_path, script),
    ):
        pass
