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
import json
from datetime import UTC, datetime, timedelta
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
from cyris.domain.models import SourceConfig, Tier

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
LETTER_EML = Path(__file__).parent / "e2e" / "letter.eml"
LETTER_TITLE = "Issue 12: what the fakes received"
# The one sender-owned post link in the letter's HTML, with its utm_ parameters stripped.
LETTER_URL = "https://letters.e2e.test/p/issue-12"
LETTER_ITEM_ID = f"nl:{LETTER_EML.stem}"

TITLE = {key: a["title"] for key, a in ARTICLES.items()} | {"l1": LETTER_TITLE}
URL = {key: a["url"] for key, a in ARTICLES.items()} | {"l1": LETTER_URL}

SCORES = {"a1": 92, "a2": 88, "b1": 75, "b2": 70, "l1": 72, "f1": 60, "f2": 40}
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
        # a1 and a2 clear 80 and form the Top story; b1, b2 and l1 are Features.
        "routing.score_threshold": 80,
        "routing.summarize_score_threshold": 50,
        "digest.max_featured": 5,
        # 2 Top story + 3 Features + 1 cluster + 1 Wire row: n3, the filter's second
        # pick, is the one article the cap cuts.
        "digest.max_articles_per_digest_output": 7,
    }
)
SHOWN = {"a1", "a2", "b1", "b2", "l1", "n1", "n2", "f1"}
FEATURES = ("b1", "l1", "b2")


def _rss_rows(now: datetime) -> list[dict]:
    return [
        {
            "guid": f"e2e-{a['key']}",
            "title": a["title"],
            "url": a["url"],
            "content": a["content"],
            "author": None,
            "published_at": (now - timedelta(minutes=10 + i)).isoformat(),
            "source_name": a["source"],
        }
        for i, a in enumerate(ARTICLES.values())
    ]


def _scenario(**overrides) -> harness.Scenario:
    return harness.Scenario(
        settings=SETTINGS,
        sources=[DEEP, WIRE, BLOG, LETTERS],
        rss_rows=_rss_rows(datetime.now(UTC)),
        newsletters=[LETTER_EML],
        llm=LLM,
        **overrides,
    )


