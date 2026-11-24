"""The settings web server: /settings, /labels and the APIs they call."""

import ipaddress
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal, get_args

from aiohttp import web
from pydantic import ValidationError

from cyris.adapters.output import html_digest
from cyris.adapters.store.feed_health import QUIET_DAYS, FeedHealth
from cyris.config import SETTINGS_FIELDS, STARTING_VALUES, DigestConfig
from cyris.diagnostics.doctor import probe_discord, probe_embedder
from cyris.domain.models import (
    NEWSLETTER_SOURCE_TYPE,
    Lane,
    SourceConfig,
    SourceType,
    TrackedTopic,
)
from cyris.i18n import (
    ENGLISH,
    Translator,
    accept_language,
    browser_file,
    catalogs,
    resolve,
    translator,
)

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# The grade-D keys stored through the generic values route. The rest need their
# own route: the LLM and the embedder are probed, the schedule and the webhook
# have their own rules.
PLAIN_KEYS: tuple[str, ...] = tuple(
    key for key, field in SETTINGS_FIELDS.items() if field["route"] == "plain"
)
# Why an edition keeps a reader from changing a key: its operator sets it, or the reader's plan
# does not include it (ADR-0043). Each is shown through `lock_reason.<code>`.
LockReason = Literal["operator-set", "plan-limit"]


def _keys_on(route: str) -> tuple[str, ...]:
    """The grade-D keys one write route saves."""
    return tuple(key for key, field in SETTINGS_FIELDS.items() if field["route"] == route)


TEMPLATES_DIR = Path(__file__).parent / "templates"

# One path segment naming a digest page; `/<name>` and `/<name>.html` both serve
# `<name>.html` in the pages directory. No dot, slash or leading dash: nothing that could
# leave that directory or name a hidden file reaches the filesystem.
PAGE_NAME = r"[A-Za-z0-9][A-Za-z0-9_-]*"
PAGE_SUFFIX = ".html"
INDEX_PAGE = "index"
NO_ISSUE_YET = (
    "No issue exists yet. Run `cyris run --period morning` to make the first one, "
    "then reload this page."
)
NO_PAGES_WRITTEN = (
    "This install writes no pages. Set `[html_output] enabled = true` in cyris.toml, restart, "
    "and run `cyris run --period morning`."
)
TYPE_SCALE_KEY = "digest.type_scale"
OUTPUT_LANGUAGE_KEY = "digest.output_language"
# The steps `DigestConfig.type_scale` allows; the app Worker's own list is `type_scale.js`'s.
TYPE_SCALES = tuple(get_args(DigestConfig.model_fields["type_scale"].annotation))
NEUTRAL_TYPE_SCALE = 1
HEAD_END = b"</head>"


def type_scale_style(scale: float) -> str:
    """The `<style>` the app Worker injects (`workers/app/src/type_scale.js` `typeScaleStyle`)."""
    return f"<style>html:root{{--type-scale:{scale}}}</style>"


def _accepts_empty(key: str) -> bool:
    from cyris.config import validate_setting

    try:
        validate_setting(key, "")
    except ValueError:
        return False
    return True


def _shows_starting_select(key: str) -> bool:
    """A required select that has a starting value: the page shows it while the key is absent.

    An optional select, or one with no starting value, shows "Not set" and is not sent.
    """
    field = SETTINGS_FIELDS[key]
    return bool(field.get("select")) and not field.get("optional") and key in STARTING_VALUES


def sent_as_shown() -> list[str]:
    """The keys a save sends as the page shows them while the home lacks them: an empty
    field's answer, and a select's starting value.

    The hosted Worker answers the list `scripts/build_shared_settings.py` builds from this.
    """
    return [key for key in SETTINGS_FIELDS if _accepts_empty(key) or _shows_starting_select(key)]


def _refused(error: str, field: str) -> web.Response:
    """A 400 whose `field`, a settings key or a source field, is the one the page marks."""
    return web.json_response({"ok": False, "error": error, "field": field}, status=400)


def _label(t: Translator, key: str) -> str:
    return t(f"settings_field.{key}")


def _fields(t: Translator) -> dict[str, dict[str, Any]]:
    """The registry as the pages read it: each field's structure, and its label in `t`'s words."""
    return {key: {**field, "label": _label(t, key)} for key, field in SETTINGS_FIELDS.items()}


def _unknown_source_type(t: Translator, value: object) -> str:
    return t("settings_api.source_type_unknown", type=repr(value))


def _probe_refusal(probe) -> str:
    """What the check found, then what to do about it."""
    return "\n".join(part for part in (probe.detail, probe.fix) if part)


def _values_note(t: Translator, keys: list[str]) -> str:
    """When the saved keys take effect: a live key reaches the pages, the rest the next run."""
    live = [_label(t, key) for key in keys if SETTINGS_FIELDS[key].get("live")]
    if not live:
        return t("settings_api.effective_next_run")
    if len(live) == len(keys):
        return t("settings_api.pages_within_a_minute")
    return t("settings_api.some_within_a_minute", labels=_list(t, live))


def _list(t: Translator, items: list[str]) -> str:
    """Names in `t`'s words for a list: "A, B, C"."""
    head, *rest = items
    for item in rest:
        head = t("settings.list_more", head=head, item=item)
    return head


def _invalid_json(t: Translator) -> web.Response:
    return web.json_response({"ok": False, "error": t("settings_api.invalid_json")}, status=400)


def _no_table(error: str) -> web.Response:
    """409 for a write this deployment's store has no table for."""
    return web.json_response({"ok": False, "error": error}, status=409)


def _feed_problem(t: Translator, health: FeedHealth, problem: str) -> str:
    """One finding of `FeedHealth.findings`, in `t`'s words."""
    if problem == "failing":
        count = health.consecutive_failures
        if health.error_line:
            return t("settings_api.feed_failing_error", count=count, error=health.error_line)
        return t("settings_api.feed_failing", count=count)
    if problem == "never_stored":
        return t("settings_api.feed_never_stored")
    return t("settings_api.feed_quiet", days=QUIET_DAYS)


def _health_json(t: Translator, health: FeedHealth | None, now: datetime) -> dict[str, Any] | None:
    """A feed's poll record, with the problems `cyris doctor` would name; None when not polled."""
    if health is None:
        return None
    return {
        "consecutive_failures": health.consecutive_failures,
        "last_error": health.last_error,
        "last_failed_at": health.last_failed_at,
        "last_ok_at": health.last_ok_at,
        "newest_article_at": health.newest_article_at,
        "problems": [_feed_problem(t, health, problem) for problem in health.findings(now)],
    }


def _topic_refusal(t: Translator, field: str) -> str | None:
    """A tracked-topic field's refusal, in the page's words; None for a field with none."""
    refusals = {
        "name": t("settings_api.topic_needs_name"),
        "description": t("settings_api.topic_needs_description"),
        "threshold": t("settings_api.topic_threshold_range"),
        "model": t("settings_api.topic_needs_model"),
    }
    return refusals.get(field)


