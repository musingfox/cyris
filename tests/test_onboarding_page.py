"""/onboarding: who is sent to the wizard, its seven steps, and what its six saves write.

The last step starts no write of its own: it starts a run through the app
Worker's `POST /run?period=&date=`, with the key `GET /api/onboarding/first-issue`
picks; that route's own answers are `tests/test_onboarding_first_issue.py`'s.

Each settings step posts the body the settings form posts, to the route
`settings_fields.json` names for that field, and the sources step posts each
picked feed to `POST /api/sources` as the list holds it, less the list's own
`topics`. The votes step's own two routes are `tests/test_onboarding_votes.py`'s;
here it is held to sending only marked cards. The model step saves the provider
and the tuned settings the store lacks, each opened on its starting value, and
Skip on the notifications step turns off what was never set. Vote similarity is
turned on by the first-issue step, with the Model form's body, whether or not a
card was voted on.
What a browser actually sends is checked by `scripts/onboarding_probe.py`; here
the shapes of those bodies are held to the settings page's own, and the stored
sources to the list's entries.
"""

import json
import re
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

import cyris
import cyris.entrypoints
from cyris.adapters.fetch.rss_worker_source import POLL_CONCURRENCY, POLL_FEED_TIMEOUT_SECONDS
from cyris.config import SETTINGS_FIELDS, STARTING_VALUES
from cyris.domain.models import SourceConfig
from cyris.entrypoints.triage_server import (
    TriageServer,
    load_onboarding_feeds,
    load_onboarding_headlines,
    load_onboarding_topics,
    render_onboarding_page,
)

pytestmark = pytest.mark.unit

STATIC = Path(cyris.entrypoints.__file__).parent / "static"
ONBOARDING_JS = (STATIC / "onboarding.js").read_text()
SETTINGS_JS = (STATIC / "settings.js").read_text()
_CATALOG = json.loads((Path(cyris.__file__).parent / "onboarding_feeds.json").read_text())
CATALOG = _CATALOG["feeds"]
TOPICS = _CATALOG["topics"]


def _as_posted(entry: dict) -> dict:
    """What the page posts for a picked entry: the entry without the list's `topics`."""
    return {key: value for key, value in entry.items() if key != "topics"}


# The seven steps, in the order the milestones set: the two that write settings,
# the one that writes sources, the one that writes votes, the two more that write
# settings, the model last so its fold is the final say before a run, and the one
# that starts the first issue.
STEPS = ["language", "schedule", "sources", "votes", "notify", "model", "first-issue"]


