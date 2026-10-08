"""The settings web server: /settings, /labels and the APIs they call."""

import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiohttp import web
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape
from pydantic import ValidationError

from cyris.adapters.output import html_digest
from cyris.adapters.store.feed_health import FeedHealth
from cyris.config import SETTINGS_FIELDS
from cyris.diagnostics.doctor import probe_discord, probe_embedder
from cyris.domain.models import NEWSLETTER_SOURCE_TYPE, SourceConfig, TrackedTopic

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


def _render_page(template: str) -> str:
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
    return env.get_template(template).render()


def render_settings_page() -> str:
    return _render_page("settings.html.j2")


def render_labels_page() -> str:
    return _render_page("labels.html.j2")


# The answers /labels takes; a skip is kept on the sample alone.
LABELS = ("up", "down", "skip")


class TriageServer:
    """Serves /settings, /labels and the settings, sources, labels and build APIs."""

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
        self._settings_page = render_settings_page()
        self._labels_page = render_labels_page()
        self._app = web.Application()
        self._app.router.add_get("/api/build", self._handle_build)
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
        self._app.router.add_get("/api/topics", self._handle_get_topics)
        self._app.router.add_post("/api/topics", self._handle_post_topic)
        self._app.router.add_delete("/api/topics/{name}", self._handle_delete_topic)
        self._app.router.add_get("/api/labels", self._handle_get_labels)
        self._app.router.add_post("/api/labels", self._handle_post_label)
        self._app.router.add_get("/settings", self._handle_settings_page)
        self._app.router.add_get("/labels", self._handle_labels_page)
        # Production never reaches this: the app Worker sends /favicon.svg to Pages.
        # A local `cyris triage-ui` has no Pages behind it.
        self._app.router.add_get("/favicon.svg", self._handle_favicon)
        self._app.router.add_static("/static", STATIC_DIR)
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
        from cyris.bootstrap import EMBEDDING_ENV, default_models, embedding_defaults
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
                "fields": SETTINGS_FIELDS,
                "missing": sorted(key for key in GRADE_D_KEYS if key not in self._values),
                "may_be_empty": [key for key in GRADE_D_KEYS if _accepts_empty(key)],
                "providers": providers,
                "embedding_providers": embedding_providers,
                "languages": list(_language_names()),
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
            candidate = LLMProviderConfig(provider=provider, model=model)
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
            candidate = LLMProviderConfig(provider=provider, model=model)
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
        from cyris.config import validate_setting

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
        from cyris.config import validate_setting

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
            probe = await probe_embedder(provider, values["vote_similarity.model"])
            if probe.status != "ok":
                # A missing key is the provider's to fix, the way the page marks it.
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
                        "tier": s.tier.value,
                        "url": s.url,
                        "email_match": s.email_match,
                        "homepage": s.homepage,
                        "tags": s.tags,
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
            self._source_store.upsert(source)
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            return web.json_response({"ok": False, "error": str(e)}, status=500)

        logger.info("Source %s written to D1", source.name)
        return web.json_response({"ok": True, "name": source.name, "note": "Effective next run."})

    async def _handle_delete_source(self, request: web.Request) -> web.Response:
        if self._source_store is None:
            return web.json_response(
                {"ok": False, "error": "No writable source table (store backend is not D1)"},
                status=409,
            )
        name = request.match_info["name"]
        try:
            if set(self._source_store.list_sources()) == {name}:
                return web.json_response(
                    {
                        "ok": False,
                        "error": (
                            f"{name} is the last source. A run with none stops, "
                            "so add another before retiring it."
                        ),
                    },
                    status=409,
                )
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

    async def _handle_get_labels(self, request: web.Request) -> web.Response:
        if self._blind_labels is None:
            return self._no_sample()
        try:
            return web.json_response(self._deck())
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def _handle_post_label(self, request: web.Request) -> web.Response:
        """Answer one open item: up and down are written to the article as a vote first,
        then the answer to the sample; a skip goes to the sample alone."""
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
            if not self._blind_labels.is_open(url):
                return web.json_response(
                    {
                        "ok": False,
                        "error": "This item is already answered or not in the sample: "
                        "reload the page.",
                    },
                    status=409,
                )
            if label != "skip":
                record_votes(
                    self._article_store,
                    accepted=[url] if label == "up" else [],
                    rejected=[url] if label == "down" else [],
                    at=now,
                )
            self._blind_labels.record(url, label, now)
            return web.json_response({"ok": True, **self._deck()})
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def _handle_labels_page(self, request: web.Request) -> web.Response:
        return web.Response(text=self._labels_page, content_type="text/html")

    async def _handle_settings_page(self, request: web.Request) -> web.Response:
        return web.Response(text=self._settings_page, content_type="text/html")

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