# The keys `static/settings.js` formats beyond those listed with the other pages' below.
SETTINGS_SCRIPT_MESSAGES: tuple[str, ...] = (
    "settings.access_not_set_up",
    "settings.answered_without_reason",
    "settings.checking_embedder",
    "settings.checking_provider",
    "settings.confirm_remove",
    "settings.confirm_retire",
    "settings.confirm_turn_off",
    "settings.editing",
    "settings.hours_need",
    "settings.hours_not_saved",
    "settings.hours_saved",
    "settings.item_saved",
    "settings.key_missing",
    "settings.key_ready",
    "settings.list_last",
    "settings.list_more",
    "settings.load_settings_failed",
    "settings.load_sources_failed",
    "settings.load_topics_failed",
    "settings.missing_notice",
    "settings.model_hint_call",
    "settings.model_hint_none",
    "settings.model_hint_pick",
    "settings.new_source",
    "settings.new_topic",
    "settings.no_key_needed",
    "settings.no_newsletter_sources",
    "settings.no_rss_sources",
    "settings.no_settings_store",
    "settings.no_source_table",
    "settings.no_sources",
    "settings.no_topic_table",
    "settings.no_topics",
    "settings.not_saved",
    "settings.not_set",
    "settings.pick_provider_first",
    "settings.remove",
    "settings.removing",
    "settings.retire",
    "settings.retiring",
    "settings.saved_value",
    "settings.saved_values",
    "settings.saving",
    "settings.sending_test",
    "settings.session_expired",
    "settings.source_retired",
    "settings.topic_other_model",
    "settings.topic_removed",
    "settings.turn_off",
    "settings.turning_off",
    "settings.unreachable",
    "settings.value_empty",
    *(f"source_type.{source_type}" for source_type in get_args(SourceType)),
    *(f"lock_reason.{reason}" for reason in get_args(LockReason)),
)
# Every catalog key a container page script formats, all pages' scripts together:
# `tests/test_catalog_coverage.py` holds it equal to what those scripts call `t` with.
BROWSER_MESSAGES: tuple[str, ...] = (
    *SETTINGS_SCRIPT_MESSAGES,
    "settings.checking_discord",
    "provider.none",
    *(f"lane.{lane}" for lane in Lane),
    "labels.signed_out",
    "labels.access_not_set_up",
    "labels.refused",
    "labels.refused_because",
    "labels.unanswered",
    "labels.progress",
    "labels.no_sample",
    "labels.all_answered",
    "labels.not_drawn",
    "labels.not_loaded",
    "onboarding.added",
    "onboarding.added_some",
    "onboarding.added_some_next",
    "onboarding.adding",
    "onboarding.answered",
    "onboarding.building",
    "onboarding.cards_empty",
    "onboarding.cards_failed",
    "onboarding.cards_no_sources",
    "onboarding.cards_none",
    "onboarding.cards_poll_failed",
    "onboarding.checking_provider",
    "onboarding.checking_url",
    "onboarding.elapsed",
    "onboarding.email_to_needed",
    "onboarding.feed_count",
    "onboarding.feed_refused",
    "onboarding.feeds_load_failed",
    "onboarding.fetch_failed_for",
    "onboarding.fetching_cards",
    "onboarding.gone",
    "onboarding.hour_needed",
    "onboarding.issue_built",
    "onboarding.issue_published",
    "onboarding.kept",
    "onboarding.key_missing",
    "onboarding.key_ready",
    "settings_option.model_default",
    "onboarding.model_hint_check",
    "onboarding.model_hint_none",
    "onboarding.model_hint_pick",
    "onboarding.no_key",
    "onboarding.no_key_needed",
    "onboarding.no_reason",
    "onboarding.no_reason_reload",
    "onboarding.not_set",
    "onboarding.own_added",
    "onboarding.own_listed",
    "onboarding.paste_first",
    "onboarding.pick_provider",
    "onboarding.picked",
    "onboarding.picked_slow",
    "onboarding.plan_no_sources",
    "onboarding.plan_published",
    "onboarding.plan_started",
    "onboarding.plan_today",
    "onboarding.plan_yesterday",
    "onboarding.progress",
    "onboarding.recorded",
    "onboarding.recorded_votes",
    "onboarding.recording",
    "onboarding.run_ended_as",
    "onboarding.run_error",
    "onboarding.run_failed_next",
    "onboarding.run_no_articles",
    "onboarding.run_no_pending",
    "onboarding.run_not_started",
    "onboarding.run_publish_failed",
    "onboarding.run_timed_out",
    "onboarding.saved_advanced",
    "onboarding.saved_embedder",
    "onboarding.saved_hours",
    "onboarding.saved_value",
    "onboarding.saved_values",
    "onboarding.saving",
    "onboarding.sending_test",
    "onboarding.session_expired",
    "onboarding.settings_load_failed",
    "onboarding.settings_not_loaded",
    "onboarding.start",
    "onboarding.starting_run",
    "onboarding.state_added",
    "onboarding.state_not_added",
    "onboarding.try_again",
    "onboarding.unreachable",
    "onboarding.vote_down",
    "onboarding.vote_on",
    "onboarding.vote_similarity_off",
    "onboarding.vote_up",
    "onboarding.votes_gone",
    "onboarding.your_feeds",
)
# One file per catalog, under `/static/` so the app Worker forwards it to this server,
# which is what serves `/static/` in both editions. A page names its own in a meta tag;
# the route registering this path is a literal, so the Worker's route table can read it.
BROWSER_MESSAGES_URL = "/static/i18n/{tag}.json"


def _render_page(template: str, language: str) -> str:
    """Render one of this server's pages, worded in `language`, with the digest pages' site bar.

    The pages live outside `static/` so no unrendered copy is served, and the
    environment also reads the digest templates, where the site bar partial is.
    """
    env = html_digest.environment(language, TEMPLATES_DIR)
    url = BROWSER_MESSAGES_URL.format(tag=env.globals["ui_lang"])
    return env.get_template(template).render(i18n_catalog=url)


def _etag_matches(if_none_match: str, etag: str) -> bool:
    """Whether an `If-None-Match` header names `etag`, by RFC 9110 §13.1.2's weak comparison."""
    tags = {tag.strip() for tag in if_none_match.split(",")}
    return "*" in tags or etag in tags or f"W/{etag}" in tags


def render_settings_page(language: str = ENGLISH) -> str:
    return _render_page("settings.html.j2", language)


def render_labels_page(language: str = ENGLISH) -> str:
    return _render_page("labels.html.j2", language)


def render_onboarding_page(language: str = ENGLISH) -> str:
    return _render_page("onboarding.html.j2", language)


# The answers /labels takes; a skip is kept on the sample alone.
LABELS = ("up", "down", "skip")

# Where the onboarding wizard leaves, and the mark its own exit carries. A
# deployment that skipped the pick-sources step finishes the wizard with the
# same empty `sources` table it started with, and /settings would send it
# straight back; this marker is what lets it through once.
ONBOARDING_PATH = "/onboarding"
ONBOARDING_EXIT_MARK = ("from", "onboarding")
# A vote card's two answers, as the digest's own votes name them: "up" is
# Interested and is written accepted, "down" is Not interested, rejected.
VOTES = ("up", "down")
# What a digest or raw page's vote button may post to /api/vote, as the promote Worker takes
# it (`VOTES` in workers/promote/src/index.js): "deep" is the deep-read button of the pages
# published 2026-07-31 to 2026-08-06, and no vote at all is an older page's, which meant it.
# `workers/app/test/vote_cases.json` holds both editions to the same answers.
PAGE_VOTES = ("up", "down", "deep")
NO_PAGE_VOTE = "deep"


def _onboarding_catalog() -> dict[str, Any]:
    raw = (files("cyris") / "onboarding_feeds.json").read_text(encoding="utf-8")
    return json.loads(raw)


def load_onboarding_feeds() -> list[dict[str, Any]]:
    """The feed picker's entries, as `onboarding_feeds.json` holds them (grade A).

    Sent to the page as written: each entry is a `SourceConfig` body plus the
    list's own `topics`, which the page strips before posting the rest to
    `POST /api/sources` unchanged, tags and lane included.
    """
    return _onboarding_catalog()["feeds"]


def load_onboarding_topics() -> list[dict[str, str]]:
    """The topics the feed picker groups its entries by, in the order it shows them.

    Each is its id alone; its label is the UI catalogs' `onboarding_topic.<id>`.
    """
    return _onboarding_catalog()["topics"]


def load_onboarding_headlines() -> list[dict[str, str]]:
    """The opening's sample headlines, as `onboarding_headlines.json` holds them (grade A)."""
    raw = (files("cyris") / "onboarding_headlines.json").read_text(encoding="utf-8")
    return json.loads(raw)["headlines"]


async def _revalidate_static(request: web.Request, response: web.StreamResponse) -> None:
    """Make the browser ask before reusing a page's CSS or JS.

    `add_static` sends an ETag and a Last-Modified but no Cache-Control, so a
    browser falls back to heuristic freshness and pairs a new page with the
    stylesheet it cached before the release. `no-cache` still lets it keep the
    file: each use is a conditional request, answered 304 while the ETag holds.
    """
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"


# Set by the app Worker's own code on the `ui` instance it starts, and by nothing else
# (`docs/architecture.md` §5). Exactly this value turns the check below off; absent, or
# anything else, leaves it on.
BEHIND_APP_WORKER_ENV = "CYRIS_BEHIND_APP_WORKER"
BEHIND_APP_WORKER_VALUE = "true"

# The Host names this server answers on whatever it was bound to, in the form a
# browser sends them. A rebinding page's Host is its own domain, so it is not here.
LOOPBACK_HOST_NAMES = frozenset({"127.0.0.1", "localhost", "[::1]"})
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# `same-site` too: another port of this machine is the same site and another origin.
REFUSED_FETCH_SITES = frozenset({"cross-site", "same-site"})
DEFAULT_PORTS = {"http": 80, "https": 443}

# A name or a bracketed IPv6 address, then a port. The characters left out are what
# would make one header name two hosts, such as `127.0.0.1@evil.example`.
_AUTHORITY = re.compile(r"(\[[0-9a-f:.]+\]|[^\[\]:/?#@\\\s]+)(?::(\d*))?", re.IGNORECASE)


def _accepted_host_names(bound: str) -> frozenset[str]:
    """Loopback, and the address given to `--host` unless it names every address.

    `0.0.0.0` and `::` say where to listen, not what this server is called. A browser on
    Linux or macOS reaches this machine at `http://0.0.0.0:8766`, so accepting the name
    would leave a page one more way in.
    """
    try:
        address = ipaddress.ip_address(bound.strip("[]"))
    except ValueError:
        return LOOPBACK_HOST_NAMES | ({bound.lower()} if bound else set())
    if address.is_unspecified:
        return LOOPBACK_HOST_NAMES
    return LOOPBACK_HOST_NAMES | {f"[{address}]" if address.version == 6 else str(address)}


def _authority(value: str) -> tuple[str, int | None] | None:
    """A Host value's lowercase name and its port; None when it is not one."""
    match = _AUTHORITY.fullmatch(value)
    if match is None:
        return None
    name, port = match.groups()
    return name.lower(), int(port) if port else None


def _same_origin(origin: str, host: tuple[str, int | None], scheme: str) -> bool:
    """Whether an Origin header's host and port are this request's Host's."""
    origin_scheme, separator, rest = origin.partition("://")
    named = _authority(rest) if separator else None
    if named is None:
        return False
    return (named[0], named[1] or DEFAULT_PORTS.get(origin_scheme.lower())) == (
        host[0],
        host[1] or DEFAULT_PORTS.get(scheme),
    )


