"""A new local install reaches its first issue on a SQLite file, through its own server.

`cyris triage-ui` runs as a subprocess on an empty directory whose `cyris.toml` names the
`sqlite` store and nothing else about it (ADR-0025). The test sends what the reader's
browser sends: first the /settings saves for the keys the wizard has no field for, then
the wizard's own requests in the order `static/onboarding.js` sends them, ending with
`POST /run` and the first-issue poll. Every outgoing request of the server, and of the
`cyris run` it starts, goes through the proxy and its fakes, which count it; the SQLite
file is read afterwards for what landed (docs/spec/e2e-asserts-what-the-fakes-received.md).
"""

import json
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from e2e import harness
from e2e.receipts import assert_no_unclaimed, assert_route_counts, by_route, json_body

from cyris.bootstrap import embedding_defaults
from cyris.config import OPTIONAL_SETTINGS, REQUIRED_KEYS, SETTINGS_FIELDS
from cyris.diagnostics.doctor import EMBEDDING_PROBE_TEXT

pytestmark = pytest.mark.e2e

SETTINGS = harness.settings(
    **{
        "general.timezone": "Asia/Taipei",
        "general.digest_window_hours": 24,
        "llm_provider.provider": "none",
        "vote_similarity.provider": "gemini",
        "vote_similarity.model": "",
    }
)
# The wizard's first step owns these two; every other plain key is a /settings field.
WIZARD_VALUES = ("digest.output_language", "general.timezone")
SETTINGS_VALUES = tuple(
    key
    for key, field in SETTINGS_FIELDS.items()
    if field["route"] == "plain" and key not in WIZARD_VALUES and key not in OPTIONAL_SETTINGS
)

FEED_URL = "https://feeds.e2e.test/rss"
# What the wizard posts for a picked feed: the catalog entry without its `topics`.
SOURCE = {"name": "E2E Local Feed", "url": FEED_URL, "lane": "skim", "tags": ["local"]}
# Hours before the start: the newest three are inside the run's 24-hour window, and the
# oldest three, the older half, are what the votes step deals as cards.
AGES = {"n1": 1, "n2": 2, "n3": 3, "o1": 50, "o2": 60, "o3": 70}
IN_WINDOW = ("n1", "n2", "n3")
EMBED_MODEL = embedding_defaults("gemini")["model"]
POLL_SECONDS = 1

EXPECTED_ROUTES = {
    "d1_query": 0,
    "pages_deployments_list": 0,
    "pages_upload_token": 0,
    "pages_check_missing": 0,
    "pages_upload": 0,
    "pages_upsert_hashes": 0,
    "pages_deployment_create": 0,
    "pages_deployment_get": 0,
    "pages_project_create": 0,
    "pages_site": 0,
    "workers_domains": 0,
    "email_send": 0,
    "discord_webhook": 0,
    "gemini_generate": 0,
    # The vote-similarity save's probe, then the run's seeds and its candidates.
    "gemini_embed": 3,
    "workers_ai_clef": 0,
    "rss_articles": 0,
    "newsletter_list": 0,
    "newsletter_ack": 0,
    "promote_list": 0,
    "promote_ack": 0,
    # Each in-window article is shorter than the summarize window, so its page is asked.
    "article_page": len(IN_WINDOW),
    # The cards, the votes' read-back and the run each poll the feed once.
    "feed_xml": 3,
}


def _items(now: datetime) -> dict[str, dict]:
    return {
        key: {
            "title": f"Local install article {key}",
            "url": f"https://news.e2e.test/posts/{key}",
            "content": f"The {key} article, short enough to be a teaser.",
            "published_at": (now - timedelta(hours=hours)).isoformat(),
        }
        for key, hours in AGES.items()
    }


@dataclass
class Install:
    dir: Path
    items: dict[str, dict]
    # Each request the test sent, in order: (method, path, status, JSON body or None).
    sent: list[tuple[str, str, int, object]]
    cards: dict
    issue: dict
    ended: dict
    page: httpx.Response
    doctor: subprocess.CompletedProcess
    records: list[dict]
    # How many requests the proxy had seen when `cyris doctor` started.
    records_before_doctor: int
    routes: list[str]
    log: str

    def rows(self, sql: str, params: tuple = ()) -> list[dict]:
        return harness._rows(self.dir / harness.LOCAL_SQLITE_PATH, sql, params)

    def diagnostics(self) -> str:
        proxy_log = (self.dir / "proxy.log").read_text(encoding="utf-8", errors="replace")
        return (
            f"\n--- triage-ui and cyris run (tail) ---\n{self.log[-6000:]}"
            f"\n--- proxy log (tail) ---\n{proxy_log[-3000:]}"
        )


class _Browser:
    """Sends a request as the reader's page does, and keeps it for the receipts."""

    def __init__(self, client: httpx.Client, base: str) -> None:
        self._client = client
        self._base = base
        self.sent: list[tuple[str, str, int, object]] = []

    def send(self, method: str, path: str, body: object = None) -> httpx.Response:
        response = self._client.request(method, f"{self._base}{path}", json=body)
        try:
            answer = response.json()
        except ValueError:
            answer = None
        self.sent.append((method, path, response.status_code, answer))
        return response

    def ok(self, method: str, path: str, body: object = None) -> dict:
        response = self.send(method, path, body)
        assert response.status_code == 200, (method, path, response.text)
        return response.json()