class FakeSettings:
    """Stands in for `D1Settings`; records what the wizard decided to store."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def set(self, values: dict) -> None:
        self.calls.append(dict(values))


class FakeSourceStore:
    """Stands in for the D1 `sources` table, empty or not; records every row it was given."""

    def __init__(self, sources: dict[str, SourceConfig]) -> None:
        self.sources = sources
        self.upserts: list[SourceConfig] = []

    def list_sources(self) -> dict[str, SourceConfig]:
        return dict(self.sources)

    def upsert(self, source: SourceConfig) -> None:
        self.upserts.append(source)
        self.sources[source.name] = source


class FailingSourceStore(FakeSourceStore):
    """A `sources` table whose read fails, as a D1 timeout does."""

    def list_sources(self) -> dict[str, SourceConfig]:
        raise RuntimeError("D1 timed out")


A_SOURCE = SourceConfig(name="Feed", url="https://feed.test/rss")


def _server(
    *,
    table: dict | None,
    settings: FakeSettings | None = None,
    store: FakeSourceStore | None = None,
) -> TriageServer:
    """A deployment whose `sources` table is `table`, or one with no table at all (`json`)."""
    return TriageServer(
        settings=settings,
        source_store=store or (None if table is None else FakeSourceStore(table)),
    )


@pytest.fixture
async def serve():
    clients: list[TestClient] = []

    async def start(server: TriageServer) -> TestClient:
        client = TestClient(TestServer(server._app))
        await client.start_server()
        clients.append(client)
        return client

    yield start
    for client in clients:
        await client.close()


class TestWhoSeesTheWizard:
    async def test_an_empty_source_table_sends_settings_to_the_wizard(self, serve) -> None:
        client = await serve(_server(table={}))

        response = await client.get("/settings", allow_redirects=False)

        assert response.status == 302
        assert response.headers["Location"] == "/onboarding"

    async def test_the_wizard_is_served_while_the_table_is_empty(self, serve) -> None:
        client = await serve(_server(table={}))

        response = await client.get("/onboarding")

        assert response.status == 200
        assert response.content_type == "text/html"
        assert 'id="o-steps"' in await response.text()

    async def test_the_wizards_own_exit_opens_settings(self, serve) -> None:
        """Walking the wizard leaves the table as empty as it was, so its exit is marked."""
        client = await serve(_server(table={}))

        response = await client.get("/settings?from=onboarding", allow_redirects=False)

        assert response.status == 200
        assert 'id="model-form"' in await response.text()

    async def test_a_deployment_with_a_source_still_opens_the_wizard(self, serve) -> None:
        """The sources step writes one, and a reload on a later step must not leave the wizard."""
        client = await serve(_server(table={"Feed": A_SOURCE}))

        response = await client.get("/onboarding", allow_redirects=False)

        assert response.status == 200
        assert 'id="o-steps"' in await response.text()

    async def test_the_wizard_stays_open_after_its_sources_step_writes(self, serve) -> None:
        client = await serve(_server(table={}))

        await client.post("/api/sources", json=CATALOG[0])
        response = await client.get("/onboarding", allow_redirects=False)

        assert response.status == 200

    async def test_a_deployment_with_a_source_keeps_its_settings_page(self, serve) -> None:
        client = await serve(_server(table={"Feed": A_SOURCE}))

        assert (await client.get("/settings")).status == 200

    @pytest.mark.parametrize("path", ["/onboarding", "/settings"])
    async def test_a_json_deployment_never_sees_the_wizard(self, serve, path: str) -> None:
        """No writable table is a `json` deployment: its sources live in a file nothing writes."""
        client = await serve(_server(table=None))

        response = await client.get(path, allow_redirects=False)

        assert (response.status, response.headers.get("Location")) == (
            (302, "/settings") if path == "/onboarding" else (200, None)
        )

    async def test_a_failed_source_read_still_serves_settings(self, serve) -> None:
        """/settings is where a reader fixes D1, so routing never depends on reading it."""

        client = await serve(TriageServer(settings=None, source_store=FailingSourceStore({})))

        response = await client.get("/settings", allow_redirects=False)

        assert response.status == 200
        assert 'id="model-form"' in await response.text()


class TestTheFrame:
    @pytest.fixture
    def page(self) -> str:
        return render_onboarding_page()

    def test_the_step_list_names_the_seven_steps_in_order(self, page: str) -> None:
        listed = re.findall(r'<li class="label" data-step="([^"]+)">', page)

        assert listed == STEPS

    def test_every_listed_step_has_a_panel(self, page: str) -> None:
        panels = re.findall(r'<section class="step" data-step="([^"]+)"', page)

        assert panels == STEPS

    def test_the_script_walks_the_same_steps(self) -> None:
        listed = re.search(r"const STEPS = \[([^\]]+)\];", ONBOARDING_JS)
        assert listed, "no STEPS in onboarding.js"

        assert json.loads(f"[{listed[1]}]") == STEPS

    def test_no_step_is_a_placeholder_any_more(self, page: str) -> None:
        assert "data-coming-next" not in page
        assert "Coming next" not in page

    def test_the_wizard_moves_with_back_skip_and_next(self, page: str) -> None:
        nav = re.search(r'<div class="step-nav">(.*?)</div>', page, re.S)
        assert nav, "no step navigation"
        pattern = r'<button class="btn [^"]*" type="button" id="([^"]+)"( hidden)?>([^<]+)<'
        buttons = re.findall(pattern, nav[1])

        assert buttons == [
            ("o-back", "", "Back"),
            ("o-skip", "", "Skip"),
            ("o-next", "", "Next"),
            ("o-start", " hidden", "Start the first issue"),
        ]

    def test_the_last_step_leaves_for_settings_with_the_exit_mark(self, page: str) -> None:
        nav = re.search(r'<div class="step-nav">(.*?)</div>', page, re.S)
        assert nav, "no step navigation"

        finish = '<a class="btn primary" id="o-finish" href="/settings?from=onboarding" hidden>'

        assert f"{finish}Finish</a>" in nav[1]

    def test_only_the_last_step_shows_finish(self) -> None:
        """The frame's own rule, read from the script: Next and Finish never show together."""
        assert '$("o-next").hidden = last;' in ONBOARDING_JS
        assert '$("o-finish").hidden = !last;' in ONBOARDING_JS

    def test_the_page_is_served_with_its_own_script(self, page: str) -> None:
        assert '<script src="/static/onboarding.js"></script>' in page


