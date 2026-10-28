"""The settings web server: /settings, /labels and the APIs they call."""

import asyncio
import json
import logging
import os
import re
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

from aiohttp import web
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape
from pydantic import ValidationError

from cyris.adapters.output import html_digest
from cyris.adapters.store.d1 import MAX_BOUND_PARAMS
from cyris.adapters.store.feed_health import FeedHealth
from cyris.config import SETTINGS_FIELDS
from cyris.diagnostics.doctor import LLM_PROBE_TIMEOUT_SECONDS, probe_discord, probe_embedder
from cyris.domain.areas import Area, AreaCatalog, is_unplaced_label, meets_birth_threshold
from cyris.domain.models import NEWSLETTER_SOURCE_TYPE, SourceConfig, TrackedTopic
from cyris.domain.news_units import UNITS_PER_BATCH
from cyris.service_layer import area_llm

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# The grade-D keys stored through the generic values route. The rest need their
# own route: the LLM and the embedder are probed, the schedule and the webhook
# have their own rules.
PLAIN_KEYS: tuple[str, ...] = tuple(
    key for key, field in SETTINGS_FIELDS.items() if field["route"] == "plain"
)
TEMPLATES_DIR = Path(__file__).parent / "templates"


def _accepts_empty(key: str) -> bool:
    from cyris.config import validate_setting

    try:
        validate_setting(key, "")
    except ValueError:
        return False
    return True


def _refused(error: str, field: str) -> web.Response:
    """A 400 whose `field`, a settings key or a source field, is the one the page marks."""
    return web.json_response({"ok": False, "error": error, "field": field}, status=400)


def _label(key: str) -> str:
    return SETTINGS_FIELDS[key]["label"]


def _probe_refusal(probe) -> str:
    """What the check found, then what to do about it."""
    return "\n".join(part for part in (probe.detail, probe.fix) if part)


def _values_note(keys: list[str]) -> str:
    """When the saved keys take effect: a live key reaches the pages, the rest the next run."""
    live = [SETTINGS_FIELDS[key]["label"] for key in keys if SETTINGS_FIELDS[key].get("live")]
    if not live:
        return "Effective next run."
    if len(live) == len(keys):
        return "Pages show it within a minute."
    return f"{', '.join(live)}: pages show it within a minute. The rest: effective next run."


_AREA_GROUP = {"active": 0, "proposed": 1, "dismissed": 2, "merged": 3}


def _area_order(area: Area) -> tuple[int, int, int, int]:
    """Active by position, proposals by how much evidence they have, then the rest by id."""
    if area.status == "active":
        return (0, area.position, 0, area.id)
    if area.status == "proposed":
        return (1, -area.proposal_issues, -area.proposal_articles, area.id)
    return (_AREA_GROUP[area.status], 0, 0, area.id)


def _health_json(health: FeedHealth | None, now: datetime) -> dict[str, Any] | None:
    """A feed's poll record, with the problems `cyris doctor` would name; None when not polled."""
    if health is None:
        return None
    return {
        "consecutive_failures": health.consecutive_failures,
        "last_error": health.last_error,
        "last_failed_at": health.last_failed_at,
        "last_ok_at": health.last_ok_at,
        "newest_article_at": health.newest_article_at,
        "problems": health.problems(now),
    }


# Each tracked-topic field's refusal, in the page's words.
TOPIC_REFUSALS = {
    "name": "A topic needs a name.",
    "description": "A topic needs a description: one sentence saying what it is about.",
    "threshold": "Threshold is a cosine above 0 and at most 1.",
    "model": "A topic needs the embedding model its threshold was set for.",
}


def _render_page(template: str, **context: object) -> str:
    """Render one of this server's pages with the digest pages' own site bar.

    The pages live outside `static/` so no unrendered copy is served, and the
    loader also reads the digest templates, where the site bar partial is.
    Autoescaping matches `HtmlDigestWriter`: these templates end in `.j2`.
    """
    env = Environment(
        loader=ChoiceLoader(
            [
                FileSystemLoader(TEMPLATES_DIR),
                FileSystemLoader(Path(html_digest.__file__).parent / "templates"),
            ]
        ),
        autoescape=select_autoescape(["html", "xml"], default=True),
    )
    return env.get_template(template).render(**context)


def render_settings_page() -> str:
    return _render_page("settings.html.j2", units_per_batch=UNITS_PER_BATCH)


def render_labels_page() -> str:
    return _render_page("labels.html.j2")


def render_onboarding_page() -> str:
    return _render_page("onboarding.html.j2")


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

# What a digest page can post to `POST /api/vote`. The promote Worker takes the
# same three, and a "deep" from a page published before 2026-08-08 still arrives.
DIGEST_VOTES = ("up", "down", "deep")

# A page the local server serves out of the HTML output directory: an issue, its
# raw form, or the index. Everything else there is not a route.
PAGE_NAME = re.compile(r"^(?:\d{4}-\d{2}-\d{2}-(?:morning|evening)(?:-raw)?|index)$")
PERIODS = ("morning", "evening")


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
    """The topics the feed picker groups its entries by, in the order it shows them."""
    return _onboarding_catalog()["topics"]


