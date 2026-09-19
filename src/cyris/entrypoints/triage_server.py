"""The settings web server: /settings and the APIs it calls."""

import logging
import os
from pathlib import Path
from typing import Any

from aiohttp import web
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape
from pydantic import ValidationError

from cyris.adapters.output import html_digest
from cyris.diagnostics.doctor import probe_discord, probe_embedder
from cyris.domain.models import SourceConfig

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# The grade-D keys stored through the generic values route. The rest need their
# own route: the LLM and the embedder are probed, the schedule and the webhook
# have their own rules.
PLAIN_KEYS: tuple[str, ...] = (
    "general.timezone",
    "general.digest_window_hours",
    "digest.max_articles_per_digest",
    "digest.max_articles_per_digest_output",
    "digest.max_featured",
    "digest.scoring_snippet_length",
    "digest.summarize_snippet_length",
    "digest.filter_snippet_length",
    "digest.output_language",
    "digest.style_prompt",
    "routing.score_threshold",
    "routing.summarize_score_threshold",
)
TEMPLATES_DIR = Path(__file__).parent / "templates"


def render_settings_page() -> str:
    """Render /settings with the digest pages' own site bar.

    The page lives outside `static/` so no unrendered copy is served, and the
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
    return env.get_template("settings.html.j2").render()


class TriageServer:
    """Serves /settings and the settings, sources and build APIs."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8766,
        settings=None,
        values: dict[str, Any] | None = None,
        sources: dict[str, SourceConfig] | None = None,
        source_store=None,
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
        # The write surface (§7 #15). Absent on a `backend = "json"` deployment,
        # where `sources.yaml` is the only home and the list stays read-only.
        self._source_store = source_store
        self._settings_page = render_settings_page()
        self._app = web.Application()
        self._app.router.add_get("/api/build", self._handle_build)
        self._app.router.add_get("/api/settings", self._handle_get_settings)
        self._app.router.add_post("/api/settings", self._handle_post_settings)
        self._app.router.add_post("/api/diagnostics/llm", self._handle_diagnose_llm)
        self._app.router.add_post("/api/settings/schedule", self._handle_post_schedule)
        self._app.router.add_post("/api/settings/values", self._handle_post_values)
        self._app.router.add_post("/api/settings/vote-similarity", self._handle_post_vote)
        self._app.router.add_post("/api/settings/notify", self._handle_post_notify)
        self._app.router.add_get("/api/sources", self._handle_get_sources)
        self._app.router.add_post("/api/sources", self._handle_post_source)
        self._app.router.add_delete("/api/sources/{name}", self._handle_delete_source)
        self._app.router.add_get("/settings", self._handle_settings_page)
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
        from cyris.bootstrap import default_models, embedding_defaults
        from cyris.config import GRADE_D_KEYS, LLMProviderConfig
        from cyris.diagnostics.doctor import EMBEDDING_ENV
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
                bool(probe_cfg.account_id) if name == "workers_ai" else True
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
                "missing": sorted(key for key in GRADE_D_KEYS if key not in self._values),
                "providers": providers,
                "embedding_providers": embedding_providers,
                "languages": list(_language_names()),
                "writable": self._settings is not None,
            }
        )

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

        if self._settings is None:
            return web.json_response(
                {"ok": False, "error": "this deployment has no settings store to write"},
                status=409,
            )
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

        if provider == "none":
            # Nothing to call, and a leftover model must not outlive its provider.
            model = ""
            detail = "No model is called: digests list plain excerpts."
        else:
            probe = await probe_llm(candidate)
            if probe.status != "ok":
                return web.json_response({"ok": False, "error": probe.detail}, status=400)
            detail = probe.detail

        try:
            self._settings.set({"llm_provider.provider": provider, "llm_provider.model": model})
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            return web.json_response({"ok": False, "error": str(e)}, status=500)

        self._values.update({"llm_provider.provider": provider, "llm_provider.model": model})
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

        if self._settings is None:
            return web.json_response(
                {"ok": False, "error": "this deployment has no settings store to write"},
                status=409,
            )
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        try:
            times = validate_schedule([str(t).strip() for t in body.get("times") or []])
        except ValueError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)

        try:
            self._settings.set({"general.digest_schedule": times})
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            return web.json_response({"ok": False, "error": str(e)}, status=500)

        self._values["general.digest_schedule"] = times
        logger.info("Digest schedule set to %s", ", ".join(times))
        return web.json_response({"ok": True, "times": times, "note": "Effective next tick."})

    async def _handle_post_values(self, request: web.Request) -> web.Response:
        """Store any of the plain settings, all or nothing, each through its own rule."""
        from cyris.config import validate_setting

        if self._settings is None:
            return web.json_response(
                {"ok": False, "error": "this deployment has no settings store to write"},
                status=409,
            )
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        values = body.get("values") if isinstance(body, dict) else None
        if not isinstance(values, dict) or not values:
            return web.json_response(
                {"ok": False, "error": "values must name at least one setting"}, status=400
            )
        validated = {}
        for key, value in values.items():
            if key not in PLAIN_KEYS:
                return web.json_response(
                    {"ok": False, "error": f"{key} is saved from its own form"}, status=400
                )
            try:
                validated[key] = validate_setting(key, value)
            except ValueError as e:
                return web.json_response({"ok": False, "error": f"{key}: {e}"}, status=400)

        try:
            self._settings.set(validated)
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            return web.json_response({"ok": False, "error": str(e)}, status=500)

        self._values.update(validated)
        logger.info("Settings saved: %s", ", ".join(sorted(validated)))
        return web.json_response({"ok": True, "values": validated, "note": "Effective next run."})

    async def _handle_post_vote(self, request: web.Request) -> web.Response:
        """Store vote similarity's switch, embedder and seeds as one unit.

        Turning it on checks the embedder with one real call first, and this is
        the only writer of `enabled`, so no run meets an unverified embedder.
        Off stores without a call: a deployment with no embedding key must still
        be able to complete its settings.
        """
        from cyris.config import validate_setting

        if self._settings is None:
            return web.json_response(
                {"ok": False, "error": "this deployment has no settings store to write"},
                status=409,
            )
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        values = {}
        for field in ("enabled", "provider", "model", "max_seeds"):
            key = f"vote_similarity.{field}"
            if field not in body:
                return web.json_response({"ok": False, "error": f"{field} is required"}, status=400)
            try:
                values[key] = validate_setting(key, body[field])
            except ValueError as e:
                return web.json_response({"ok": False, "error": f"{key}: {e}"}, status=400)

        if values["vote_similarity.enabled"]:
            probe = await probe_embedder(
                values["vote_similarity.provider"], values["vote_similarity.model"]
            )
            if probe.status != "ok":
                return web.json_response({"ok": False, "error": probe.detail}, status=400)
            detail = probe.detail
        else:
            detail = "Not checked: vote similarity is off. Turning it on checks the embedder first."

        try:
            self._settings.set(values)
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            return web.json_response({"ok": False, "error": str(e)}, status=500)

        self._values.update(values)
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

        if self._settings is None:
            return web.json_response(
                {"ok": False, "error": "this deployment has no settings store to write"},
                status=409,
            )
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        if body.get("off") is True:
            try:
                self._settings.set({"notify.discord_webhook_url": ""})
            except Exception as e:  # noqa: BLE001 - the reason belongs in the response
                return web.json_response({"ok": False, "error": str(e)}, status=500)
            self._values["notify.discord_webhook_url"] = ""
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
            return web.json_response(
                {
                    "ok": False,
                    "error": (
                        "Paste a Discord webhook URL, or press Turn off to stop notifications."
                    ),
                },
                status=400,
            )

        probe = await probe_discord(url)
        if probe.status != "ok":
            return web.json_response({"ok": False, "error": probe.detail}, status=400)

        try:
            self._settings.set({"notify.discord_webhook_url": url})
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            return web.json_response({"ok": False, "error": str(e)}, status=500)

        self._values["notify.discord_webhook_url"] = url
        logger.info("Discord webhook saved")
        return web.json_response(
            {
                "ok": True,
                "discord_webhook_url": mask_discord_webhook_url(url),
                "detail": probe.detail,
                "note": "Saved. The next digest run picks this up.",
            }
        )

    async def _handle_get_sources(self, request: web.Request) -> web.Response:
        """What the pipeline is actually fetching.

        `email_match` rides along because it is source data (grade D);
        Cloudflare Email Routing is grade B and stays in the dashboard.
        """
        sources = self._effective_sources()
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
                    }
                    for s in sources.values()
                ],
            }
        )

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
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        if not source.name.strip():
            return web.json_response({"ok": False, "error": "name is required"}, status=400)

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

    async def _handle_settings_page(self, request: web.Request) -> web.Response:
        return web.Response(text=self._settings_page, content_type="text/html")

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        logger.info("Settings at http://%s:%d/settings", self._host, self._port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