def _local_date(at: datetime) -> str:
    return at.astimezone(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d")


class Receipts:
    """What one run left behind, in the forms the checks read; copied before breaking."""

    def __init__(self, run: harness.Run) -> None:
        self.records = run.records
        self.routes = run.routes
        self.site = pages_site(run.records)
        (page_path,) = [p for p in self.site if p.endswith("-morning.html")]
        self.slug = page_path.removeprefix("/").removesuffix(".html")
        self.date = self.slug.removesuffix("-morning")
        dates = {_local_date(run.started_at), _local_date(run.finished_at)}
        assert self.date in dates, f"digest dated {self.date}, run spanned {dates}"
        self.digest_url = f"https://{harness.WORKER_DOMAINS[0]}/{self.slug}"
        self.articles = {
            r["url"]: r
            for r in run.rows(
                "SELECT url, original_id, state, digest_date, rejection_reason FROM stored_articles"
            )
        }
        self.runs = run.rows(
            "SELECT status, period, dry_run, build_sha, degraded, summary FROM digest_runs"
        )

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
    return harness.run_deployment(tmp_path_factory.mktemp("e2e-digest"), _scenario())


@pytest.fixture(scope="module")
def receipts(run) -> Receipts:
    assert run.returncode == 0, run.diagnostics()
    assert_no_unclaimed(run.records)
    return Receipts(run)


# ---- the record as a whole ----------------------------------------------------


EXPECTED_ROUTES = {
    "d1_query": 28,
    "pages_deployments_list": 1,
    "pages_upload_token": 1,
    "pages_check_missing": 1,
    "pages_upload": 1,
    "pages_upsert_hashes": 1,
    "pages_deployment_create": 1,
    "pages_deployment_get": 0,
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
    "promote_ack": 0,
}

# Derived from the code paths of one first publish, before the suite ever ran:
# load_effective_config, then run_digest, then publish_site and the run record.
EXPECTED_D1 = {
    "schema": 1,
    "SELECT settings": 1,
    "SELECT sources": 1,
    # The newsletter dedup read, then three window loads: two pending, one collected.
    "SELECT stored_articles": 4,
    # Ten articles at 17 columns, five rows under D1's 100 bound parameters.
    "INSERT OR IGNORE INTO stored_articles": 2,
    "UPDATE stored_articles SET score": 1,
    "INSERT OR IGNORE INTO tags": 1,
    "INSERT OR REPLACE INTO article_tags": 1,
    "INSERT OR REPLACE INTO stories": 1,
    "INSERT OR IGNORE INTO story_members": 1,
    "DELETE FROM story_members": 1,
    "DELETE FROM stories": 1,
    "INSERT INTO usage_log": 1,
    # One per verdict: accepted, then rejected.
    "UPDATE stored_articles SET state": 2,
    "INSERT OR REPLACE INTO digests": 1,
    # The archive's file list for the index page, then the manifest the deploy extends.
    "SELECT pages_manifest": 2,
    "SELECT usage_log": 1,
    "SELECT pages_deploy_receipt": 1,
    "INSERT OR IGNORE INTO pages_deploy_receipt": 1,
    "DELETE FROM pages_manifest": 1,
    "INSERT INTO pages_manifest": 1,
    "INSERT INTO digest_runs": 1,
}


def test_the_run_exits_ok_and_reaches_only_the_fakes(run) -> None:
    assert run.returncode == 0, run.diagnostics()
    assert_no_unclaimed(run.records)


def test_every_route_receives_exactly_its_requests(receipts) -> None:
    assert_route_counts(receipts.records, receipts.routes, EXPECTED_ROUTES)


def test_d1_receives_exactly_these_statements(receipts) -> None:
    kinds = [statement_kind(s["sql"]) for s in d1_statements(receipts.records)]
    assert {k: kinds.count(k) for k in kinds} == EXPECTED_D1


def test_every_request_carries_the_credential_its_service_requires(receipts) -> None:
    cf = f"Bearer {harness.CF_TOKEN}"
    jwt = f"Bearer {harness.PAGES_UPLOAD_JWT}"
    worker = f"Bearer {harness.WORKER_TOKEN}"
    expected = {
        "d1_query": ("authorization", cf),
        "pages_deployments_list": ("authorization", cf),
        "pages_upload_token": ("authorization", cf),
        "pages_check_missing": ("authorization", jwt),
        "pages_upload": ("authorization", jwt),
        "pages_upsert_hashes": ("authorization", jwt),
        "pages_deployment_create": ("authorization", cf),
        "workers_domains": ("authorization", cf),
        "email_send": ("authorization", cf),
        "gemini_generate": ("x-goog-api-key", harness.GEMINI_KEY),
        "rss_articles": ("authorization", worker),
        "newsletter_list": ("authorization", worker),
        "newsletter_ack": ("authorization", worker),
        "promote_list": ("authorization", f"Bearer {harness.PROMOTE_TOKEN}"),
    }
    for record in receipts.records:
        if record["route"] in ("discord_webhook", "pages_site"):
            continue  # Discord's credential is the path; the site is public.
        header, value = expected[record["route"]]
        assert record["headers"].get(header) == value, record["route"]
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
    window = datetime.fromisoformat(query["before"]) - datetime.fromisoformat(query["after"])
    assert window == timedelta(hours=WINDOW_HOURS)
    assert query["limit"] == "2000"


def test_no_vote_is_pulled_so_none_is_acked(receipts) -> None:
    pull = only(receipts.records, "promote_list")
    assert (pull["method"], pull["query"]) == ("GET", [])


def _llm_calls(records: list[dict]) -> list[tuple[str, list[str]]]:
    return [
        (r["llm"]["kind"], [title for _, _, title in r["llm"]["articles"]])
        for r in by_route(records, "gemini_generate")
    ]


def test_each_llm_call_names_its_kind_and_the_articles_it_was_given(receipts) -> None:
    calls = by_route(receipts.records, "gemini_generate")
    kinds = [c["llm"]["kind"] for c in calls]
    assert kinds == ["scoring", "cluster", "filter", "summarize", "summarize"]
    given = [frozenset(titles) for _, titles in _llm_calls(receipts.records)]
    assert given[:3] == [
        {TITLE[k] for k in ("a1", "a2", "b1", "b2", "l1", "f1", "f2")},
        {TITLE[k] for k in ("n1", "n2", "n3")},
        {TITLE[k] for k in ("f1", "f2", "n3")},
    ]
    # One call per topic group, in the order the store returns the window.
    assert set(given[3:]) == {
        frozenset(TITLE[k] for k in ("a1", "a2", "b1", "b2")),
        frozenset({TITLE["l1"]}),
    }
    for call in calls:
        ids = [article_id for article_id, _, _ in call["llm"]["articles"]]
        body = json_body(call)
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        if call["llm"]["kind"] == "scoring":
            # Scoring names each article by its stored id, not by position.
            id_by_title = {title: article_id for article_id, _, title in call["llm"]["articles"]}
            assert id_by_title == {
                TITLE[k]: receipts.articles[URL[k]]["original_id"] for k in SCORES
            }
            assert id_by_title[TITLE["a1"]] == "e2e-a1"
            assert "temperature" not in body["generationConfig"]
        else:
            assert ids == [str(i) for i in range(len(ids))]
            assert body["generationConfig"]["temperature"] == 1.0


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
    ]
    assert [(p[0], p[1], p[2], set(p[3:])) for p in updates] == [
        ("accepted", r.date, None, {URL[k] for k in SHOWN}),
        ("rejected", r.date, "filtered", {URL["f2"]}),
    ]
    page = r.page()
    shown_links = set(page.hrefs())
    assert URL["n3"] not in shown_links
    assert {URL[k] for k in SHOWN} <= shown_links