# The body of the Skip button's click handler.
SKIP_HANDLER = r'\$\("o-skip"\)\.addEventListener\("click", async \(\) => \{(.*?)\n\}\);'


def _body_keys(script: str) -> dict[str, list[set[str]]]:
    """The top-level keys of every body `script` POSTs, by the route it posts them to."""
    bodies: dict[str, list[set[str]]] = {}
    for match in re.finditer(r'post\(\s*"(/api/settings[^"]*)",\s*\{', script):
        route = match[1]
        depth, start = 1, match.end()
        for end in range(start, len(script)):
            depth += {"{": 1, "}": -1}.get(script[end], 0)
            if depth == 0:
                break
        fields, depth = [""], 0
        for char in script[start:end]:
            depth += {"{": 1, "}": -1, "(": 1, ")": -1, "[": 1, "]": -1}.get(char, 0)
            if char == "," and depth == 0:
                fields.append("")
            else:
                fields[-1] += char
        bodies.setdefault(route, []).append(
            {field.split(":")[0].strip() for field in fields if field.strip()}
        )
    return bodies


def _settings_bodies() -> dict[str, list[set[str]]]:
    """The settings page's bodies, its vote-similarity one included.

    That one is built as `const body = {...}` and posted by name, which the
    literal reader cannot see, so it is read from the declaration the post sends.
    """
    bodies = _body_keys(SETTINGS_JS)
    sent = re.search(
        r"const body = \{(.*?)\};\s*show\([^;]*\);\s*try \{\s*"
        r'const data = await post\("/api/settings/vote-similarity", body\)',
        SETTINGS_JS,
        re.S,
    )
    assert sent, "no vote-similarity body in settings.js"
    declared = _body_keys(f'post("/api/settings/vote-similarity", {{{sent[1]}}})')
    return {**bodies, **declared}


# Each field the wizard writes, by the key it writes it under, and the route
# `settings_fields.json` gives that key.
WRITTEN = {
    "digest.output_language": "plain",
    "general.timezone": "plain",
    "general.digest_schedule": "schedule",
    "notify.discord_webhook_url": "notify",
    "notify.email_to": "email",
    "notify.email_from": "email",
    "vote_similarity.enabled": "vote",
    "vote_similarity.provider": "vote",
    "vote_similarity.model": "vote",
    "vote_similarity.max_seeds": "vote",
    "llm_provider.provider": "llm",
    "llm_provider.model": "llm",
    "general.digest_window_hours": "plain",
    "digest.max_articles_per_digest": "plain",
    "routing.score_threshold": "plain",
    "digest.scoring_snippet_length": "plain",
    "digest.summarize_snippet_length": "plain",
    "digest.filter_snippet_length": "plain",
    "digest.style_prompt": "plain",
    "digest.type_scale": "plain",
    "digest.rank_by_preference": "plain",
    "digest.preference_source": "plain",
    "digest.summarize_follow": "plain",
    "digest.vote_half_life_days": "plain",
    "digest.topic_lifetime_days": "plain",
}