def _forbidden(error: str) -> web.Response:
    return web.json_response({"ok": False, "error": error}, status=403)


def _refuse_foreign_requests(bound: str) -> Callable[..., Awaitable[web.StreamResponse]]:
    """The middleware that holds every route to this server's own Host and Origin.

    A browser sends a cross-site `text/plain` POST without asking first, and a page on a
    rebound domain reads this server as its own. Neither carries anything a handler
    could tell from the reader's own page, so this runs before all of them, static
    files and unrouted paths included. A client that sends no `Origin` and no
    `Sec-Fetch-Site`, such as curl, is not a browser carrying someone else's page.
    """
    accepted = _accepted_host_names(bound)

    @web.middleware
    async def refuse_foreign_requests(
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        # The header itself: `request.host` falls back to this machine's own name.
        raw_host = request.headers.get("Host", "")
        host = _authority(raw_host)
        if host is None or host[0] not in accepted:
            logger.warning("Refused %s %s: Host %r", request.method, request.path, raw_host)
            return _forbidden(
                f"Refused: this server answers on a loopback address or its --host address, "
                f"and this request's Host is {raw_host!r}."
            )
        if request.method not in READ_METHODS:
            origin = request.headers.get("Origin")
            if origin is not None and not _same_origin(origin, host, request.scheme):
                logger.warning("Refused %s %s: Origin %r", request.method, request.path, origin)
                return _forbidden(
                    f"Refused: a write from Origin {origin!r} cannot reach {raw_host}, "
                    f"because only a page this server serves may write to it."
                )
            site = request.headers.get("Sec-Fetch-Site", "").lower()
            if site in REFUSED_FETCH_SITES:
                logger.warning(
                    "Refused %s %s: Sec-Fetch-Site %r", request.method, request.path, site
                )
                return _forbidden(
                    f"Refused: the browser marked this write Sec-Fetch-Site {site!r}, "
                    f"and only a page this server serves may write to it."
                )
        return await handler(request)

    return refuse_foreign_requests


class TriageServer:
    """Serves /settings, /onboarding, /labels, /interest and the settings, sources, labels
    and build APIs."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8766,
        settings=None,
        values: dict[str, Any] | None = None,
        sources: dict[str, SourceConfig] | None = None,
        source_store=None,
        feed_health=None,
        tracked_topics: list[TrackedTopic] | None = None,
        topic_store=None,
        blind_labels=None,
        article_store=None,
        issue_topics=None,
        onboarding_articles=None,
        feed_checker=None,
        digest_store=None,
        run_log=None,
        interest_reader=None,
        clock=None,
        behind_app_worker: bool = False,
        store_is_local: bool = False,
        html_dir: Path | None = None,
        start_run: Callable[[str | None, str | None], None] | None = None,
        locked: Mapping[str, LockReason] | None = None,
    ) -> None:
        self._host = host
        # The grade-D keys this edition keeps the reader from changing, each with its reason
        # (ADR-0043): `GET /api/settings` names them and every write route refuses them. No
        # server `cyris triage-ui` starts locks a key; the shared settings cases do.
        self._locked = dict(locked or {})
        for key, reason in self._locked.items():
            if key not in SETTINGS_FIELDS or reason not in get_args(LockReason):
                raise ValueError(f"cannot lock {key!r} for {reason!r}")
        # Where `HtmlDigestWriter` wrote the archive; None serves no page. The app Worker
        # sends these paths to Pages, so only a local `cyris triage-ui` ever answers them.
        self._html_dir = html_dir
        # The app Worker checked the Access JWT and forwards the original URL, so a Host
        # here is not an address of this server. Off only when told so: a deployment
        # that sets nothing gets the check.
        self._behind_app_worker = behind_app_worker
        self._port = port
        # Without a settings store the page still renders, read-only. The store
        # is optional for a partial server.
        self._settings = settings
        # The grade-D values the deployment's home holds, by `table.field`; a key
        # absent here is missing. Each successful save updates it.
        self._values = dict(values or {})
        # Already resolved by `load_effective_config`: the `sources` table, empty or not.
        self._sources = sources or {}
        # The write surface; optional for a partial server, whose list stays read-only.
        self._source_store = source_store
        # D1's `feed_health` beside each source's newest article; optional for a
        # partial server.
        self._feed_health = feed_health
        # Like sources: the startup list, read-only without the store, which is
        # optional for a partial server.
        self._tracked_topics = tracked_topics or []
        self._topic_store = topic_store
        # D1's blind-label sample, and the store its up and down answers are
        # written to as votes; both optional for a partial server.
        self._blind_labels = blind_labels
        self._article_store = article_store
        # D1's `topics` and `topic_votes` (a `TopicRepository`, not the tracked topics above):
        # a page vote on an item an issue put under a topic is also a vote on that topic.
        # Optional; without it a vote stamps its article and nothing more.
        self._issue_topics = issue_topics
        # The pages' votes are taken here only on a local `cyris triage-ui` whose store is
        # the local SQLite file. Behind the app Worker the Worker answers /api/vote itself and
        # queues the vote for the run (`docs/architecture.md` §6). On D1 a vote stays the
        # cloud's: each write would block this loop on D1's REST round trips, and a later
        # `promote-sync` would stamp an older queued vote over it at the sync's time (ADR-0044).
        self._takes_votes = article_store is not None and store_is_local and not behind_app_worker
        # The wizard's card reader, over the run's own RSS edge
        # (`bootstrap.build_onboarding_articles`); optional for a partial server.
        self._onboarding_articles = onboarding_articles
        # The wizard's check of a feed the reader pastes
        # (`bootstrap.build_feed_checker`); optional for a partial server.
        self._feed_checker = feed_checker
        # D1's stored issues and `digest_runs`, which the wizard's first issue
        # reads its key's state and its run's outcome from; both optional for a
        # partial server.
        self._digest_store = digest_store
        self._run_log = run_log
        # D1's voted tags and live holds, which the interest map is drawn from on each
        # request (`D1InterestReader.read`); optional for a partial server.
        self._interest_reader = interest_reader
        # The time the first issue's key is picked at; a probe pins it.
        self._clock = clock or (lambda: datetime.now(UTC))
        # Starts `cyris run` apart from this server, given the period and the issue's date
        # (either may be None). Behind the app Worker `POST /run` never reaches here: the
        # Worker answers it and starts its own run container.
        self._start_run = start_run
        # Rendered per request in the language `_ui_language` picks, so a saved language
        # takes effect on the next request; each takes the catalog tag.
        self._settings_page: Callable[[str], str] = render_settings_page
        self._labels_page: Callable[[str], str] = render_labels_page
        self._onboarding_page: Callable[[str], str] = render_onboarding_page
        self._app = web.Application(
            middlewares=[] if behind_app_worker else [_refuse_foreign_requests(host)]
        )
        self._app.router.add_get("/api/build", self._handle_build)
        if self._takes_votes:
            # Every method, as the app Worker's `handleVote` answers every method.
            self._app.router.add_route("*", "/api/vote", self._handle_vote)
        else:
            self._app.router.add_get("/api/vote", self._handle_vote_probe)
        self._app.router.add_get("/api/settings", self._handle_get_settings)
        self._app.router.add_post("/api/settings", self._handle_post_settings)
        self._app.router.add_post("/api/diagnostics/llm", self._handle_diagnose_llm)
        self._app.router.add_post("/api/settings/schedule", self._handle_post_schedule)
        self._app.router.add_post("/api/settings/values", self._handle_post_values)
        self._app.router.add_post("/api/settings/vote-similarity", self._handle_post_vote)
        self._app.router.add_post("/api/settings/notify", self._handle_post_notify)
        self._app.router.add_post("/api/settings/email", self._handle_post_email)
        self._app.router.add_get("/api/sources", self._handle_get_sources)
        self._app.router.add_post("/api/sources", self._handle_post_source)
        self._app.router.add_delete("/api/sources/{name}", self._handle_delete_source)
        self._app.router.add_get("/api/onboarding/feeds", self._handle_get_onboarding_feeds)
        self._app.router.add_get("/api/onboarding/headlines", self._handle_get_headlines)
        self._app.router.add_post("/api/onboarding/feed-check", self._handle_post_feed_check)
        self._app.router.add_post("/api/onboarding/cards", self._handle_post_onboarding_cards)
        self._app.router.add_post("/api/onboarding/votes", self._handle_post_onboarding_votes)
        self._app.router.add_get("/api/onboarding/first-issue", self._handle_get_first_issue)
        if start_run is not None and not behind_app_worker:
            self._app.router.add_post("/run", self._handle_post_run)
        self._app.router.add_get("/api/topics", self._handle_get_topics)
        self._app.router.add_post("/api/topics", self._handle_post_topic)
        self._app.router.add_delete("/api/topics/{name}", self._handle_delete_topic)
        self._app.router.add_get("/api/labels", self._handle_get_labels)
        self._app.router.add_post("/api/labels", self._handle_post_label)
        self._app.router.add_get("/settings", self._handle_settings_page)
        self._app.router.add_get("/onboarding", self._handle_onboarding_page)
        self._app.router.add_get("/labels", self._handle_labels_page)
        self._app.router.add_get("/interest", self._handle_interest_page)
        # Production never reaches this: the app Worker sends /favicon.svg to Pages.
        # A local `cyris triage-ui` has no Pages behind it.
        self._app.router.add_get("/favicon.svg", self._handle_favicon)
        # aiohttp tries the longest path prefix first and then registration order, so
        # this route answers `/static/i18n/` and add_static never looks in STATIC_DIR for it.
        self._app.router.add_get("/static/i18n/{tag}.json", self._handle_browser_messages)
        self._app.router.add_static("/static", STATIC_DIR)
        # A fixed path outranks these in aiohttp's resolver, so a page cannot take a route's path.
        self._app.router.add_get("/", self._handle_index)
        self._app.router.add_get(f"/{{name:{PAGE_NAME}}}", self._handle_page)
        self._app.router.add_get(f"/{{name:{PAGE_NAME}}}{PAGE_SUFFIX}", self._handle_page)
        self._app.on_response_prepare.append(_revalidate_static)
        self._runner: web.AppRunner | None = None

    async def _handle_build(self, request: web.Request) -> web.Response:
        """Which image this deployment starts, asked without waiting for a run.

        Cloudflare documents no way to read the image a Worker version
        references and injects no image identity into a running container, so
        the deployment saying so itself is the only answer there is. Waking this
        instance is what makes the question answerable on demand: the `run` role
        only speaks at its cron hours, and a deployment that has not run yet
        would otherwise be indistinguishable from one that never deployed.

        An empty sha is a real answer, not a missing one — a local `docker
        build` with no `--build-arg` produces exactly that.
        """
        return web.json_response({"git_sha": os.environ.get("CYRIS_GIT_SHA", "")})

    async def _handle_get_settings(self, request: web.Request) -> web.Response:
        """Every runtime setting and which are missing, and what this machine could switch to."""
        from cyris.adapters.notify import mask_discord_webhook_url
        from cyris.bootstrap import (
            EMBEDDING_ENV,
            clef_credentials,
            default_models,
            embedding_defaults,
        )
        from cyris.config import GRADE_D_KEYS, LLMProviderConfig
        from cyris.service_layer.prompts import _language_names

        values = {key: self._values.get(key) for key in GRADE_D_KEYS}
        webhook = values["notify.discord_webhook_url"]
        if webhook:
            values["notify.discord_webhook_url"] = mask_discord_webhook_url(webhook)
        providers = []
        models = default_models()
        for name in models:
            # Constructing it is what resolves the key from the environment, so
            # `configured` reflects what a run would actually find, not a guess.
            probe_cfg = LLMProviderConfig(provider=name, model="")
            ready = bool(probe_cfg.api_key) and (
                bool(probe_cfg.account_id) if name in ("workers_ai", "ai_gateway") else True
            )
            providers.append(
                {
                    "name": name,
                    "env_var": probe_cfg.api_key_env_var,
                    "default_model": models[name],
                    "configured": ready,
                }
            )
        # Not from provider_defaults.json: "none" has no model, and anything read
        # from that file is treated as a provider that has one.
        providers.append({"name": "none", "env_var": "", "default_model": "", "configured": True})
        embedding_providers = [
            {
                "name": name,
                "env_var": env[0],
                "default_model": embedding_defaults(name)["model"],
                "configured": all(os.environ.get(var) for var in env),
            }
            for name, env in EMBEDDING_ENV.items()
        ]
        return web.json_response(
            {
                "values": values,
                "fields": _fields(self._translator(request)),
                "missing": sorted(key for key in GRADE_D_KEYS if key not in self._values),
                "sent_as_shown": sent_as_shown(),
                # What the wizard pre-fills for each key it may write, only while the home
                # lacks it: a stored key is never offered, so the wizard cannot write over it.
                "starting": {
                    key: value for key, value in STARTING_VALUES.items() if key not in self._values
                },
                "providers": providers,
                "embedding_providers": embedding_providers,
                # Whether a run could call Clef; the page disables it as a judge if not.
                "clef_ready": clef_credentials() is not None,
                # Each tag with the name languages.json gives it, in the file's order:
                # the pages offer them as a select's options.
                "languages": [
                    {"tag": tag, "name": name} for tag, name in _language_names().items()
                ],
                "writable": self._settings is not None,
                # Each key this edition locks, and why: the page disables its controls and
                # shows the reason (ADR-0043).
                "locked": {key: {"reason": reason} for key, reason in self._locked.items()},
            }
        )

    def _refuse_locked(self, t: Translator, keys: Iterable[str]) -> web.Response | None:
        """The 400 for the first of `keys` this edition locks, or None when it locks none."""
        for key in keys:
            if (reason := self._locked.get(key)) is not None:
                why = t(f"lock_reason.{reason}")
                return _refused(t("settings_api.locked", label=_label(t, key), reason=why), key)
        return None

    def _ui_language(self, request: web.Request) -> str:
        """The catalog this deployment's own pages and errors are worded from.

        The saved `digest.output_language`; until one is saved, the browser's `Accept-Language`.
        """
        saved = self._values.get(OUTPUT_LANGUAGE_KEY)
        if saved:
            return resolve(saved)
        return accept_language(request.headers.get("Accept-Language", "")) or ENGLISH

    def _translator(self, request: web.Request) -> Translator:
        """The `t` this request's answer is worded with: its pages' language."""
        return translator(self._ui_language(request))

    async def _settings_body(self, request: web.Request) -> dict | web.Response:
        """The JSON object a settings write sent, or the response refusing it."""
        if self._settings is None:
            t = self._translator(request)
            return web.json_response(
                {"ok": False, "error": t("error.no_settings_store")}, status=409
            )
        try:
            body = await request.json()
        except Exception:
            body = None
        if not isinstance(body, dict):
            return _invalid_json(self._translator(request))
        return body

    def _store(self, values: dict[str, Any]) -> web.Response | None:
        """Write `values` to the settings store and this server's copy; the 500 if D1 refuses."""
        try:
            self._settings.set(values)
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            return web.json_response({"ok": False, "error": str(e)}, status=500)
        self._values.update(values)
        return None

    async def _handle_post_settings(self, request: web.Request) -> web.Response:
        """Validate against the live provider, then write the D1 settings row.

        The order is the whole point. A bad provider or a mistyped model saved
        here would not surface until the next scheduled digest, hours later and
        after the fetch — so nothing is stored until a real call comes back.
        See `cyris.diagnostics.doctor.probe_llm`.
        """
        from pydantic import ValidationError

        from cyris.config import LLMProviderConfig
        from cyris.diagnostics.doctor import probe_llm

        body = await self._settings_body(request)
        if isinstance(body, web.Response):
            return body

        t = self._translator(request)
        if (refused := self._refuse_locked(t, _keys_on("llm"))) is not None:
            return refused
        provider = (body.get("provider") or "").strip()
        model = (body.get("model") or "").strip()
        try:
            candidate = LLMProviderConfig(
                provider=provider,
                model=model,
                reasoning_effort=self._values.get("llm_provider.reasoning_effort", "default"),
            )
        except ValidationError:
            return _refused(
                t("settings_api.unknown_provider", provider=repr(provider)), "llm_provider.provider"
            )

        if provider == "none":
            # Nothing to call, and a leftover model must not outlive its provider.
            model = ""
            detail = t("settings.model_hint_none")
        else:
            probe = await probe_llm(candidate)
            if probe.status != "ok":
                return _refused(_probe_refusal(probe), "llm_provider.model")
            detail = probe.detail

        chosen = {"llm_provider.provider": provider, "llm_provider.model": model}
        if (refused := self._store(chosen)) is not None:
            return refused

        logger.info("LLM provider set to %s · %s", provider, model or "(default model)")
        return web.json_response(
            {
                "ok": True,
                "provider": provider,
                "model": model,
                "detail": detail,
                # This server holds no LLM of its own; every run resolves settings
                # fresh, so the change lands on the next digest.
                "note": t("settings_api.saved_next_run"),
            }
        )

    async def _handle_diagnose_llm(self, request: web.Request) -> web.Response:
        """Probe a provider from this instance's egress, and store nothing.

        The Worker's `/api/diagnostics/gemini` proves the Worker's path; providers
        refused the Container's, which leaves from somewhere else.
        """
        from pydantic import ValidationError

        from cyris.config import LLMProviderConfig
        from cyris.diagnostics import doctor

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        provider = (body.get("provider") or "").strip()
        model = (body.get("model") or "").strip()
        try:
            candidate = LLMProviderConfig(
                provider=provider,
                model=model,
                reasoning_effort=self._values.get("llm_provider.reasoning_effort", "default"),
            )
        except ValidationError:
            return web.json_response(
                {"ok": False, "error": f"unknown provider {provider!r}"}, status=400
            )

        probe = await doctor.probe_llm(candidate)
        ok = probe.status == "ok"
        return web.json_response(
            {
                "ok": ok,
                "provider": provider,
                "model": model,
                "detail": probe.detail,
                "egress": await doctor.probe_egress(),
            },
            status=200 if ok else 502,
        )

    async def _handle_post_schedule(self, request: web.Request) -> web.Response:
        """Set the two digest hours. The cron tick is hourly and reads this."""
        from cyris.service_layer.schedule import validate_schedule

        body = await self._settings_body(request)
        if isinstance(body, web.Response):
            return body
        locked = self._refuse_locked(self._translator(request), _keys_on("schedule"))
        if locked is not None:
            return locked

        try:
            times = validate_schedule([str(t).strip() for t in body.get("times") or []])
        except ValueError as e:
            return _refused(str(e), "general.digest_schedule")

        if (refused := self._store({"general.digest_schedule": times})) is not None:
            return refused

        logger.info("Digest schedule set to %s", ", ".join(times))
        t = self._translator(request)
        return web.json_response(
            {"ok": True, "times": times, "note": t("settings_api.effective_next_tick")}
        )

    async def _handle_post_values(self, request: web.Request) -> web.Response:
        """Store any of the plain settings, all or nothing, each through its own rule."""
        from cyris.config import LLMProviderConfig, validate_setting
        from cyris.diagnostics.doctor import probe_llm

        body = await self._settings_body(request)
        if isinstance(body, web.Response):
            return body
        t = self._translator(request)

        values = body.get("values")
        if not isinstance(values, dict) or not values:
            return web.json_response(
                {"ok": False, "error": t("settings_api.values_empty")}, status=400
            )
        validated = {}
        for key, value in values.items():
            if key not in SETTINGS_FIELDS:
                return web.json_response(
                    {"ok": False, "error": t("settings_api.not_a_setting", key=repr(key))},
                    status=400,
                )
            if (refused := self._refuse_locked(t, [key])) is not None:
                return refused
            if key not in PLAIN_KEYS:
                return _refused(t("settings_api.own_form", label=_label(t, key)), key)
            try:
                validated[key] = validate_setting(key, value)
            except ValueError as e:
                return _refused(t("settings_api.refused", label=_label(t, key), reason=str(e)), key)

        stored_provider = self._values.get("llm_provider.provider")
        if "llm_provider.reasoning_effort" in validated and stored_provider not in (
            None,
            "",
            "none",
        ):
            # A model before Claude 4.6 rejects an effort, and a run would find out.
            candidate = LLMProviderConfig(
                provider=stored_provider,
                model=self._values.get("llm_provider.model") or "",
                reasoning_effort=validated["llm_provider.reasoning_effort"],
            )
            probe = await probe_llm(candidate)
            if probe.status != "ok":
                return _refused(_probe_refusal(probe), "llm_provider.reasoning_effort")

        if (refused := self._store(validated)) is not None:
            return refused

        logger.info("Settings saved: %s", ", ".join(sorted(validated)))
        note = _values_note(t, list(validated))
        # The catalog this deployment's pages are now worded from, so a page worded
        # from another reloads in it (ADR-0027).
        ui_lang = self._ui_language(request)
        return web.json_response(
            {"ok": True, "values": validated, "note": note, "ui_lang": ui_lang}
        )

    async def _handle_post_vote(self, request: web.Request) -> web.Response:
        """Store vote similarity's switch, embedder and seeds as one unit.

        Turning it on checks the embedder with one real call first, and this is
        the only writer of `enabled`, so no run meets an unverified embedder.
        Off stores without a call: a deployment with no embedding key must still
        be able to complete its settings.
        """
        from cyris.bootstrap import EMBEDDING_ENV
        from cyris.config import validate_setting

        body = await self._settings_body(request)
        if isinstance(body, web.Response):
            return body
        t = self._translator(request)
        if (refused := self._refuse_locked(t, _keys_on("vote"))) is not None:
            return refused

        values = {}
        for field in ("enabled", "provider", "model", "max_seeds"):
            key = f"vote_similarity.{field}"
            if field not in body:
                return _refused(t("settings_api.required", label=_label(t, key)), key)
            try:
                values[key] = validate_setting(key, body[field])
            except ValueError as e:
                return _refused(t("settings_api.refused", label=_label(t, key), reason=str(e)), key)

        if values["vote_similarity.enabled"]:
            provider = values["vote_similarity.provider"]
            probe = await probe_embedder(provider, values["vote_similarity.model"])
            if probe.status != "ok":
                # A missing key is the provider's to fix, the way the page marks it.
                keyed = all(os.environ.get(var) for var in EMBEDDING_ENV[provider])
                field = "vote_similarity.model" if keyed else "vote_similarity.provider"
                return _refused(_probe_refusal(probe), field)
            detail = probe.detail
        else:
            detail = t("settings_api.vote_not_checked")

        if (refused := self._store(values)) is not None:
            return refused

        logger.info(
            "Vote similarity saved: %s", "on" if values["vote_similarity.enabled"] else "off"
        )
        return web.json_response(
            {
                "ok": True,
                "values": values,
                "detail": detail,
                "note": t("settings_api.saved_next_run"),
            }
        )

    async def _handle_post_notify(self, request: web.Request) -> web.Response:
        """Store a Discord webhook only after Discord confirms it exists.

        `{"off": true}` stores "" instead: turning notifications off is its own
        action, so clearing the field by accident cannot stop them.
        """
        from cyris.adapters.notify import mask_discord_webhook_url

        body = await self._settings_body(request)
        if isinstance(body, web.Response):
            return body
        t = self._translator(request)
        if (refused := self._refuse_locked(t, _keys_on("notify"))) is not None:
            return refused

        if body.get("off") is True:
            if (refused := self._store({"notify.discord_webhook_url": ""})) is not None:
                return refused
            logger.info("Discord notifications turned off")
            return web.json_response(
                {
                    "ok": True,
                    "discord_webhook_url": "",
                    "note": t("settings_api.notifications_off"),
                }
            )

        url = (body.get("discord_webhook_url") or "").strip()
        if not url:
            return _refused(t("settings_api.webhook_empty"), "notify.discord_webhook_url")

        probe = await probe_discord(url)
        if probe.status != "ok":
            return _refused(_probe_refusal(probe), "notify.discord_webhook_url")

        if (refused := self._store({"notify.discord_webhook_url": url})) is not None:
            return refused

        logger.info("Discord webhook saved")
        return web.json_response(
            {
                "ok": True,
                "discord_webhook_url": mask_discord_webhook_url(url),
                "detail": probe.detail,
                "note": t("settings_api.saved_next_run"),
            }
        )

    async def _handle_post_email(self, request: web.Request) -> web.Response:
        """Store the mail addresses only after a test message reaches the recipient.

        An empty recipient turns mail off and sends nothing. Unlike the webhook,
        both addresses are shown in full, so clearing one by accident is undone
        by typing it again: off needs no separate action.
        """
        from cyris.adapters.mail import send_mail

        body = await self._settings_body(request)
        if isinstance(body, web.Response):
            return body
        t = self._translator(request)
        if (refused := self._refuse_locked(t, _keys_on("email"))) is not None:
            return refused

        recipient = (body.get("email_to") or "").strip()
        sender = (body.get("email_from") or "").strip()
        values = {"notify.email_to": recipient, "notify.email_from": sender}

        if not recipient:
            if (refused := self._store(values)) is not None:
                return refused
            logger.info("Digest mail turned off")
            return web.json_response({"ok": True, **values, "note": t("settings_api.mail_off")})

        if not sender:
            return _refused(t("settings_api.mail_needs_sender"), "notify.email_from")

        account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
        token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
        if not (account_id and token):
            return web.json_response(
                {
                    "ok": False,
                    "error": t("settings_api.mail_needs_cloudflare"),
                },
                status=400,
            )

        try:
            status = await send_mail(
                account_id,
                token,
                sender,
                recipient,
                t("settings_api.test_mail_subject"),
                t("settings_api.test_mail_body"),
            )
        except RuntimeError as e:
            return web.json_response(
                {"ok": False, "error": t("settings_api.test_mail_failed", error=str(e))}, status=400
            )

        if (refused := self._store(values)) is not None:
            return refused

        logger.info("Digest mail addresses saved")
        return web.json_response(
            {
                "ok": True,
                **values,
                "detail": t("settings_api.test_mail_sent", status=status, recipient=recipient),
                "note": t("settings_api.mail_saved"),
            }
        )

    async def _handle_get_sources(self, request: web.Request) -> web.Response:
        """What the pipeline is actually fetching.

        `email_match` rides along because it is source data (grade D);
        Cloudflare Email Routing is grade B and stays in the dashboard.
        """
        t = self._translator(request)
        sources = self._effective_sources()
        health = self._read_feed_health()
        now = datetime.now(UTC)
        return web.json_response(
            {
                "writable": self._source_store is not None,
                "sources": [
                    {
                        "name": s.name,
                        "type": s.type,
                        "lane": s.lane.value,
                        "url": s.url,
                        "email_match": s.email_match,
                        "homepage": s.homepage,
                        "tags": s.tags,
                        "health": _health_json(t, health.get(s.name), now),
                    }
                    for s in sources.values()
                ],
            }
        )

    def _read_feed_health(self) -> dict[str, FeedHealth]:
        """Each polled feed's health; none when it cannot be read, so the list still loads."""
        if self._feed_health is None:
            return {}
        try:
            return self._feed_health.read()
        except Exception:  # noqa: BLE001 - health is an annotation, never the list
            logger.warning("Could not read feed_health", exc_info=True)
            return {}

    def _effective_sources(self) -> dict[str, SourceConfig]:
        """The live table when there is one, empty included; else the startup list."""
        if self._source_store is None:
            return self._sources
        return self._source_store.list_sources()

    async def _handle_post_source(self, request: web.Request) -> web.Response:
        """Add or edit one source, over the row `name` owns."""
        t = self._translator(request)
        if self._source_store is None:
            return _no_table(t("settings_api.no_source_table"))
        try:
            body = await request.json()
        except Exception:
            return _invalid_json(t)

        try:
            source = SourceConfig.model_validate(body)
        except ValidationError as e:
            if e.errors()[0]["loc"] == ("type",):
                return _refused(_unknown_source_type(t, body["type"]), "type")
            # One line, in the body's own field names: the page sends only values
            # this model accepts, so whoever reads this wrote the request.
            reasons = "; ".join(
                f"{'.'.join(map(str, err['loc'])) or 'body'}: {err['msg']}" for err in e.errors()
            )
            return _refused(reasons, ".".join(map(str, e.errors()[0]["loc"][:1])))
        if not source.name.strip():
            return _refused(t("settings_api.source_needs_name"), "name")
        if not source.fetchable:
            needs = {
                "rss": (t("settings_api.rss_needs_url"), "url"),
                NEWSLETTER_SOURCE_TYPE: (t("settings_api.newsletter_needs_sender"), "email_match"),
            }
            return _refused(*needs[source.type])

        try:
            self._source_store.upsert(source)
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            return web.json_response({"ok": False, "error": str(e)}, status=500)

        logger.info("Source %s written to D1", source.name)
        note = t("settings_api.effective_next_run")
        return web.json_response({"ok": True, "name": source.name, "note": note})

    async def _handle_get_onboarding_feeds(self, request: web.Request) -> web.Response:
        """The wizard's feed list, offered only while the `sources` table is empty.

        Once a deployment has a source, more are added one at a time under
        Sources in /settings, so `topics` and `feeds` are empty and the step
        says so instead.
        """
        if self._source_store is None:
            return web.json_response({"awaiting": False, "topics": [], "feeds": []})
        # Read here rather than through _awaiting_onboarding(), which reads a
        # failure as "not waiting": the page would then claim sources that do not exist.
        try:
            awaiting = not self._effective_sources()
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            logger.warning("Could not read the sources table for onboarding", exc_info=True)
            return web.json_response({"ok": False, "error": str(e)}, status=500)
        t = translator(self._ui_language(request))
        topics = [
            {"id": topic["id"], "label": t(f"onboarding_topic.{topic['id']}")}
            for topic in load_onboarding_topics()
        ]
        return web.json_response(
            {
                "awaiting": awaiting,
                "topics": topics if awaiting else [],
                "feeds": load_onboarding_feeds() if awaiting else [],
            }
        )

    async def _handle_get_headlines(self, request: web.Request) -> web.Response:
        """The opening's sample headlines, whatever the `sources` table holds.

        The opening plays on any visit with no hash, a deployment with sources
        included, so unlike the feed list this is not tied to an empty table.
        """
        return web.json_response({"headlines": load_onboarding_headlines()})

    async def _handle_post_feed_check(self, request: web.Request) -> web.Response:
        """Check a feed the reader pasted, or the feed a pasted home page links; writes nothing.

        `taken` is what the page already lists, as `{name, url}`; the sources
        table is added to it here. The answer's `name` is the feed's own title,
        made distinct from every taken name, and `url` is the feed's address,
        which the page then posts to `POST /api/sources` with the rest.
        """
        from cyris.adapters.fetch.feed_check import FeedCheckError

        if self._feed_checker is None:
            return web.json_response(
                {"ok": False, "error": "This deployment has no wizard to add feeds from."},
                status=409,
            )
        try:
            body = await request.json()
        except Exception:
            body = None
        url = body.get("url") if isinstance(body, dict) else None
        listed = body.get("taken", []) if isinstance(body, dict) else None
        if (
            not isinstance(url, str)
            or not isinstance(listed, list)
            or not all(
                isinstance(t, dict)
                and isinstance(t.get("name"), str)
                and isinstance(t.get("url") or "", str)
                for t in listed
            )
        ):
            return web.json_response(
                {"ok": False, "error": 'The body is {"url", "taken": [{"name", "url"}]}.'},
                status=400,
            )
        taken = [(t["name"], t.get("url") or "") for t in listed]
        try:
            taken += [(s.name, s.url or "") for s in self._effective_sources().values()]
        except Exception:  # noqa: BLE001 - the page's own list still guards the names
            logger.warning("Could not read the sources table for a feed check", exc_info=True)
        try:
            feed = await self._feed_checker.check(url, taken)
        except FeedCheckError as e:
            t = translator(self._ui_language(request))
            return web.json_response(
                {"ok": False, "error": t(f"feed_check.{e.code}", **e.fields), "name": e.name},
                status=409 if e.name else 422,
            )
        logger.info("Feed check passed: %s", feed.url)
        return web.json_response({"ok": True, "name": feed.name, "url": feed.url})

    def _no_card_reader(self) -> web.Response | None:
        """The refusal of a deployment the vote cards cannot read or write for, else None."""
        if self._onboarding_articles is None or self._article_store is None:
            return web.json_response(
                {
                    "ok": False,
                    "error": "This deployment has no article store to vote into "
                    '(store backend is neither "sqlite" nor "d1").',
                },
                status=409,
            )
        return None

    async def _handle_post_onboarding_cards(self, request: web.Request) -> web.Response:
        """Fetch the reader's feeds now and deal the vote cards; writes nothing of its own.

        A POST because, with the rss Worker, it polls first, which fills the
        buffer the first run reads. `empty` names each RSS source the read found
        no article for, counted by `source_name` in the read itself.
        """
        from cyris.adapters.fetch.onboarding_cards import draw_cards, rss_sources
        from cyris.service_layer.degrade import excerpt

        if (refused := self._no_card_reader()) is not None:
            return refused
        try:
            feeds = rss_sources(self._effective_sources())
            read = await self._onboarding_articles.refresh(feeds, lambda: datetime.now(UTC))
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            logger.warning("Onboarding cards could not be read", exc_info=True)
            return web.json_response(
                {"ok": False, "error": f"Could not read the feeds: {e}"}, status=502
            )
        draw = draw_cards(read.articles, feeds)
        return web.json_response(
            {
                "ok": True,
                "sources": len(feeds),
                "cards": [
                    {
                        "url": a.url,
                        "title": a.title,
                        "source": a.source_name,
                        "excerpt": excerpt(a.content),
                        "published_at": a.published_at.isoformat(),
                    }
                    for a in draw.cards
                ],
                "empty": draw.empty,
                "poll_error": read.poll_error,
            }
        )

    async def _handle_post_onboarding_votes(self, request: web.Request) -> web.Response:
        """Write the cards the reader marked, as `/labels` writes an answer: in the container.

        Each voted article is read again through the same RSS edge the cards
        came from, stored as fetched, and only then voted on, because `accept`
        and `reject` update existing rows only. `record_votes` stamps
        `triaged_at`: the press was a person's. A card left alone is not sent,
        so nothing of it is written, and it stays a candidate for the first run.
        """
        from cyris.adapters.fetch.onboarding_cards import rss_sources
        from cyris.adapters.promotions import record_votes

        if (refused := self._no_card_reader()) is not None:
            return refused
        try:
            body = await request.json()
        except Exception:
            body = None
        votes = body.get("votes") if isinstance(body, dict) else None
        if (
            not isinstance(votes, list)
            or not votes
            or not all(
                isinstance(v, dict) and isinstance(v.get("url"), str) and v.get("vote") in VOTES
                for v in votes
            )
        ):
            return web.json_response(
                {
                    "ok": False,
                    "error": 'votes is a non-empty list of {"url", "vote"}, '
                    f"vote one of {', '.join(VOTES)}",
                },
                status=400,
            )
        by_url = {v["url"]: v["vote"] for v in votes}
        if len(by_url) != len(votes):
            return web.json_response(
                {"ok": False, "error": "Each article is voted on once."}, status=400
            )

        now = datetime.now(UTC)
        try:
            feeds = rss_sources(self._effective_sources())
            read = await self._onboarding_articles.read(feeds, now)
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            return web.json_response(
                {"ok": False, "error": f"Could not read the feeds: {e}"}, status=502
            )
        found = {a.url: a for a in read}
        missing = [url for url in by_url if url not in found]
        # A card that left its feed since it was dealt (the cards are each feed's
        # oldest) drops alone: the reader's other votes still count.
        by_url = {url: vote for url, vote in by_url.items() if url in found}
        if not by_url:
            return web.json_response(
                {
                    "ok": False,
                    "error": "These articles are no longer in the feeds, so no vote was "
                    "recorded. Reload the page for fresh cards.",
                    "missing": missing,
                },
                status=409,
            )

        accepted = [url for url, vote in by_url.items() if vote == "up"]
        rejected = [url for url, vote in by_url.items() if vote == "down"]
        try:
            self._article_store.save([found[url] for url in by_url], now)
            record_votes(self._article_store, accepted=accepted, rejected=rejected, at=now)
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            logger.warning("Onboarding votes failed", exc_info=True)
            return web.json_response({"ok": False, "error": str(e)}, status=500)

        logger.info("Onboarding votes: %d up, %d down", len(accepted), len(rejected))
        return web.json_response(
            {"ok": True, "accepted": accepted, "rejected": rejected, "missing": missing}
        )

    async def _handle_get_first_issue(self, request: web.Request) -> web.Response:
        """What the wizard's last step needs to start the first issue and wait for it.

        `date` and `period` are the key a run started now can take without a
        tick later today storing over its issue (`first_issue_key`): today's
        latest publish hour already begun, or yesterday's evening before both.
        `today` is the deployment's own date, for the page to say which it is.
        `published` says an issue already holds that key, which a new run would
        replace. `run` is the newest recorded run, so the page can tell the one
        it starts by its id. The run itself is started by `POST /run?period=&date=`,
        as any manual run is: the app Worker's, or this server's own on a local install.
        """
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        from cyris.service_layer.schedule import first_issue_key, validate_schedule

        if self._run_log is None or self._digest_store is None or self._source_store is None:
            return web.json_response(
                {
                    "ok": False,
                    "error": "This deployment keeps no run log to wait on "
                    '(store backend is neither "sqlite" nor "d1").',
                },
                status=409,
            )
        timezone = self._values.get("general.timezone")
        times = self._values.get("general.digest_schedule")
        try:
            now = self._clock().astimezone(ZoneInfo(str(timezone)))
            date, period = first_issue_key(now, times)
            hours = validate_schedule(times)
        except (ValueError, TypeError, ZoneInfoNotFoundError) as e:
            return web.json_response(
                {
                    "ok": False,
                    "error": f"The publish hours or the timezone cannot be read ({e}). "
                    "Fix them on the first two steps, then come back.",
                },
                status=409,
            )
        try:
            sources = len(self._effective_sources())
            published = (date, period) in self._digest_store.issues()
            run = self._run_log.newest()
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            logger.warning("Could not read the first issue's state", exc_info=True)
            return web.json_response({"ok": False, "error": str(e)}, status=500)
        return web.json_response(
            {
                "ok": True,
                "date": date,
                "period": period,
                "today": now.strftime("%Y-%m-%d"),
                "published": published,
                "sources": sources,
                "timezone": timezone,
                "hours": hours,
                "run": run,
            }
        )

    async def _handle_post_run(self, request: web.Request) -> web.Response:
        """A manual run on a local install: the app Worker's `POST /run`, answered here.

        The same query, refusals and answer as `workers/app/src/router.js` and
        `startRun` in `workers/app/src/index.js`, so the wizard's last step works
        unchanged. The run is `cyris run` started apart from this server, as the
        Worker starts its run container, and this answers once it has started.
        """
        from cyris.service_layer.schedule import PERIOD_ORDER

        period = request.query.get("period")
        date = request.query.get("date")
        if period is None:
            # `cyris run --if-due` dates its issue by the run's own clock.
            if date is not None:
                return web.json_response({"error": "date needs a period"}, status=400)
        elif period not in PERIOD_ORDER:
            return web.json_response(
                {"error": f"period must be one of {', '.join(PERIOD_ORDER)}"}, status=400
            )
        elif date is not None and (refused := self._refuse_run_date(date)) is not None:
            status, error = refused
            return web.json_response({"error": error}, status=status)
        try:
            self._start_run(period, date)
        except OSError as e:
            logger.warning("Could not start cyris run", exc_info=True)
            return web.json_response({"error": f"could not start cyris run: {e}"}, status=500)
        at = self._clock().astimezone(UTC).isoformat(timespec="milliseconds")
        return web.json_response({"started": "run", "at": at.replace("+00:00", "Z")})

    def _refuse_run_date(self, date: str) -> tuple[int, str] | None:
        """Why a manual run's issue date is refused, as (status, error), or None to take it.

        `refuseRunDate` in `workers/app/src/router.js`: a real YYYY-MM-DD, today or
        yesterday in the deployment's time zone. Yesterday is the onboarding's first
        issue before the morning hour; anything older would store over an issue
        already published. A time zone that cannot be read refuses rather than takes.
        """
        from datetime import date as calendar_date
        from datetime import timedelta
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            real = re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) and calendar_date.fromisoformat(date)
        except ValueError:
            real = None
        if not real:
            return 400, "date must be a real YYYY-MM-DD date"
        timezone = self._values.get("general.timezone")
        try:
            today = self._clock().astimezone(ZoneInfo(str(timezone))).date()
        except (ValueError, ZoneInfoNotFoundError):
            timezone = None
        if not timezone:
            return 503, "could not read the deployment's time zone to check the date; try again"
        yesterday = today - timedelta(days=1)
        if real in (today, yesterday):
            return None
        return 400, f"date must be today or yesterday in {timezone} ({yesterday} or {today})"

    async def _handle_delete_source(self, request: web.Request) -> web.Response:
        t = self._translator(request)
        if self._source_store is None:
            return _no_table(t("settings_api.no_source_table"))
        name = request.match_info["name"]
        try:
            # The last source may go: an empty table is a deployment waiting for
            # onboarding, and a run with none ends without fetching anything.
            self._source_store.delete(name)
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            return web.json_response({"ok": False, "error": str(e)}, status=500)

        logger.info("Source %s retired", name)
        note = t("settings_api.effective_next_run")
        return web.json_response({"ok": True, "name": name, "note": note})

    def _embedding_model(self) -> str:
        """The model a run embeds with now, which a new topic's threshold is set for."""
        from cyris.bootstrap import embedding_model

        provider = self._values.get("vote_similarity.provider")
        if not provider:
            return ""
        return embedding_model(provider, self._values.get("vote_similarity.model") or "")

    async def _handle_get_topics(self, request: web.Request) -> web.Response:
        """The topics the next run tracks, and the model it will judge them with."""
        topics = (
            self._tracked_topics if self._topic_store is None else self._topic_store.list_topics()
        )
        return web.json_response(
            {
                "writable": self._topic_store is not None,
                "topics": [topic.model_dump() for topic in topics],
                "embedding_model": self._embedding_model(),
            }
        )

    async def _handle_post_topic(self, request: web.Request) -> web.Response:
        """Add or edit one topic, over the row `name` owns. Nothing is probed: a
        topic that cannot be embedded is skipped by the run and named in its summary."""
        t = self._translator(request)
        if self._topic_store is None:
            return _no_table(t("settings_api.no_topic_table"))
        try:
            body = await request.json()
        except Exception:
            return _invalid_json(t)

        try:
            topic = TrackedTopic.model_validate(body)
        except ValidationError as e:
            first = e.errors()[0]
            field = str(first["loc"][0]) if first["loc"] else ""
            return _refused(_topic_refusal(t, field) or f"body: {first['msg']}", field)

        try:
            self._topic_store.upsert(topic)
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            return web.json_response({"ok": False, "error": str(e)}, status=500)

        logger.info("Tracked topic %s written to D1", topic.name)
        note = t("settings_api.effective_next_run")
        return web.json_response({"ok": True, "name": topic.name, "note": note})

    async def _handle_delete_topic(self, request: web.Request) -> web.Response:
        t = self._translator(request)
        if self._topic_store is None:
            return _no_table(t("settings_api.no_topic_table"))
        name = request.match_info["name"]
        try:
            removed = self._topic_store.delete(name)
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            return web.json_response({"ok": False, "error": str(e)}, status=500)
        if not removed:
            return web.json_response(
                {"ok": False, "error": t("settings_api.no_topic", name=name)}, status=404
            )

        logger.info("Tracked topic %s removed", name)
        note = t("settings_api.effective_next_run")
        return web.json_response({"ok": True, "name": name, "note": note})

    def _no_sample(self) -> web.Response:
        return web.json_response(
            {
                "ok": False,
                "error": "Blind labels live in the SQL store: this deployment's [store] backend "
                'is neither "sqlite" nor "d1".',
            },
            status=409,
        )

    def _deck(self) -> dict[str, Any]:
        """Progress and the next item, as the page shows it: nothing of the pipeline's verdict."""
        from cyris.service_layer.degrade import excerpt

        answered, total = self._blind_labels.progress()
        card = self._blind_labels.next_card()
        item = None
        if card is not None:
            item = {
                "url": card.url,
                "title": card.title,
                "source": card.source,
                "excerpt": excerpt(card.content),
            }
        return {"answered": answered, "total": total, "item": item}

    async def _handle_get_labels(self, request: web.Request) -> web.Response:
        if self._blind_labels is None:
            return self._no_sample()
        try:
            return web.json_response(self._deck())
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def _handle_post_label(self, request: web.Request) -> web.Response:
        """Answer one open item. The sample decides: the answer is kept there first, by a
        write that succeeds only on an unanswered item, and only its winner writes the
        vote to the article. A vote that fails releases the answer, so the item stays
        open rather than kept with no vote behind it. A skip goes to the sample alone."""
        from cyris.adapters.promotions import record_votes

        if self._blind_labels is None:
            return self._no_sample()
        try:
            body = await request.json()
        except Exception:
            body = None
        if not isinstance(body, dict) or not isinstance(body.get("url"), str):
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        url, label = body["url"], body.get("label")
        if label not in LABELS:
            return web.json_response(
                {"ok": False, "error": f"label is one of {', '.join(LABELS)}"}, status=400
            )

        now = datetime.now(UTC)
        try:
            if not self._blind_labels.record(url, label, now):
                return web.json_response(
                    {
                        "ok": False,
                        "error": "This item is already answered or not in the sample: "
                        "reload the page.",
                    },
                    status=409,
                )
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            return web.json_response({"ok": False, "error": str(e)}, status=500)
        if label != "skip":
            try:
                record_votes(
                    self._article_store,
                    accepted=[url] if label == "up" else [],
                    rejected=[url] if label == "down" else [],
                    at=now,
                )
            except Exception as e:  # noqa: BLE001 - the reason belongs in the response
                logger.warning("Label vote for %s failed; releasing the answer: %s", url, e)
                try:
                    self._blind_labels.release(url, label, now)
                except Exception:  # noqa: BLE001 - the vote's failure is the one to report
                    logger.exception("Could not release the answer for %s", url)
                return web.json_response({"ok": False, "error": str(e)}, status=500)
        try:
            return web.json_response({"ok": True, **self._deck()})
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def _handle_labels_page(self, request: web.Request) -> web.Response:
        return self._own_page(self._labels_page(self._ui_language(request)))

    def _awaiting_onboarding(self) -> bool:
        """Whether this deployment is waiting for onboarding: a writable, empty source table.

        A partial server with no source store has no writable table, so it never is.
        """
        if self._source_store is None:
            return False
        try:
            return not self._effective_sources()
        except Exception:  # noqa: BLE001 - /settings must load while D1 fails; its own reads say why
            logger.warning("Could not read the sources table to route onboarding", exc_info=True)
            return False

    async def _handle_settings_page(self, request: web.Request) -> web.Response:
        """The settings page, or the wizard while this deployment is waiting for onboarding."""
        key, value = ONBOARDING_EXIT_MARK
        if self._awaiting_onboarding() and request.query.get(key) != value:
            raise web.HTTPFound(ONBOARDING_PATH)
        return self._own_page(self._settings_page(self._ui_language(request)))

    async def _handle_onboarding_page(self, request: web.Request) -> web.Response:
        """The wizard, on any deployment with a writable source table.

        It stays open once the table has a source, because its own sources step
        writes one: a reload on a later step must not send the reader out of it.
        A partial server with no source store has no table the wizard could write.
        """
        if self._source_store is None:
            raise web.HTTPFound("/settings")
        return self._own_page(self._onboarding_page(self._ui_language(request)))

    async def _handle_interest_page(self, request: web.Request) -> web.Response:
        """The interest map, drawn from D1 as it is now."""
        from cyris.service_layer.interest_page import render_interest_page

        t = translator(self._ui_language(request))
        if self._interest_reader is None:
            return web.Response(text=t("interest.no_store"), status=409)
        try:
            writer = html_digest.HtmlDigestWriter(Path("."), language=t.tag)
            page = render_interest_page(self._interest_reader, writer, self._clock())
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            logger.error("Failed to render the interest map: %s", e)
            return web.Response(text=t("interest.failed", reason=str(e)), status=500)
        return web.Response(text=page, content_type="text/html")

    async def _handle_browser_messages(self, request: web.Request) -> web.Response:
        """One catalog's browser messages, merged with English's for each key it lacks.

        `no-cache`, as for every `/static/` file: the browser keeps its copy and asks
        each time, and a matching `If-None-Match` is answered 304 with no body.
        """
        tag = request.match_info["tag"]
        if tag not in catalogs():
            raise web.HTTPNotFound()
        served = browser_file(tag, BROWSER_MESSAGES)
        headers = {"ETag": served.etag, "Cache-Control": "no-cache"}
        if _etag_matches(request.headers.get("If-None-Match", ""), served.etag):
            return web.Response(status=304, headers=headers)
        return web.Response(body=served.body, content_type="application/json", headers=headers)

    async def _handle_favicon(self, request: web.Request) -> web.Response:
        return web.Response(body=html_digest.FAVICON.read_bytes(), content_type="image/svg+xml")

    async def _handle_vote_probe(self, request: web.Request) -> web.Response:
        """The pages' capability probe on a server with no local SQLite store to vote into.

        `votes: false` opens Settings and the Archive link and leaves the vote buttons and
        `.gated` controls closed. A server on D1 answers it too: the cloud's queue takes
        that store's votes. A local `cyris triage-ui` takes votes (`_handle_vote`),
        and the app Worker answers this path itself; both answer without that field.
        """
        return web.json_response({"authorized": True, "votes": False})

    async def _handle_vote(self, request: web.Request) -> web.Response:
        """A digest or raw page's vote, answered as the cloud edition answers it, applied at once.

        The request and every answer are the app Worker's `handleVote` and the promote
        Worker's behind it (`workers/app/test/vote_cases.json`), so the pages speak to
        either edition alike. Where the cloud queues the vote for the next run's
        `promote-sync`, this server is that run's own Python and the store is a file beside
        it, so it hands the vote straight to `apply_promotions`, the one rule every queue is
        drained by: the same `triaged_at` stamp, state and topic vote, on the same tables
        (ADR-0044). The Host and Origin check is what lets only this server's own pages
        write here; a local install has no sign-in (ADR-0025).
        """
        from cyris.adapters.promotions import PromotedArticle, apply_promotions

        if request.method in ("GET", "HEAD"):
            return web.json_response({"authorized": True})
        if request.method != "POST":
            return web.json_response({"error": "method not allowed"}, status=405)
        try:
            body = await request.json()
        except Exception:
            # The promote Worker reads a body it cannot parse as none.
            body = None
        url = body.get("url") if isinstance(body, dict) else None
        if not isinstance(url, str) or not url:
            return web.json_response({"error": "missing url"}, status=400)
        vote = body.get("vote")
        vote = NO_PAGE_VOTE if vote is None else vote
        if vote not in PAGE_VOTES:
            return web.json_response({"error": "bad vote"}, status=400)
        digest_date = body.get("digest_date")
        # The click's time, as the promote Worker stamps its queue entry with its own clock,
        # and the `triaged_at` stamp's, which a sync would put at the sync's time.
        clicked = self._clock()
        promotion = PromotedArticle(
            url=url,
            vote=vote,
            digest_date=digest_date if isinstance(digest_date, str) else None,
            ts=clicked.isoformat(),
        )
        try:
            result = apply_promotions(
                [promotion], self._article_store, self._issue_topics, at=clicked
            )
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            logger.warning("Vote for %s not recorded: %s", url, e)
            return web.json_response({"error": str(e)}, status=500)
        if result.topic_votes_failed:
            # A sync acks past this, as a held ack would re-stamp its URLs every run; here the
            # reader is still on the page, so the click shows as failed and can be made again.
            logger.warning("Topic vote for %s not recorded: %s", url, result.topic_votes_failed)
            return web.json_response({"error": result.topic_votes_failed}, status=500)
        return web.json_response({"ok": True})

    def _type_scale(self) -> float:
        """The deployment's type size; 1 for a value that is not one of the steps."""
        value = self._values.get(TYPE_SCALE_KEY)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return NEUTRAL_TYPE_SCALE
        return value if value in TYPE_SCALES else NEUTRAL_TYPE_SCALE

    def _sized(self, body: bytes) -> bytes:
        """The page with this deployment's type size, as the app Worker serves it."""
        scale = self._type_scale()
        end = body.find(HEAD_END)
        # The Worker sizes what passes through it, from the value it reads itself.
        if self._behind_app_worker or scale == NEUTRAL_TYPE_SCALE or end < 0:
            return body
        return body[:end] + type_scale_style(scale).encode() + body[end:]

    def _page_path(self, name: str) -> Path | None:
        """`<name>.html` in the pages directory, or None unless it is a file that stays in it."""
        if self._html_dir is None:
            return None
        try:
            root = self._html_dir.resolve()
            path = (root / f"{name}{PAGE_SUFFIX}").resolve()
            # A name past the filesystem's limit raises here, not just on a read.
            is_page = path.suffix == PAGE_SUFFIX and path.is_relative_to(root) and path.is_file()
        except (OSError, RuntimeError):
            return None
        return path if is_page else None

    def _serve_page(self, name: str) -> web.Response | None:
        path = self._page_path(name)
        if path is None:
            return None
        try:
            body = path.read_bytes()
        except OSError:
            return None
        return web.Response(
            body=self._sized(body),
            content_type="text/html",
            charset="utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    def _own_page(self, html: str) -> web.Response:
        return web.Response(
            body=self._sized(html.encode()), content_type="text/html", charset="utf-8"
        )

    def _no_page(self, name: str) -> web.Response:
        """404. The archive address says what to do: links lead here on a new install."""
        if name != INDEX_PAGE:
            text = "Not found"
        else:
            text = NO_ISSUE_YET if self._html_dir is not None else NO_PAGES_WRITTEN
        return web.Response(status=404, text=text, content_type="text/plain")

    async def _handle_index(self, request: web.Request) -> web.Response:
        page = self._serve_page(INDEX_PAGE)
        if page is not None:
            return page
        if self._awaiting_onboarding():
            raise web.HTTPFound(ONBOARDING_PATH)
        return self._no_page(INDEX_PAGE)

    async def _handle_page(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        return self._serve_page(name) or self._no_page(name)

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        logger.info("Settings at http://%s:%d/settings", self._host, self._port)
        if self._behind_app_worker:
            logger.warning(
                "%s is set: requests are not checked for a foreign Host or Origin",
                BEHIND_APP_WORKER_ENV,
            )

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
        if self._feed_checker is not None:
            await self._feed_checker.close()