def _wait_for_run(browser: _Browser, after: int, log: Path) -> dict:
    """The wizard's poll: the first-issue route until a run newer than `after` is recorded."""
    deadline = time.monotonic() + harness.RUN_SECONDS
    while time.monotonic() < deadline:
        run = browser.ok("GET", "/api/onboarding/first-issue")["run"]
        if run is not None and run["id"] > after:
            return run
        time.sleep(POLL_SECONDS)
    tail = log.read_text(encoding="utf-8", errors="replace")[-6000:]
    raise AssertionError(f"no run was recorded in {harness.RUN_SECONDS}s\n{tail}")


@pytest.fixture(scope="module")
def install(tmp_path_factory) -> Install:
    run_dir = tmp_path_factory.mktemp("e2e-local-install")
    now = datetime.now(UTC)
    items = _items(now)
    config = harness.write_local_install(run_dir)
    scenario = harness.Scenario(
        settings={},
        sources=[],
        rss_rows=[],
        newsletters=[],
        llm={},
        feeds={FEED_URL: list(items.values())},
    )
    with harness.fakes(run_dir, scenario) as (proxy_url, ca_file):
        env = harness.cyris_env(run_dir, proxy_url, ca_file)
        with harness.triage_ui(run_dir, env) as base, harness.local_client() as client:
            browser = _Browser(client, base)
            browser.ok("GET", "/api/settings")
            # /settings: the keys the wizard has no field for, each through its own form.
            browser.ok("POST", "/api/settings", {"provider": "none", "model": ""})
            plain = {k: SETTINGS[k] for k in SETTINGS_VALUES}
            browser.ok("POST", "/api/settings/values", {"values": plain})
            vote = {
                "provider": SETTINGS["vote_similarity.provider"],
                "model": SETTINGS["vote_similarity.model"],
                "max_seeds": SETTINGS["vote_similarity.max_seeds"],
            }
            browser.ok("POST", "/api/settings/vote-similarity", {"enabled": False, **vote})
            browser.ok("POST", "/api/settings/notify", {"off": True})
            browser.ok("POST", "/api/settings/email", {"email_to": "", "email_from": ""})
            # The wizard: language and timezone, the publish hours, a feed, its cards and votes.
            browser.ok(
                "POST", "/api/settings/values", {"values": {k: SETTINGS[k] for k in WIZARD_VALUES}}
            )
            browser.ok(
                "POST", "/api/settings/schedule", {"times": SETTINGS["general.digest_schedule"]}
            )
            browser.ok("GET", "/api/onboarding/feeds")
            browser.ok("POST", "/api/sources", SOURCE)
            cards = browser.ok("POST", "/api/onboarding/cards", {})
            up, down = (card["url"] for card in cards["cards"][:2])
            browser.ok(
                "POST",
                "/api/onboarding/votes",
                {"votes": [{"url": up, "vote": "up"}, {"url": down, "vote": "down"}]},
            )
            # The first-issue step: its plan, vote similarity on, the plan again, the run.
            browser.ok("GET", "/api/onboarding/first-issue")
            browser.ok("POST", "/api/settings/vote-similarity", {"enabled": True, **vote})
            issue = browser.ok("GET", "/api/onboarding/first-issue")
            after = (issue["run"] or {}).get("id", 0)
            started = browser.send("POST", f"/run?period={issue['period']}&date={issue['date']}")
            assert started.status_code == 200, started.text
            ended = _wait_for_run(browser, after, run_dir / "triage-ui.log")
            page = client.get(f"{base}/{issue['date']}-{issue['period']}")
            records_before_doctor = len(harness.read_records(run_dir))
            doctor = subprocess.run(
                [
                    str(harness.VENV_BIN / "cyris"), "doctor",
                    "--config", str(config),
                    "--sources", str(run_dir / "sources.yaml"),
                ],
                cwd=run_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=harness.RUN_SECONDS,
            )  # fmt: skip
    return Install(
        dir=run_dir,
        items=items,
        sent=browser.sent,
        cards=cards,
        issue=issue,
        ended=ended,
        page=page,
        doctor=doctor,
        records=harness.read_records(run_dir),
        records_before_doctor=records_before_doctor,
        routes=json.loads((run_dir / "routes.json").read_text(encoding="utf-8")),
        log=(run_dir / "triage-ui.log").read_text(encoding="utf-8", errors="replace"),
    )


def test_every_request_the_page_sends_is_answered(install) -> None:
    refused = [(m, p, s, b) for m, p, s, b in install.sent if s != 200]
    assert refused == [], install.diagnostics()
    assert install.sent[-1][:2] == ("GET", "/api/onboarding/first-issue")