class TestItWritesWhatTheSettingsFormWrites:
    def test_the_body_reader_finds_a_bodys_keys(self) -> None:
        script = 'post("/api/settings/email", {email_to: f(a, b), email_from: x["y"]})'

        assert _body_keys(script) == {"/api/settings/email": [{"email_to", "email_from"}]}

    def test_the_settings_reader_finds_the_vote_similarity_body(self) -> None:
        assert _settings_bodies()["/api/settings/vote-similarity"] == [
            {"enabled", "provider", "model", "max_seeds"}
        ]

    def test_the_wizard_posts_to_no_route_the_settings_page_does_not(self) -> None:
        assert set(_body_keys(ONBOARDING_JS)) <= set(_settings_bodies())

    def test_the_wizard_starts_a_run_only_through_the_workers_own_route(self) -> None:
        """The run path any manual run takes; the wizard opens none of its own."""
        start_run = re.search(r"async function startRun.*?\n\}", ONBOARDING_JS, re.S)[0]
        starts = re.findall(r"fetch\(`(/run[^`]*)`", ONBOARDING_JS)

        assert starts == ["/run?${query}"]
        assert (
            "const query = `period=${encodeURIComponent(period)}"
            "&date=${encodeURIComponent(date)}`;" in start_run
        )
        assert (
            'method: "POST"' in re.search(r"async function startRun.*?\n\}", ONBOARDING_JS, re.S)[0]
        )

    def test_the_run_takes_the_key_the_server_picked(self) -> None:
        start = re.search(r"async function startIssue\(\) \{(.*?)\n\}", ONBOARDING_JS, re.S)
        assert start, "no startIssue in onboarding.js"

        assert 'getJson("/api/onboarding/first-issue")' in start[1]
        assert "await startRun(ISSUE.period, ISSUE.date);" in start[1]
        assert start[1].index("planIssue(ISSUE, retry)") < start[1].index("startRun(")

    def test_skipping_the_last_step_leaves_through_finish(self) -> None:
        handler = re.search(SKIP_HANDLER, ONBOARDING_JS, re.S)
        assert handler, "no Skip handler"

        assert '$("o-finish").click();' in handler[1]
        assert "startIssue" not in handler[1] and "startRun" not in handler[1]

    def test_the_wizard_writes_sources_and_votes_only_through_its_own_routes(self) -> None:
        """Besides the settings routes: the form's `POST /api/sources`, the feed check,
        and the votes step's two."""
        routes = set(re.findall(r'post\(\s*"([^"]+)"', ONBOARDING_JS))

        assert routes - set(_body_keys(ONBOARDING_JS)) == {
            "/api/sources",
            "/api/onboarding/feed-check",
            "/api/onboarding/cards",
            "/api/onboarding/votes",
            # The model step's Check, which stores nothing.
            "/api/diagnostics/llm",
        }
        assert "/api/sources" in SETTINGS_JS

    def test_the_wizard_posts_only_the_six_settings_routes(self) -> None:
        assert set(_body_keys(ONBOARDING_JS)) == {
            "/api/settings",
            "/api/settings/values",
            "/api/settings/schedule",
            "/api/settings/vote-similarity",
            "/api/settings/notify",
            "/api/settings/email",
        }

    @pytest.mark.parametrize("route", sorted(_body_keys(ONBOARDING_JS)))
    def test_each_body_is_one_the_settings_page_sends_too(self, route: str) -> None:
        """Same route, same field names: the store cannot tell the two pages apart."""
        wizard = _body_keys(ONBOARDING_JS)[route]
        form = _settings_bodies()[route]

        assert [keys for keys in wizard if keys not in form] == []

    @pytest.mark.parametrize("key", sorted(WRITTEN))
    def test_each_written_key_is_saved_by_its_registry_route(self, key: str) -> None:
        assert SETTINGS_FIELDS[key]["route"] == WRITTEN[key]

    def test_the_hours_are_padded_as_the_settings_form_pads_them(self) -> None:
        padded = 'padStart(2, "0")}:00'

        assert padded in ONBOARDING_JS and padded in SETTINGS_JS

    def test_skipping_a_step_sends_only_what_its_skip_names(self) -> None:
        handler = re.search(SKIP_HANDLER, ONBOARDING_JS, re.S)
        assert handler, "no Skip handler"

        assert "post(" not in handler[1] and "fetch(" not in handler[1]
        assert "const skip = SKIPS[stepOf()];" in handler[1]

    def test_only_the_notifications_skip_saves(self) -> None:
        """A run stops on a notification never set, so leaving the step turns it off."""
        skips = re.search(r"const SKIPS = \{(.*?)\};", ONBOARDING_JS, re.S)
        assert skips, "no SKIPS in onboarding.js"

        assert skips[1].split() == ["notify:", "notifyOffWhereUnset,"]

    def test_skipping_notifications_turns_off_only_what_was_never_set(self) -> None:
        """Through the settings form's own Turn off and empty pair; a stored value stays."""
        off = _function("notifyOffWhereUnset")

        assert 'if (VALUES["notify.discord_webhook_url"] == null) {' in off
        assert 'post("/api/settings/notify", {off: true})' in off
        assert 'VALUES["notify.email_to"] == null || VALUES["notify.email_from"] == null' in off
        assert 'post("/api/settings/email", {email_to: "", email_from: ""})' in off

    def test_next_on_notifications_turns_off_what_it_left_unset(self) -> None:
        save = _function("saveNotify")

        assert save.index("problems.length") < save.index("await notifyOffWhereUnset()")