async def _revalidate_static(request: web.Request, response: web.StreamResponse) -> None:
    """Make the browser ask before reusing a page's CSS or JS.

    `add_static` sends an ETag and a Last-Modified but no Cache-Control, so a
    browser falls back to heuristic freshness and pairs a new page with the
    stylesheet it cached before the release. `no-cache` still lets it keep the
    file: each use is a conditional request, answered 304 while the ETag holds.
    """
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"


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
        area_store=None,
        source_titles=None,
        llm_factory=None,
        onboarding_articles=None,
        feed_checker=None,
        digest_store=None,
        run_log=None,
        interest_reader=None,
        start_run=None,
        archive_dir=None,
        clock=None,
    ) -> None:
        self._host = host
        self._port = port
        # Without a settings store the page still renders, read-only: a
        # `backend = "json"` deployment has nowhere to put a runtime setting, so
        # its owner edits `cyris.toml` by hand.
        self._settings = settings
        # The grade-D values the deployment's home holds, by `table.field`; a key
        # absent here is missing. Each successful save updates it.
        self._values = dict(values or {})
        # Already resolved by `load_effective_config` — D1's `sources` table under
        # a D1 store, empty or not; `sources.yaml` otherwise.
        self._sources = sources or {}
        # The write surface. Absent on a `backend = "json"` deployment,
        # where `sources.yaml` is the only home and the list stays read-only.
        self._source_store = source_store
        # D1's `feed_health` beside each source's newest article; absent on a
        # `backend = "json"` deployment, which polls no Worker.
        self._feed_health = feed_health
        # Like sources: the startup list, read-only on a `backend = "json"`
        # deployment, whose topics live in cyris.toml; D1's table otherwise.
        self._tracked_topics = tracked_topics or []
        self._topic_store = topic_store
        # D1's blind-label sample, and the store its up and down answers are
        # written to as votes; both absent on a `backend = "json"` deployment.
        self._blind_labels = blind_labels
        self._article_store = article_store
        # D1's areas and birth threshold; absent on a `backend = "json"` deployment.
        self._area_store = area_store
        # D1's recent article titles for the Propose prompt; absent on a `backend = "json"`
        # deployment, and a Propose without it asks the model with no titles.
        self._source_titles = source_titles
        # Called with the grade-D values for an LLM client; None when no LLM can be built.
        self._llm_factory = llm_factory
        # The wizard's card reader, over the run's own RSS edge
        # (`bootstrap.build_onboarding_articles`); absent on a `backend = "json"`
        # deployment, which has no wizard.
        self._onboarding_articles = onboarding_articles
        # The wizard's check of a feed the reader pastes
        # (`bootstrap.build_feed_checker`); absent where there is no wizard.
        self._feed_checker = feed_checker
        # D1's stored issues and `digest_runs`, which the wizard's first issue
        # reads its key's state and its run's outcome from; both absent on a
        # `backend = "json"` deployment, which has no wizard.
        self._digest_store = digest_store
        self._run_log = run_log
        # D1's voted tags and live holds, which the interest map is drawn from on each
        # request (`D1InterestReader.read`); absent on a `backend = "json"` deployment.
        self._interest_reader = interest_reader
        # `(period, date) -> {"started", "at"}`, starting this host's own run: the
        # app Worker starts a container instead. Absent where nothing starts runs.
        self._start_run = start_run
        # Where `[html_output]` writes: the pages a run publishes, served here so a
        # local deployment has the archive the app Worker gets from Pages.
        self._archive_dir = Path(archive_dir) if archive_dir else None
        # The time the first issue's key is picked at; a probe pins it.
        self._clock = clock or (lambda: datetime.now(UTC))
        self._settings_page = render_settings_page()
        self._labels_page = render_labels_page()
        self._onboarding_page = render_onboarding_page()
        self._app = web.Application()
        self._app.router.add_get("/api/build", self._handle_build)
        self._app.router.add_get("/api/settings", self._handle_get_settings)
        self._app.router.add_post("/api/settings", self._handle_post_settings)
        self._app.router.add_post("/api/diagnostics/llm", self._handle_diagnose_llm)
        self._app.router.add_post("/api/diagnostics/embedding", self._handle_diagnose_embedding)
        self._app.router.add_post("/api/settings/schedule", self._handle_post_schedule)
        self._app.router.add_post("/api/settings/values", self._handle_post_values)
        self._app.router.add_post("/api/settings/vote-similarity", self._handle_post_vote)
        self._app.router.add_post("/api/settings/notify", self._handle_post_notify)
        self._app.router.add_post("/api/settings/email", self._handle_post_email)
        self._app.router.add_get("/api/sources", self._handle_get_sources)
        self._app.router.add_post("/api/sources", self._handle_post_source)
        self._app.router.add_delete("/api/sources/{name}", self._handle_delete_source)
        self._app.router.add_get("/api/onboarding/feeds", self._handle_get_onboarding_feeds)
        self._app.router.add_post("/api/onboarding/feed-check", self._handle_post_feed_check)
        self._app.router.add_post("/api/onboarding/cards", self._handle_post_onboarding_cards)
        self._app.router.add_post("/api/onboarding/votes", self._handle_post_onboarding_votes)
        self._app.router.add_get("/api/onboarding/first-issue", self._handle_get_first_issue)
        self._app.router.add_get("/api/topics", self._handle_get_topics)
        self._app.router.add_post("/api/topics", self._handle_post_topic)
        self._app.router.add_delete("/api/topics/{name}", self._handle_delete_topic)
        self._app.router.add_get("/api/areas", self._handle_get_areas)
        self._app.router.add_post("/api/sources/suggest-area", self._handle_suggest_area)
        self._app.router.add_post("/api/areas/propose", self._handle_propose_areas)
        self._app.router.add_post("/api/areas/assign", self._handle_bulk_assign)
        self._app.router.add_put("/api/areas/birth-threshold", self._handle_put_birth_threshold)
        self._app.router.add_put("/api/areas/order", self._handle_put_area_order)
        self._app.router.add_post(r"/api/areas/{id:\d+}/activate", self._handle_activate_area)
        self._app.router.add_post(r"/api/areas/{id:\d+}/dismiss", self._handle_dismiss_area)
        self._app.router.add_post(r"/api/areas/{id:\d+}/merge", self._handle_merge_area)
        self._app.router.add_post(r"/api/areas/{id:\d+}/sources", self._handle_assign_sources)
        self._app.router.add_put(r"/api/areas/{id:\d+}", self._handle_rename_area)
        self._app.router.add_get("/api/labels", self._handle_get_labels)
        self._app.router.add_post("/api/labels", self._handle_post_label)
        # The digest's own two routes, which the app Worker fronts on Cloudflare.
        self._app.router.add_get("/api/vote", self._handle_get_vote)
        self._app.router.add_post("/api/vote", self._handle_post_digest_vote)
        self._app.router.add_post("/run", self._handle_post_run)
        self._app.router.add_get("/settings", self._handle_settings_page)
        self._app.router.add_get("/onboarding", self._handle_onboarding_page)
        self._app.router.add_get("/labels", self._handle_labels_page)
        self._app.router.add_get("/interest", self._handle_interest_page)
        # Production never reaches this: the app Worker sends /favicon.svg to Pages.
        # A local `cyris triage-ui` has no Pages behind it.
        self._app.router.add_get("/favicon.svg", self._handle_favicon)
        self._app.router.add_static("/static", STATIC_DIR)
        # Last: a published page's short URL, the shape Pages resolves, and the
        # index at /. Nothing else in the output directory is a route.
        # Every method, but only pages answered: a GET-less write to a page's URL
        # must stay the 404 it was before these routes existed, not a 405 that
        # names the one method the directory answers.
        self._app.router.add_route("*", "/", self._handle_archive_page)
        self._app.router.add_route("*", r"/{name:.+}", self._handle_archive_page)
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

    async def _handle_get_vote(self, request: web.Request) -> web.Response:
        """The digest's capability probe: does this deployment take votes?

        The app Worker answers this after checking Access, and the digest hides
        its vote buttons and gated links on anything but `{"authorized": true}`.
        Here the answer is the store's: a vote needs rows to write to, and a
        `backend = "json"` deployment has none of those tables.
        """
        if self._article_store is None:
            return web.json_response({"error": "no article store to vote into"}, status=503)
        return web.json_response({"authorized": True})

    async def _handle_post_digest_vote(self, request: web.Request) -> web.Response:
        """A digest vote, applied to the store now.

        The Cloudflare path parks the vote in the promote Worker's KV and the
        next run's `promote-sync` applies it; locally there is no hop worth
        keeping, so this writes what `sync_promotions` would: `down` rejects,
        anything else accepts, and every voted row is stamped `triaged_at` — the
        press was a person's. A URL the store no longer holds is logged and
        answered `ok`, the way the Worker acks a vote for a cleaned article.
        """
        from cyris.adapters.promotions import record_votes

        if self._article_store is None:
            return web.json_response(
                {"error": 'votes need the SQL store: set [store] backend = "sqlite"'},
                status=503,
            )
        try:
            body = await request.json()
        except Exception:
            body = None
        url = body.get("url") if isinstance(body, dict) else None
        vote = body.get("vote", "up") if isinstance(body, dict) else None
        if not isinstance(url, str) or not url:
            return web.json_response({"ok": False, "error": "missing url"}, status=400)
        if vote not in DIGEST_VOTES:
            return web.json_response({"ok": False, "error": "bad vote"}, status=400)

        if not self._article_store.get_by_urls([url]):
            logger.warning("Voted URL not in store: %s", url)
            return web.json_response({"ok": True})

        record_votes(
            self._article_store,
            accepted=[] if vote == "down" else [url],
            rejected=[url] if vote == "down" else [],
            at=datetime.now(UTC),
        )
        logger.info("Digest vote: %s %s", vote, url)
        return web.json_response({"ok": True})

    async def _handle_post_run(self, request: web.Request) -> web.Response:
        """Start a run, as the app Worker's `POST /run?period=&date=` does.

        The Worker starts its `run` container; this server asks the host it
        already is to start the same command, so the wizard's first issue and a
        manual run work with no Cloudflare in front. `date` is checked to be a
        real day only: the Worker also refuses a date that is not today or
        yesterday in the deployment's time zone, because a published issue must
        not be stored over — a local caller starts its own run and may say so.
        """
        if self._start_run is None:
            return web.json_response({"error": "this deployment starts no run"}, status=503)
        period = request.query.get("period")
        date = request.query.get("date")
        if period is None and date is not None:
            return web.json_response({"error": "date needs a period"}, status=400)
        if period is not None and period not in PERIODS:
            return web.json_response(
                {"error": f"period must be one of {', '.join(PERIODS)}"}, status=400
            )
        if date is not None:
            try:
                datetime.strptime(date, "%Y-%m-%d")
            except ValueError:
                return web.json_response(
                    {"error": "date must be a real YYYY-MM-DD date"}, status=400
                )
        try:
            started = await asyncio.to_thread(self._start_run, period, date)
        except RuntimeError as e:
            return web.json_response({"error": str(e)}, status=409)
        return web.json_response(started)

    async def _handle_archive_page(self, request: web.Request) -> web.StreamResponse:
        """A page `[html_output]` wrote: `/`, `/index.html` and an issue's short URL.

        Pages resolves `/2026-10-28-morning` to that file on Cloudflare; a local
        deployment has no Pages, so the same URL is answered from the output
        directory, and the pages' `index.html` links resolve against it.
        """
        # Pages are read; a write to a page's URL stays the 404 it always was.
        if request.method not in ("GET", "HEAD"):
            raise web.HTTPNotFound()
        name = request.match_info.get("name", "index.html")
        stem = name.removesuffix(".html") or "index"
        if not PAGE_NAME.match(stem) or self._archive_dir is None:
            raise web.HTTPNotFound()
        path = self._archive_dir / f"{stem}.html"
        if not path.is_file():
            raise web.HTTPNotFound()
        # Same live override as the interest page: the file is read out and
        # tagged when a palette is set, streamed untouched when the tokens are
        # the theme.
        from cyris.adapters.output.themes import theme_style

        if theme_style(self._values.get("digest.theme") or "default"):
            return web.Response(
                text=self._injected(path.read_text(encoding="utf-8")),
                content_type="text/html",
                # A cached copy keeps the old palette through a theme save,
                # which is the one thing a switch must not show.
                headers={"Cache-Control": "no-cache"},
            )
        return web.FileResponse(path, headers={"Cache-Control": "no-cache"})

    def _admin_page(self, page: str) -> web.Response:
        """A cached admin page served in the saved theme, never re-served from a cache.

        Server-side injection is what keeps the first paint honest: the page
        arrives already themed, so no default-then-switch flash. The digest
        pages get the same override, so every surface a reader sees carries
        one palette without a run in between.
        """
        return web.Response(
            text=self._injected(page),
            content_type="text/html",
            headers={"Cache-Control": "no-cache"},
        )

    def _injected(self, page: str) -> str:
        """A page with the deployment's theme override before its `</head>`.

        Live, not baked: per request, from the values this server keeps, so a
        save on the Theme page reaches the next page view. "" for the default
        theme — the tokens as written are that palette, and nothing overrides.
        """
        from cyris.adapters.output.themes import theme_style

        style = theme_style(self._values.get("digest.theme") or "default")
        return page.replace("</head>", f"<style>{style}</style>\n</head>", 1) if style else page

    async def _handle_get_settings(self, request: web.Request) -> web.Response:
        """Every runtime setting and which are missing, and what this machine could switch to."""
        from cyris.adapters.notify import mask_discord_webhook_url
        from cyris.adapters.output.themes import THEMES, theme_style
        from cyris.bootstrap import (
            EMBEDDING_ENV,
            clef_credentials,
            default_models,
            embedding_defaults,
        )
        from cyris.config import GRADE_D_KEYS, LLMProviderConfig, VoteSimilarityConfig
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
            # The compatible provider's key is optional — a LAN or localhost
            # endpoint may answer with none — so it is choosable either way;
            # its base_url is part of the save, not the environment.
            ready = name == "openai_compatible" or bool(probe_cfg.api_key)
            ready = ready and (
                bool(probe_cfg.account_id) if name in ("workers_ai", "ai_gateway") else True
            )
            providers.append(
                {
                    "name": name,
                    "env_var": probe_cfg.api_key_env_var,
                    "default_model": models[name],
                    # The compatible provider is configured by its own three
                    # environment names, so the page shows what they hold
                    # instead of asking for fields it cannot save.
                    "env_model": probe_cfg.model,
                    "env_base_url": probe_cfg.base_url,
                    "configured": ready,
                }
            )
        # Not from provider_defaults.json: "none" has no model, and anything read
        # from that file is treated as a provider that has one.
        providers.append({"name": "none", "env_var": "", "default_model": "", "configured": True})
        embedding_providers = []
        for name, env in EMBEDDING_ENV.items():
            # Constructing one is what resolves the compatible pair's
            # environment: the page shows what the names hold, not fields.
            vote_cfg = VoteSimilarityConfig(enabled=False, provider=name, model="", max_seeds=1)
            embedding_providers.append(
                {
                    "name": name,
                    "env_var": env[0],
                    "default_model": embedding_defaults(name)["model"],
                    "env_model": vote_cfg.model,
                    "env_base_url": vote_cfg.base_url,
                    "configured": name == "openai_compatible"
                    or all(os.environ.get(var) for var in env),
                }
            )
        return web.json_response(
            {
                "values": values,
                "fields": SETTINGS_FIELDS,
                "missing": sorted(key for key in GRADE_D_KEYS if key not in self._values),
                "may_be_empty": [key for key in GRADE_D_KEYS if _accepts_empty(key)],
                "providers": providers,
                "embedding_providers": embedding_providers,
                # Whether a run could call Clef; the page disables it as a judge if not.
                "clef_ready": clef_credentials() is not None,
                # The theme select switches the page under the reader as it
                # picks, so the styles travel with the names (themes.py's own).
                "themes": [
                    {"id": value, "label": label, "css": theme_style(value)}
                    for value, label in THEMES
                ],
                # Each tag with the name languages.json gives it, in the file's order:
                # the pages offer them as a select's options.
                "languages": [
                    {"tag": tag, "name": name} for tag, name in _language_names().items()
                ],
                "writable": self._settings is not None,
            }
        )

    async def _settings_body(self, request: web.Request) -> dict | web.Response:
        """The JSON object a settings write sent, or the response refusing it."""
        if self._settings is None:
            return web.json_response(
                {"ok": False, "error": "this deployment has no settings store to write"},
                status=409,
            )
        try:
            body = await request.json()
        except Exception:
            body = None
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
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

        provider = (body.get("provider") or "").strip()
        model = (body.get("model") or "").strip()
        try:
            candidate = LLMProviderConfig(
                provider=provider,
                model=model,
                reasoning_effort=self._values.get("llm_provider.reasoning_effort", "default"),
            )
        except ValidationError:
            return _refused(f"unknown provider {provider!r}", "llm_provider.provider")

        if provider == "none":
            # Nothing to call, and a leftover model must not outlive its provider.
            model = ""
            detail = "No model is called: digests list plain excerpts."
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
                "note": "Saved. The next digest run picks this up.",
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

    async def _handle_diagnose_embedding(self, request: web.Request) -> web.Response:
        """Probe an embedder choice without storing it — the Test button's backend.

        The same probe the enable-switch makes; separate so a base_url and a
        model can be tried against an endpoint before committing to them.
        """
        from cyris.bootstrap import EMBEDDING_ENV
        from cyris.config import VoteSimilarityConfig
        from cyris.diagnostics import doctor

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        provider = (body.get("provider") or "").strip()
        if provider not in EMBEDDING_ENV:
            return web.json_response(
                {"ok": False, "error": f"unknown embedder {provider!r}"}, status=400
            )
        # The model and the address come from the embedding environment, the
        # way a run reads them; the page sends the provider choice alone.
        candidate = VoteSimilarityConfig(enabled=True, provider=provider, model="", max_seeds=1)
        probe = await doctor.probe_embedder(candidate.provider, candidate.model, candidate.base_url)
        ok = probe.status == "ok"
        return web.json_response(
            {"ok": ok, "provider": provider, "model": candidate.model, "detail": probe.detail},
            status=200 if ok else 502,
        )

    async def _handle_post_schedule(self, request: web.Request) -> web.Response:
        """Set the two digest hours. The cron tick is hourly and reads this."""
        from cyris.service_layer.schedule import validate_schedule

        body = await self._settings_body(request)
        if isinstance(body, web.Response):
            return body

        try:
            times = validate_schedule([str(t).strip() for t in body.get("times") or []])
        except ValueError as e:
            return _refused(str(e), "general.digest_schedule")

        if (refused := self._store({"general.digest_schedule": times})) is not None:
            return refused

        logger.info("Digest schedule set to %s", ", ".join(times))
        return web.json_response({"ok": True, "times": times, "note": "Effective next tick."})

    async def _handle_post_values(self, request: web.Request) -> web.Response:
        """Store any of the plain settings, all or nothing, each through its own rule."""
        from cyris.config import LLMProviderConfig, validate_setting
        from cyris.diagnostics.doctor import probe_llm

        body = await self._settings_body(request)
        if isinstance(body, web.Response):
            return body

        values = body.get("values")
        if not isinstance(values, dict) or not values:
            return web.json_response(
                {"ok": False, "error": "values must name at least one setting"}, status=400
            )
        validated = {}
        for key, value in values.items():
            if key not in SETTINGS_FIELDS:
                return web.json_response(
                    {"ok": False, "error": f"{key!r} is not a setting"}, status=400
                )
            if key not in PLAIN_KEYS:
                return _refused(f"{_label(key)} is saved from its own form", key)
            try:
                validated[key] = validate_setting(key, value)
            except ValueError as e:
                return _refused(f"{_label(key)}: {e}", key)

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
        note = _values_note(list(validated))
        return web.json_response({"ok": True, "values": validated, "note": note})

    async def _handle_post_vote(self, request: web.Request) -> web.Response:
        """Store vote similarity's switch, embedder and seeds as one unit.

        Turning it on checks the embedder with one real call first, and this is
        the only writer of `enabled`, so no run meets an unverified embedder.
        Off stores without a call: a deployment with no embedding key must still
        be able to complete its settings.
        """
        from cyris.bootstrap import EMBEDDING_ENV
        from cyris.config import VoteSimilarityConfig, validate_setting

        body = await self._settings_body(request)
        if isinstance(body, web.Response):
            return body

        values = {}
        for field in ("enabled", "provider", "model", "max_seeds"):
            key = f"vote_similarity.{field}"
            if field not in body:
                return _refused(f"{_label(key)} is required", key)
            try:
                values[key] = validate_setting(key, body[field])
            except ValueError as e:
                return _refused(f"{_label(key)}: {e}", key)

        if values["vote_similarity.enabled"]:
            provider = values["vote_similarity.provider"]
            # Constructing the row fills the compatible pair's address and
            # model from the environment — the same place a run reads them.
            candidate = VoteSimilarityConfig(
                enabled=True,
                provider=provider,
                model=values["vote_similarity.model"],
                max_seeds=values["vote_similarity.max_seeds"],
            )
            probe = await probe_embedder(provider, candidate.model, candidate.base_url)
            if probe.status != "ok":
                # A missing key is the provider's to fix, the way the page marks it;
                # the compatible one's key is optional, so its failures name the
                # address or the model, whichever its own words point at.
                if provider == "openai_compatible":
                    field = (
                        "vote_similarity.base_url"
                        if "base_url" in probe.detail
                        else "vote_similarity.model"
                    )
                else:
                    keyed = all(os.environ.get(var) for var in EMBEDDING_ENV[provider])
                    field = "vote_similarity.model" if keyed else "vote_similarity.provider"
                return _refused(_probe_refusal(probe), field)
            detail = probe.detail
        else:
            detail = "Not checked: vote similarity is off. Turning it on checks the embedder first."

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
                "note": "Saved. The next digest run picks this up.",
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

        if body.get("off") is True:
            if (refused := self._store({"notify.discord_webhook_url": ""})) is not None:
                return refused
            logger.info("Discord notifications turned off")
            return web.json_response(
                {
                    "ok": True,
                    "discord_webhook_url": "",
                    "note": "Notifications are off. The next run finishes without a message.",
                }
            )

        url = (body.get("discord_webhook_url") or "").strip()
        if not url:
            return _refused(
                "Paste a Discord webhook URL, or press Turn off to stop notifications.",
                "notify.discord_webhook_url",
            )

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
                "note": "Saved. The next digest run picks this up.",
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

        recipient = (body.get("email_to") or "").strip()
        sender = (body.get("email_from") or "").strip()
        values = {"notify.email_to": recipient, "notify.email_from": sender}

        if not recipient:
            if (refused := self._store(values)) is not None:
                return refused
            logger.info("Digest mail turned off")
            return web.json_response(
                {"ok": True, **values, "note": "Mail is off. The next run sends no message."}
            )

        if not sender:
            return _refused("Enter the address the digest is sent from, too.", "notify.email_from")

        account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
        token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
        if not (account_id and token):
            return web.json_response(
                {
                    "ok": False,
                    "error": (
                        "This deployment has no CLOUDFLARE_ACCOUNT_ID or CLOUDFLARE_API_TOKEN, "
                        "so it cannot send mail. Set both, then save again."
                    ),
                },
                status=400,
            )

        try:
            status = await send_mail(
                account_id,
                token,
                sender,
                recipient,
                "Cyris test message",
                "This address will receive a message for every digest.",
            )
        except RuntimeError as e:
            return web.json_response(
                {"ok": False, "error": f"The test message was not sent: {e}"}, status=400
            )

        if (refused := self._store(values)) is not None:
            return refused

        logger.info("Digest mail addresses saved")
        return web.json_response(
            {
                "ok": True,
                **values,
                "detail": f"A test message was {status} to {recipient}.",
                "note": "Saved. The next digest run mails this address.",
            }
        )

    async def _handle_get_sources(self, request: web.Request) -> web.Response:
        """What the pipeline is actually fetching.

        `email_match` rides along because it is source data (grade D);
        Cloudflare Email Routing is grade B and stays in the dashboard.
        """
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
                        "area_id": s.area_id,
                        "health": _health_json(health.get(s.name), now),
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
        if self._source_store is None:
            return web.json_response(
                {"ok": False, "error": "No writable source table (store backend is not D1)"},
                status=409,
            )
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        try:
            source = SourceConfig.model_validate(body)
        except ValidationError as e:
            # One line, in the body's own field names: the page sends only values
            # this model accepts, so whoever reads this wrote the request.
            reasons = "; ".join(
                f"{'.'.join(map(str, err['loc'])) or 'body'}: {err['msg']}" for err in e.errors()
            )
            return _refused(reasons, ".".join(map(str, e.errors()[0]["loc"][:1])))
        if not source.name.strip():
            return _refused("A source needs a name.", "name")
        if not source.fetchable:
            needs = {
                "rss": ("An RSS source needs a Feed URL.", "url"),
                NEWSLETTER_SOURCE_TYPE: (
                    "A newsletter source needs a Sender match.",
                    "email_match",
                ),
            }
            error, field = needs.get(
                source.type, (f"A source's type is rss or newsletter, not {source.type!r}.", "type")
            )
            return _refused(error, field)

        try:
            if "area_id" in body:
                if source.area_id is not None:
                    if self._area_store is None:
                        return web.json_response(
                            {
                                "ok": False,
                                "error": "No area table to check the area against.",
                                "field": "area_id",
                            },
                            status=409,
                        )
                    area = self._area_store.get(source.area_id)
                    if area is None or area.status != "active":
                        return _refused("Pick an active area, or none.", "area_id")
            else:
                # A save that does not mention the area leaves the stored one as it was.
                stored = self._source_store.list_sources().get(source.name)
                source.area_id = stored.area_id if stored else None
            self._source_store.upsert(source)
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            return web.json_response({"ok": False, "error": str(e)}, status=500)

        logger.info("Source %s written to D1", source.name)
        return web.json_response({"ok": True, "name": source.name, "note": "Effective next run."})

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
        return web.json_response(
            {
                "awaiting": awaiting,
                "topics": load_onboarding_topics() if awaiting else [],
                "feeds": load_onboarding_feeds() if awaiting else [],
            }
        )

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
            return web.json_response(
                {"ok": False, "error": str(e), "name": e.name},
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
                    "(store backend is not D1).",
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
        it starts by its id. The run itself is started by the app Worker's
        `POST /run?period=&date=`, as any manual run is; nothing here starts one.
        """
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        from cyris.service_layer.schedule import first_issue_key, validate_schedule

        if self._run_log is None or self._digest_store is None or self._source_store is None:
            return web.json_response(
                {
                    "ok": False,
                    "error": "This deployment keeps no run log to wait on "
                    "(store backend is not D1).",
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

    async def _handle_delete_source(self, request: web.Request) -> web.Response:
        if self._source_store is None:
            return web.json_response(
                {"ok": False, "error": "No writable source table (store backend is not D1)"},
                status=409,
            )
        name = request.match_info["name"]
        try:
            # The last source may go: an empty table is a deployment waiting for
            # onboarding, and a run with none ends without fetching anything.
            self._source_store.delete(name)
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            return web.json_response({"ok": False, "error": str(e)}, status=500)

        logger.info("Source %s retired", name)
        return web.json_response({"ok": True, "name": name, "note": "Effective next run."})

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

    def _no_topic_table(self) -> web.Response:
        return web.json_response(
            {
                "ok": False,
                "error": "No writable topic table (store backend is not D1): "
                "edit [[tracked_topics]] in cyris.toml instead.",
            },
            status=409,
        )

    async def _handle_post_topic(self, request: web.Request) -> web.Response:
        """Add or edit one topic, over the row `name` owns. Nothing is probed: a
        topic that cannot be embedded is skipped by the run and named in its summary."""
        if self._topic_store is None:
            return self._no_topic_table()
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        try:
            topic = TrackedTopic.model_validate(body)
        except ValidationError as e:
            first = e.errors()[0]
            field = str(first["loc"][0]) if first["loc"] else ""
            return _refused(TOPIC_REFUSALS.get(field, f"body: {first['msg']}"), field)

        try:
            self._topic_store.upsert(topic)
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            return web.json_response({"ok": False, "error": str(e)}, status=500)

        logger.info("Tracked topic %s written to D1", topic.name)
        return web.json_response({"ok": True, "name": topic.name, "note": "Effective next run."})

    async def _handle_delete_topic(self, request: web.Request) -> web.Response:
        if self._topic_store is None:
            return self._no_topic_table()
        name = request.match_info["name"]
        try:
            removed = self._topic_store.delete(name)
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            return web.json_response({"ok": False, "error": str(e)}, status=500)
        if not removed:
            return web.json_response(
                {"ok": False, "error": f"No topic named {name}: reload the page."}, status=404
            )

        logger.info("Tracked topic %s removed", name)
        return web.json_response({"ok": True, "name": name, "note": "Effective next run."})

    def _no_sample(self) -> web.Response:
        return web.json_response(
            {
                "ok": False,
                "error": 'Blind labels live in D1: this deployment\'s [store] backend is not "d1".',
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

    async def _handle_get_areas(self, request: web.Request) -> web.Response:
        """Every area with its status and counts, in the order the page lists them."""
        if self._area_store is None:
            return web.json_response({"writable": False, "areas": [], "birth_threshold": None})
        try:
            areas = self._area_store.list_areas()
            threshold = self._area_store.birth_threshold()
        except Exception as e:  # noqa: BLE001 - the reason goes to the log, not the page
            logger.error("Reading areas failed: %s", type(e).__name__)
            return web.json_response(
                {"ok": False, "error": "The area table could not be read."}, status=500
            )
        return web.json_response(
            {
                "writable": True,
                "areas": [
                    {
                        **area.model_dump(),
                        "meets_birth_threshold": meets_birth_threshold(area, threshold),
                    }
                    for area in sorted(areas, key=_area_order)
                ],
                "birth_threshold": (
                    None
                    if threshold is None
                    else {"articles": threshold[0], "issues": threshold[1]}
                ),
            }
        )

    @staticmethod
    def _unplaced_proposal(area: Area) -> web.Response:
        """409 for activating a proposal stored as "Other" before the name was reserved."""
        return web.json_response(
            {
                "ok": False,
                "error": f"{area.name} is the page's label for entries no area claims: "
                "rename it before confirming it.",
            },
            status=409,
        )

    def _not_proposed(self, area_id: int) -> web.Response:
        """409 for a proposal-only action on an area that is not a proposal; 404 if it is gone."""
        area = self._area_store.get(area_id)
        if area is None:
            return web.json_response(
                {"ok": False, "error": f"No area with id {area_id}: reload the page."}, status=404
            )
        return web.json_response(
            {"ok": False, "error": f"{area.name} is {area.status}, not proposed."}, status=409
        )

    async def _handle_activate_area(self, request: web.Request) -> web.Response:
        """Confirm a proposal: it joins the closed set the next run offers the model."""
        if self._area_store is None:
            return self._no_area_table()
        area_id = int(request.match_info["id"])
        try:
            area = self._area_store.get(area_id)
            if area is not None and area.status == "proposed" and is_unplaced_label(area.name):
                return self._unplaced_proposal(area)
            if not self._area_store.activate(area_id):
                return self._not_proposed(area_id)
            area = self._area_store.get(area_id)
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            return web.json_response({"ok": False, "error": str(e)}, status=500)

        logger.info("Area %s activated", area_id)
        return web.json_response(
            {
                "ok": True,
                "id": area.id,
                "name": area.name,
                "position": area.position,
                "note": "Effective next run.",
            }
        )

    async def _handle_dismiss_area(self, request: web.Request) -> web.Response:
        """Reject a proposal; its name is then ignored, not proposed again."""
        if self._area_store is None:
            return self._no_area_table()
        area_id = int(request.match_info["id"])
        try:
            if not self._area_store.dismiss(area_id):
                return self._not_proposed(area_id)
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            return web.json_response({"ok": False, "error": str(e)}, status=500)

        logger.info("Area %s dismissed", area_id)
        return web.json_response({"ok": True, "id": area_id, "note": "Effective next run."})

    async def _handle_merge_area(self, request: web.Request) -> web.Response:
        """Fold a proposal into an active area; the proposal's name resolves to that area."""
        if self._area_store is None:
            return self._no_area_table()
        area_id = int(request.match_info["id"])
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        into = body.get("into") if isinstance(body, dict) else None
        if isinstance(into, bool) or not isinstance(into, int) or into == area_id:
            return _refused("Merge into a different active area.", "into")
        try:
            target = self._area_store.get(into)
            if target is None or target.status != "active":
                return _refused("Merge into an active area.", "into")
            if self._area_store.get(area_id) is None:
                return web.json_response(
                    {"ok": False, "error": f"No area with id {area_id}: reload the page."},
                    status=404,
                )
            if not self._area_store.merge(area_id, into):
                return self._not_proposed(area_id)
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            return web.json_response({"ok": False, "error": str(e)}, status=500)

        logger.info("Area %s merged into %s", area_id, into)
        return web.json_response(
            {"ok": True, "id": area_id, "into": into, "note": "Effective next run."}
        )

    async def _handle_assign_sources(self, request: web.Request) -> web.Response:
        """Place a batch of sources in one active area; each keeps every other field."""
        if self._area_store is None or self._source_store is None:
            return self._no_area_table()
        area_id = int(request.match_info["id"])
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        names = body.get("sources") if isinstance(body, dict) else None
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            return _refused("sources must be a list of source names.", "sources")
        try:
            area = self._area_store.get(area_id)
            if area is None:
                return web.json_response(
                    {"ok": False, "error": f"No area with id {area_id}: reload the page."},
                    status=404,
                )
            if area.status != "active":
                return web.json_response(
                    {"ok": False, "error": f"{area.name} is {area.status}, not active."},
                    status=409,
                )
            stored = self._source_store.list_sources()
            unknown = [name for name in names if name not in stored]
            if unknown:
                return web.json_response(
                    {
                        "ok": False,
                        "error": "Unknown sources: " + ", ".join(unknown),
                        "field": "sources",
                        "names": unknown,
                    },
                    status=400,
                )
            for name in dict.fromkeys(names):
                self._source_store.upsert(stored[name].model_copy(update={"area_id": area_id}))
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            return web.json_response({"ok": False, "error": str(e)}, status=500)

        logger.info("%d sources assigned to area %s", len(set(names)), area_id)
        return web.json_response({"ok": True, "assigned": len(set(names))})

    async def _handle_bulk_assign(self, request: web.Request) -> web.Response:
        """Give each named source its area, or none, in one request.

        A proposal named here is activated first.

        Every refusal is decided before the first write, so a refused request changes nothing.
        A write that fails midway answers 500 with what did land, so the page can recompute.
        """
        if self._area_store is None or self._source_store is None:
            return self._no_area_table()
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        assignments = body.get("assignments") if isinstance(body, dict) else None
        if not isinstance(assignments, dict) or not all(
            value is None or (isinstance(value, int) and not isinstance(value, bool))
            for value in assignments.values()
        ):
            return _refused(
                "assignments must map each source name to an area id or null.", "assignments"
            )
        assigned: list[str] = []
        activated: list[dict[str, Any]] = []
        try:
            stored = self._source_store.list_sources()
            unknown = [name for name in assignments if name not in stored]
            if unknown:
                return web.json_response(
                    {
                        "ok": False,
                        "error": "Unknown sources: " + ", ".join(unknown),
                        "field": "assignments",
                        "names": unknown,
                    },
                    status=400,
                )
            targets: dict[int, Area] = {}
            for area_id in dict.fromkeys(v for v in assignments.values() if v is not None):
                area = self._area_store.get(area_id)
                if area is None:
                    return web.json_response(
                        {"ok": False, "error": f"No area with id {area_id}: reload the page."},
                        status=404,
                    )
                if area.status in ("dismissed", "merged"):
                    return web.json_response(
                        {"ok": False, "error": f"{area.name} is {area.status}, not assignable."},
                        status=409,
                    )
                if area.status == "proposed" and is_unplaced_label(area.name):
                    return self._unplaced_proposal(area)
                targets[area_id] = area
            for area in sorted(
                (a for a in targets.values() if a.status == "proposed"), key=_area_order
            ):
                if self._area_store.activate(area.id):
                    activated.append({"id": area.id, "name": area.name})
            for name, area_id in assignments.items():
                self._source_store.upsert(stored[name].model_copy(update={"area_id": area_id}))
                assigned.append(name)
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            return web.json_response(
                {
                    "ok": False,
                    "error": str(e),
                    "assigned": assigned,
                    "activated": activated,
                },
                status=500,
            )

        logger.info("%d sources assigned, %d areas activated", len(assigned), len(activated))
        return web.json_response({"ok": True, "assigned": len(assigned), "activated": activated})

    def _llm(self):
        return self._llm_factory(self._values) if self._llm_factory is not None else None

    async def _handle_suggest_area(self, request: web.Request) -> web.Response:
        """Suggest the active area for a source being added. A failure never stops the save."""
        if self._area_store is None:
            return self._no_area_table()
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        url = body.get("url") if isinstance(body, dict) else None
        if not isinstance(url, str) or not url.strip():
            return _refused("A source needs a Feed URL before an area can be suggested.", "url")

        def answer(suggestion, note: str) -> web.Response:
            return web.json_response({"ok": True, "suggestion": suggestion, "note": note})

        try:
            catalog = AreaCatalog(self._area_store.list_areas())
        except Exception as e:  # noqa: BLE001 - the reason stays in the log, not the page
            logger.error("Reading areas failed: %s", type(e).__name__)
            return web.json_response(
                {"ok": False, "error": "The area table could not be read."}, status=500
            )
        names = catalog.names()
        if not names:
            return answer(None, "No active area to suggest from yet.")
        try:
            llm = self._llm()
        except Exception as e:  # noqa: BLE001 - only the class name leaves this handler
            logger.warning("Suggesting an area failed: %s", type(e).__name__)
            return answer(None, f"The model call failed ({type(e).__name__}).")
        if llm is None:
            return answer(None, "No LLM is configured, so no area is suggested.")

        source = SourceConfig(
            name=str(body.get("name") or "").strip() or url,
            url=url.strip(),
            type=str(body.get("type") or "rss"),
            tags=[t for t in body.get("tags") or [] if isinstance(t, str)],
            homepage=body.get("homepage") if isinstance(body.get("homepage"), str) else None,
        )
        try:
            picked = await asyncio.wait_for(
                area_llm.suggest_area(llm, source, names), LLM_PROBE_TIMEOUT_SECONDS
            )
        except Exception as e:  # noqa: BLE001 - only the class name leaves this handler
            logger.warning("Suggesting an area failed: %s", type(e).__name__)
            return answer(None, f"The model call failed ({type(e).__name__}).")
        if picked is None:
            return answer(None, "The model did not pick one of the active areas.")
        area = catalog.place(picked).area
        return answer(
            {"id": area.id, "name": area.name},
            "Suggested from your active areas. Confirm it or choose another.",
        )

    async def _handle_propose_areas(self, request: web.Request) -> web.Response:
        """Ask the model to group every source into areas, and record the new names as proposals."""
        if self._area_store is None:
            return self._no_area_table()
        sources = self._source_store.list_sources() if self._source_store else self._sources
        if not sources:
            return _refused("There are no sources to group yet.", "sources")

        def model_failed(error: Exception) -> web.Response:
            logger.warning("Proposing areas failed: %s", type(error).__name__)
            return web.json_response(
                {"ok": False, "error": f"The model call failed ({type(error).__name__})."},
                status=502,
            )

        try:
            llm = self._llm()
        except Exception as e:  # noqa: BLE001 - only the class name leaves this handler
            return model_failed(e)
        if llm is None:
            return web.json_response(
                {"ok": False, "error": "No LLM is configured, so no areas are proposed."},
                status=409,
            )
        try:
            areas = self._area_store.list_areas()
        except Exception as e:  # noqa: BLE001 - the reason stays in the log, not the page
            logger.error("Reading areas failed: %s", type(e).__name__)
            return web.json_response(
                {"ok": False, "error": "The area table could not be read."}, status=500
            )
        catalog = AreaCatalog(areas)
        # The model is offered the active areas by position, then the proposals.
        names = [
            area.name
            for area in sorted(areas, key=_area_order)
            if area.status in ("active", "proposed")
        ]
        titles = None
        if self._source_titles is not None:
            try:
                titles = self._source_titles.recent(datetime.now(UTC))
            except Exception as e:  # noqa: BLE001 - only the class name leaves this handler
                logger.warning("Reading source titles failed: %s", type(e).__name__)
        try:
            grouping = await asyncio.wait_for(
                area_llm.propose_areas(llm, sources, titles=titles, area_names=names),
                LLM_PROBE_TIMEOUT_SECONDS,
            )
        except Exception as e:  # noqa: BLE001 - only the class name leaves this handler
            return model_failed(e)

        # Every name is placed before the first write, so a refused reply writes nothing.
        placements = [(name, members, catalog.place(name)) for name, members in grouping.items()]
        placements = [
            entry
            for entry in placements
            if entry[2].kind != "dismissed" and not is_unplaced_label(entry[0])
        ]
        new_names = [name for name, _, placed in placements if placed.kind == "unknown"]
        # The new rows go in one statement so a failure writes none, and D1 binds
        # one parameter per name; chunking would give up the all-or-nothing write.
        if len(new_names) > MAX_BOUND_PARAMS:
            return web.json_response(
                {
                    "ok": False,
                    "error": f"The model proposed more than {MAX_BOUND_PARAMS} new areas; "
                    "nothing was saved.",
                },
                status=502,
            )
        try:
            new_ids = self._area_store.insert_proposed(new_names)
        except Exception as e:  # noqa: BLE001 - the reason stays in the log, not the page
            logger.error("Writing proposed areas failed: %s", type(e).__name__)
            return web.json_response(
                {"ok": False, "error": "The area table could not be written."}, status=500
            )
        proposals = []
        for name, members, placed in placements:
            if placed.kind == "unknown":
                proposals.append({"id": new_ids[name], "name": name, "sources": members})
            else:
                area = placed.area
                proposals.append({"id": area.id, "name": area.name, "sources": members})

        return web.json_response({"ok": True, "proposals": proposals, "created": len(new_names)})

    async def _handle_put_area_order(self, request: web.Request) -> web.Response:
        """Set the order of the active areas: the page's list, top to bottom."""
        if self._area_store is None:
            return self._no_area_table()
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        ids = body.get("ids") if isinstance(body, dict) else None
        if not isinstance(ids, list) or not all(
            isinstance(i, int) and not isinstance(i, bool) for i in ids
        ):
            return _refused("ids must be a list of area ids.", "ids")
        try:
            active = {area.id for area in self._area_store.list_areas() if area.status == "active"}
            if len(set(ids)) != len(ids) or set(ids) != active:
                return _refused("ids must name every active area exactly once.", "ids")
            self._area_store.set_order(ids)
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            return web.json_response({"ok": False, "error": str(e)}, status=500)

        logger.info("Active areas reordered")
        return web.json_response({"ok": True, "note": "Effective next run."})

    async def _handle_rename_area(self, request: web.Request) -> web.Response:
        """Give an area a new name; every source and pick keeps resolving to it by id."""
        if self._area_store is None:
            return self._no_area_table()
        area_id = int(request.match_info["id"])
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        raw = body.get("name") if isinstance(body, dict) else None
        name = raw.strip() if isinstance(raw, str) else ""
        if not name or "\n" in raw or "\r" in raw:
            return _refused(
                "A name needs at least one visible character and no line break.", "name"
            )
        try:
            if self._area_store.get(area_id) is None:
                return web.json_response(
                    {"ok": False, "error": f"No area with id {area_id}: reload the page."},
                    status=404,
                )
            if is_unplaced_label(name):
                return web.json_response(
                    {
                        "ok": False,
                        "error": f"{name} is the page's label for entries no area claims.",
                        "field": "name",
                    },
                    status=409,
                )
            if self._area_store.name_taken(name, area_id):
                return web.json_response(
                    {
                        "ok": False,
                        "error": f"Another area already has the name {name}.",
                        "field": "name",
                    },
                    status=409,
                )
            self._area_store.rename(area_id, name)
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            return web.json_response({"ok": False, "error": str(e)}, status=500)

        logger.info("Area %s renamed", area_id)
        return web.json_response(
            {"ok": True, "id": area_id, "name": name, "note": "Effective next run."}
        )

    def _no_area_table(self) -> web.Response:
        return web.json_response(
            {
                "ok": False,
                "error": "No writable area table (store backend is not D1).",
            },
            status=409,
        )

    async def _handle_put_birth_threshold(self, request: web.Request) -> web.Response:
        """The (articles, issues) a proposal must reach to be marked as meeting the threshold."""
        if self._area_store is None:
            return self._no_area_table()
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        values = {}
        for field in ("articles", "issues"):
            value = body.get(field) if isinstance(body, dict) else None
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                return _refused(f"{field} must be a whole number of at least 1.", field)
            values[field] = value
        try:
            self._area_store.set_birth_threshold(values["articles"], values["issues"])
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            return web.json_response({"ok": False, "error": str(e)}, status=500)

        logger.info("Area birth threshold set to %s", values)
        return web.json_response({"ok": True, **values})

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

    async def _handle_labels_page(self, request: web.Response) -> web.Response:
        return self._admin_page(self._labels_page)

    def _awaiting_onboarding(self) -> bool:
        """Whether this deployment is waiting for onboarding: a writable, empty source table.

        A `backend = "json"` deployment has no writable table, so it never is:
        its sources live in `sources.yaml`, which the app never writes.
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
        return self._admin_page(self._settings_page)

    async def _handle_onboarding_page(self, request: web.Request) -> web.Response:
        """The wizard, on any deployment with a writable source table.

        It stays open once the table has a source, because its own sources step
        writes one: a reload on a later step must not send the reader out of it.
        A `backend = "json"` deployment has no table the wizard could write.
        """
        if self._source_store is None:
            raise web.HTTPFound("/settings")
        return self._admin_page(self._onboarding_page)

    async def _handle_interest_page(self, request: web.Request) -> web.Response:
        """The interest map, drawn from D1 as it is now."""
        from cyris.service_layer.interest_page import render_interest_page

        if self._interest_reader is None:
            return web.Response(
                text="The interest map reads votes and holds from D1: this deployment's "
                '[store] backend is not "d1".',
                status=409,
            )
        try:
            page = render_interest_page(
                self._interest_reader, html_digest.HtmlDigestWriter(Path(".")), self._clock()
            )
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            logger.error("Failed to render the interest map: %s", e)
            return web.Response(text=f"Could not draw the interest map: {e}", status=500)
        return web.Response(
            text=self._injected(page),
            content_type="text/html",
            # The interest map is drawn per request; a cached copy is stale votes.
            headers={"Cache-Control": "no-cache"},
        )

    async def _handle_favicon(self, request: web.Request) -> web.Response:
        return web.Response(body=html_digest.FAVICON.read_bytes(), content_type="image/svg+xml")

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        logger.info("Settings at http://%s:%d/settings", self._host, self._port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
        if self._feed_checker is not None:
            await self._feed_checker.close()