def test_the_first_issue_ends_ok_and_is_recorded_in_the_sqlite_file(install) -> None:
    assert install.ended["status"] == "ok", install.diagnostics()
    assert install.ended["period"] == install.issue["period"]
    runs = install.rows("SELECT id, status, period, dry_run FROM digest_runs")
    assert runs == [
        {"id": install.ended["id"], "status": "ok", "period": install.issue["period"], "dry_run": 0}
    ]
    issues = install.rows("SELECT date, period FROM digests")
    assert issues == [{"date": install.issue["date"], "period": install.issue["period"]}]


def test_the_issue_page_is_served_by_the_same_server(install) -> None:
    assert install.page.status_code == 200, install.diagnostics()
    assert install.page.headers["content-type"].startswith("text/html")
    for key in IN_WINDOW:
        assert install.items[key]["title"] in install.page.text


def test_the_run_reaches_only_the_fakes_and_each_route_exactly(install) -> None:
    assert_no_unclaimed(install.records)
    assert_route_counts(install.records, install.routes, EXPECTED_ROUTES)


def test_each_feed_read_is_a_plain_get_of_the_feed(install) -> None:
    reads = by_route(install.records, "feed_xml")
    assert [(r["method"], r["host"], r["path"], r["query"]) for r in reads] == [
        ("GET", "feeds.e2e.test", "/rss", [])
    ] * 3


def test_the_embedder_is_probed_then_embeds_the_seeds_and_the_candidates(install) -> None:
    calls = by_route(install.records, "gemini_embed")
    assert [r["path"] for r in calls] == [f"/v1beta/models/{EMBED_MODEL}:batchEmbedContents"] * 3
    assert all(r["headers"]["x-goog-api-key"] == harness.GEMINI_KEY for r in calls)
    probe, seeds, candidates = (r["embed"] for r in calls)
    assert probe == [EMBEDDING_PROBE_TEXT]
    by_url = {item["url"]: item["title"] for item in install.items.values()}
    up, down = (card["url"] for card in install.cards["cards"][:2])
    assert seeds == [by_url[up], by_url[down]]
    assert sorted(candidates) == sorted(install.items[key]["title"] for key in IN_WINDOW)
    for r in calls:
        assert [q["model"] for q in json_body(r)["requests"]] == [f"models/{EMBED_MODEL}"] * len(
            r["embed"]
        )


def test_each_in_window_articles_page_is_asked_once(install) -> None:
    pages = by_route(install.records, "article_page")
    assert sorted(f"https://{r['host']}{r['path']}" for r in pages) == sorted(
        install.items[key]["url"] for key in IN_WINDOW
    )
    for r in pages:
        assert "cyris" in r["headers"]["user-agent"]


def test_the_cards_are_the_older_half_of_the_feed(install) -> None:
    oldest_first = sorted(AGES, key=AGES.get, reverse=True)
    assert [card["url"] for card in install.cards["cards"]] == [
        install.items[key]["url"] for key in oldest_first[:3]
    ]
    assert install.cards["empty"] == []
    assert install.cards["poll_error"] is None


def test_the_settings_and_the_source_live_in_the_sqlite_file(install) -> None:
    stored = {r["key"]: json.loads(r["value"]) for r in install.rows("SELECT * FROM settings")}
    expected = {key: SETTINGS[key] for key in REQUIRED_KEYS}
    expected["vote_similarity.enabled"] = True
    assert stored == expected
    [source] = install.rows("SELECT name, url, type, config FROM sources")
    assert (source["name"], source["url"], source["type"]) == (SOURCE["name"], FEED_URL, "rss")
    config = json.loads(source["config"])
    assert (config["lane"], config["tags"]) == (SOURCE["lane"], SOURCE["tags"])


def test_the_votes_are_human_verdicts_and_the_run_stored_the_window(install) -> None:
    rows = {
        r["url"]: r
        for r in install.rows("SELECT url, state, triaged_at, source_name FROM stored_articles")
    }
    up, down = (card["url"] for card in install.cards["cards"][:2])
    assert rows[up]["state"] == "accepted" and rows[up]["triaged_at"] is not None
    assert rows[down]["state"] == "rejected" and rows[down]["triaged_at"] is not None
    # The card left alone has no row; the window's three came with the run.
    assert set(rows) == {up, down, *(install.items[key]["url"] for key in IN_WINDOW)}
    assert {r["source_name"] for r in rows.values()} == {SOURCE["name"]}
    judged = install.rows("SELECT candidate_url FROM vote_similarity_shadow")
    assert sorted(r["candidate_url"] for r in judged) == sorted(
        install.items[key]["url"] for key in IN_WINDOW
    )


def test_doctor_names_the_sqlite_store_and_its_path_and_asks_cloudflare_nothing(install) -> None:
    out = install.doctor.stdout
    [line] = [line for line in out.splitlines() if "article store" in line]
    assert "article store (sqlite)" in line, out
    assert f"at {harness.LOCAL_SQLITE_PATH}" in line, out
    # Nothing doctor did left this machine: no request reached the proxy while it ran.
    assert len(install.records) == install.records_before_doctor