class TestTheVotesStep:
    """What the votes step's Next sends, read from the script; the browser walk is the probe's."""

    @pytest.fixture
    def save_votes(self) -> str:
        body = re.search(r"async function saveVotes\(\) \{(.*?)\n\}\n", ONBOARDING_JS, re.S)
        assert body, "no saveVotes in onboarding.js"
        return body[1]

    def test_next_on_the_votes_step_saves_the_votes(self) -> None:
        assert "votes: saveVotes," in ONBOARDING_JS

    def test_only_a_marked_card_not_yet_recorded_is_sent(self, save_votes: str) -> None:
        """An untouched card is no signal, and a recorded one is already the reader's."""
        assert "CARDS.filter((card) => card.vote && !card.recorded && !card.gone)" in save_votes

    def test_the_votes_step_leaves_vote_similarity_to_the_first_issue(
        self, save_votes: str
    ) -> None:
        """A Skip here never saves, so a switch kept on this step stayed off on trial1."""
        assert "/api/settings/vote-similarity" not in save_votes

    def test_the_cards_are_dealt_when_the_step_opens(self) -> None:
        assert 'if (step === "votes") loadCards();' in ONBOARDING_JS

    def test_the_step_has_a_loading_line_for_the_wait(self) -> None:
        page = render_onboarding_page()

        assert '<p class="small" id="o-cards-loading" aria-busy="true" hidden>' in page


def _function(name: str) -> str:
    body = re.search(rf"(?:async )?function {name}\(\) \{{(.*?)\n\}}\n", ONBOARDING_JS, re.S)
    assert body, f"no {name} in onboarding.js"
    return body[1]