def test_an_article_the_cap_cut_stays_pending_while_every_shown_one_is_accepted(receipts) -> None:
    check_cap_leaves_cut_pending(receipts)


# ---- scripted case 4: a queued newsletter through the real email parser ------


def check_newsletter_reaches_the_digest(r: Receipts) -> None:
    pull = only(r.records, "newsletter_list")
    ack = only(r.records, "newsletter_ack")
    assert pull["seq"] < ack["seq"]
    assert json_body(ack) == {"ids": [LETTER_ITEM_ID]}
    assert LETTER_URL in r.articles, "the letter was stored without its canonical link"
    assert r.articles[LETTER_URL]["state"] == "accepted"
    card = next(c for c in r.page().find_all("article", "featured-item") if LETTER_URL in c.hrefs())
    assert card.one("h3").text() == LETTER_TITLE
    assert LETTERS.name in card.text()


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
    # Top story, Features, In Focus, The Wire, then the stats: Following and On the
    # Radar have nothing in them this issue.
    top, features, focus, wire, stats = receipts.discord()["embeds"]
    for key in FEATURES:
        assert f"[{TITLE[key]}]({URL[key]})" in features["description"], key
    assert CLUSTER["heading"] in focus["description"]
    assert f"[{TITLE['f1']}]({URL['f1']})" in wire["description"]
    assert TITLE["n3"] not in wire["description"]
    assert f"({receipts.digest_url})" in stats["description"]
    # 4 sources, 10 articles received, 7 kept.
    for count in ("**4**", "**10**", "**7**"):
        assert count in stats["description"], count
    assert stats["title"].endswith(receipts.date)
    mail = receipts.mail()
    assert (mail["to"], mail["from"]) == ("reader@e2e.test", "digest@e2e.test")
    assert receipts.date in mail["subject"]
    assert receipts.digest_url in mail["text"]
    assert receipts.digest_url in parse_html(mail["html"]).hrefs()
    for key in SHOWN - {"n1", "n2"}:
        assert TITLE[key] in mail["text"], key
    (row,) = receipts.runs
    assert (row["status"], row["period"], row["dry_run"], row["build_sha"]) == (
        "ok",
        "morning",
        0,
        harness.GIT_SHA,
    )


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