class TestVoteSimilarityIsOnByTheFirstIssue:
    """On by the end of the wizard, voted or not: later issues' votes count only with it on.

    The milestone has the wizard turn it on without asking the reader, so no
    Skip keeps it off; trial1 skipped the cards and its digest votes did nothing.
    """

    def test_it_waits_on_no_recorded_vote(self) -> None:
        assert "recorded" not in _function("turnOnVoteSimilarity")

    def test_it_is_turned_on_with_the_stored_or_starting_embedder(self) -> None:
        """Which model embeds is the deployer's, under Model or in the model step's fold;
        on an empty store it is the one that step opened on, so the save is not refused."""
        turn_on = _function("turnOnVoteSimilarity")

        assert 'post("/api/settings/vote-similarity"' in turn_on
        assert "enabled: true," in turn_on
        assert "const stored = (key) => VALUES[key] ?? STARTING[key];" in turn_on
        for key in ("provider", "model", "max_seeds"):
            assert f'stored("vote_similarity.{key}")' in turn_on

    def test_the_first_issue_step_turns_it_on_once_there_is_a_source(self) -> None:
        assert "if (data.sources) voteSimilarityOn();" in _function("loadIssue")

    def test_start_waits_for_it_before_the_run(self) -> None:
        """The run reads its settings as it starts, so a switch saved later misses the issue."""
        start = _function("startIssue")

        assert start.index("await voteSimilarityOn();") < start.index("await startRun(")


def _fold(page: str) -> str:
    fold = re.search(r'<details class="more" id="o-advanced" hidden>(.*?)</details>', page, re.S)
    assert fold, "no fold on the model step"
    return fold[1]


class TestTheModelStep:
    """The provider and model, then the tuned settings the store lacks, in one fold.

    What the page sends from a browser is the probe's; here the fold is held to the
    starting values the server sends and to the registry's labels and routes.
    """

    @pytest.fixture
    def page(self) -> str:
        return render_onboarding_page()

    def test_it_comes_right_before_the_first_issue(self) -> None:
        assert STEPS[-2:] == ["model", "first-issue"]

    def test_next_on_the_model_step_saves_it(self) -> None:
        assert "model: saveModel," in ONBOARDING_JS

    def test_the_fold_has_a_field_for_every_starting_value_but_the_switch(self, page) -> None:
        """Vote similarity's switch is the first-issue step's to turn on, so it has no field."""
        keys = re.findall(r'<div class="field" data-key="([^"]+)">', _fold(page))

        assert len(keys) == len(set(keys))
        assert set(keys) == set(STARTING_VALUES) - {"vote_similarity.enabled"}

    def test_each_field_is_labelled_as_settings_labels_it(self, page) -> None:
        fields = re.findall(
            r'<div class="field" data-key="([^"]+)">\s*<(?:label|span) class="label"[^>]*>([^<]+)<',
            _fold(page),
        )

        assert fields and {key: label for key, label in fields} == {
            key: SETTINGS_FIELDS[key]["label"] for key, _ in fields
        }

    def test_the_fold_writes_no_value_of_its_own(self, page) -> None:
        """Every value comes from the server's starting values, so the page holds no second list."""
        fold = _fold(page)

        assert not re.search(r"<(?:input|textarea)[^>]*\bvalue=", fold)
        assert " selected" not in fold and " checked" not in fold
        assert "STARTING = state.starting || {};" in ONBOARDING_JS

    def test_the_fold_is_a_settings_fold_that_names_its_settings(self, page) -> None:
        summary = re.search(r'id="o-advanced" hidden>\s*<summary>([^<]+)</summary>', page)

        assert summary and summary[1].startswith("Advanced: ")

    def test_only_a_setting_the_store_lacks_is_shown(self) -> None:
        draw = re.search(r"function drawTuned\(state\) \{(.*?)\n\}\n", ONBOARDING_JS, re.S)
        assert draw, "no drawTuned in onboarding.js"

        assert "field.hidden = !(key in STARTING);" in draw[1]
        assert "shownTuned()" in _function_with_args("saveModel")

    def test_check_asks_the_provider_and_stores_nothing(self) -> None:
        check = _function("checkModel")

        assert 'post("/api/diagnostics/llm", {provider: p.name, model:' in check
        assert "/api/settings" not in check.replace("/api/diagnostics/llm", "")

    def test_next_saves_the_provider_through_the_model_forms_route(self) -> None:
        assert 'post("/api/settings", {provider: p.name, model})' in _function("saveModel")

    def test_with_no_key_present_the_step_names_every_variable(self) -> None:
        draw = re.search(r"function drawProviders\(stored\) \{(.*?)\n\}\n", ONBOARDING_JS, re.S)
        assert draw, "no drawProviders in onboarding.js"

        assert '$("o-no-key").hidden = keyed.some((p) => p.configured);' in draw[1]
        assert '${keyed.map((p) => p.env_var).join(", ")}' in draw[1]


def _function_with_args(name: str) -> str:
    return _function(name)


class TestTheStepsWriteThroughTheExistingApi:
    """The bodies the wizard sends, stored by the real routes into a fake settings home."""

    async def test_the_language_step_stores_the_language_and_the_timezone(self, serve) -> None:
        settings = FakeSettings()
        client = await serve(_server(table={}, settings=settings))

        response = await client.post(
            "/api/settings/values",
            json={"values": {"digest.output_language": "en", "general.timezone": "Asia/Taipei"}},
        )

        assert response.status == 200
        assert settings.calls == [
            {"digest.output_language": "en", "general.timezone": "Asia/Taipei"}
        ]

    async def test_the_hours_step_stores_the_schedule(self, serve) -> None:
        settings = FakeSettings()
        client = await serve(_server(table={}, settings=settings))

        response = await client.post("/api/settings/schedule", json={"times": ["07:00", "19:00"]})

        assert response.status == 200
        assert settings.calls == [{"general.digest_schedule": ["07:00", "19:00"]}]

    async def test_a_step_stores_nothing_when_the_body_is_refused(self, serve) -> None:
        settings = FakeSettings()
        client = await serve(_server(table={}, settings=settings))

        response = await client.post(
            "/api/settings/values", json={"values": {"general.timezone": "Mars/Base"}}
        )

        assert response.status == 400
        assert (await response.json())["field"] == "general.timezone"
        assert settings.calls == []


class TestTheOpening:
    """The opening plays on any visit with no hash, so its headlines are served to any table."""

    @pytest.mark.parametrize("table", [{}, {"Feed": A_SOURCE}, None])
    async def test_every_deployment_is_sent_the_whole_pool(self, serve, table) -> None:
        client = await serve(_server(table=table))

        response = await client.get("/api/onboarding/headlines")

        assert response.status == 200
        assert await response.json() == {"headlines": load_onboarding_headlines()}

    def test_the_script_reads_the_pool_from_the_server(self) -> None:
        assert 'fetch("/api/onboarding/headlines")' in ONBOARDING_JS


class TestThePickSourcesStep:
    """The list comes from the server, only to an empty table, and a pick is stored as listed."""

    async def test_an_empty_table_is_offered_every_entry_of_the_list(self, serve) -> None:
        client = await serve(_server(table={}))

        response = await client.get("/api/onboarding/feeds")

        assert response.status == 200
        assert await response.json() == {"awaiting": True, "topics": TOPICS, "feeds": CATALOG}

    async def test_a_table_with_a_source_is_offered_no_list(self, serve) -> None:
        client = await serve(_server(table={"Feed": A_SOURCE}))

        response = await client.get("/api/onboarding/feeds")

        assert await response.json() == {"awaiting": False, "topics": [], "feeds": []}

    async def test_a_json_deployment_is_offered_no_list(self, serve) -> None:
        client = await serve(_server(table=None))

        response = await client.get("/api/onboarding/feeds")

        assert await response.json() == {"awaiting": False, "topics": [], "feeds": []}

    async def test_a_failed_source_read_is_an_error_not_a_full_table(self, serve) -> None:
        """Read as "has sources", a failure would tell the reader sources exist."""
        client = await serve(TriageServer(settings=None, source_store=FailingSourceStore({})))

        response = await client.get("/api/onboarding/feeds")

        assert response.status == 500
        assert (await response.json())["error"] == "D1 timed out"

    def test_the_list_is_the_catalog_as_written(self) -> None:
        assert load_onboarding_feeds() == CATALOG
        assert load_onboarding_topics() == TOPICS
        assert len(CATALOG) == 63

    async def test_each_picked_entry_is_stored_as_the_list_holds_it(self, serve) -> None:
        """What the page posts, the entry less `topics`, is stored with its tags and lane."""
        store = FakeSourceStore({})
        client = await serve(_server(table={}, store=store))
        offered = (await (await client.get("/api/onboarding/feeds")).json())["feeds"]
        posted = [_as_posted(entry) for entry in offered]

        for body in posted:
            assert "topics" not in body
            response = await client.post("/api/sources", json=body)
            assert response.status == 200, await response.text()

        assert [
            s.model_dump(mode="json", include=set(body))
            for s, body in zip(store.upserts, posted, strict=True)
        ] == posted
        assert [s.area_id for s in store.upserts] == [None] * len(CATALOG)
        assert {s.type for s in store.upserts} == {"rss"}

    async def test_an_entry_posted_with_its_topics_still_stores_none(self, serve) -> None:
        """Were the page to send `topics`, the stored row would still not carry them."""
        store = FakeSourceStore({})
        client = await serve(_server(table={}, store=store))

        response = await client.post("/api/sources", json=CATALOG[0])

        assert response.status == 200, await response.text()
        assert "topics" not in store.upserts[0].model_dump(mode="json")

    async def test_the_list_is_gone_once_a_pick_is_stored(self, serve) -> None:
        client = await serve(_server(table={}))

        await client.post("/api/sources", json=CATALOG[0])
        response = await client.get("/api/onboarding/feeds")

        assert await response.json() == {"awaiting": False, "topics": [], "feeds": []}

    def test_the_script_posts_each_entry_without_its_topics(self) -> None:
        assert 'post("/api/sources", row.source)' in ONBOARDING_JS
        assert ONBOARDING_JS.count('post("/api/sources"') == 1
        assert "const {topics, ...source} = entry;" in ONBOARDING_JS

    def test_the_script_keeps_the_chosen_topics_for_a_reload(self) -> None:
        """scripts/onboarding_probe.py's reload check clears this key as its sabotage."""
        assert 'const TOPICS_KEY = "cyris-onboarding-topics";' in ONBOARDING_JS

    def test_the_slow_pick_warning_starts_where_the_poll_passes_a_minute(self) -> None:
        """Up to SLOW_PICKS feeds, the Votes step's poll is three batches of 20 seconds."""
        within_a_minute = POLL_CONCURRENCY * (60 // POLL_FEED_TIMEOUT_SECONDS)
        assert f"const SLOW_PICKS = {within_a_minute};" in ONBOARDING_JS

    def test_the_script_reads_the_list_from_the_server(self) -> None:
        assert 'fetch("/api/onboarding/feeds")' in ONBOARDING_JS

    @pytest.mark.parametrize("field", ["name", "url"])
    def test_no_entry_is_written_into_the_page_or_its_script(self, field: str) -> None:
        page = render_onboarding_page()
        written = [e[field] for e in CATALOG if e[field] in page or e[field] in ONBOARDING_JS]

        assert written == []

    @pytest.mark.parametrize("field", ["id", "label"])
    def test_no_topic_is_written_into_the_page_or_its_script(self, field: str) -> None:
        page = render_onboarding_page()
        written = [t[field] for t in TOPICS if t[field] in page or t[field] in ONBOARDING_JS]

        assert written == []

    def test_skipping_the_step_saves_no_pick(self) -> None:
        """Only Next saves; Skip moves on with every checked feed left unwritten."""
        handler = re.search(SKIP_HANDLER, ONBOARDING_JS, re.S)
        assert handler, "no Skip handler"

        assert "SAVES" not in handler[1] and "saveSources" not in handler[1]
        assert "sources: saveSources" in ONBOARDING_JS
