"""The settings web server: /settings and the APIs it calls."""

import logging
import os
from pathlib import Path

from aiohttp import web
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape
from pydantic import ValidationError

from cyris.adapters.output import html_digest
from cyris.diagnostics.doctor import probe_discord
from cyris.domain.models import SourceConfig
from cyris.service_layer.ports import ArticleRepository

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
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
        store: ArticleRepository,
        host: str = "127.0.0.1",
        port: int = 8766,
        settings=None,
        llm_provider=None,
        schedule: list[str] | None = None,
        max_featured: int = 5,
        sources: dict[str, SourceConfig] | None = None,
        sources_origin: str = "",
        source_store=None,
        notify_webhook: str = "",
    ) -> None:
        self._store = store
        self._host = host
        self._port = port
        # Without a settings store the page still renders, read-only: a
        # `backend = "json"` deployment has nowhere to put a runtime setting, so
        # its owner edits `cyris.toml` by hand.
        self._settings = settings
        self._llm_provider = llm_provider
        self._schedule = schedule or []
        self._max_featured = max_featured
        # Already resolved by `load_effective_config` — D1's `sources` table when
        # it has rows, `sources.yaml` otherwise. The page reports which, so a
        # half-migrated deployment does not look like a stale one.
        self._sources = sources or {}
        self._sources_origin = sources_origin
        # The write surface (§7 #15). Absent on a `backend = "json"` deployment,
        # where `sources.yaml` is the only home and the list stays read-only.
        self._source_store = source_store
        self._notify_webhook = notify_webhook
        self._settings_page = render_settings_page()
        self._app = web.Application()
        self._app.router.add_get("/api/build", self._handle_build)
        self._app.router.add_get("/api/settings", self._handle_get_settings)
        self._app.router.add_post("/api/settings", self._handle_post_settings)
        self._app.router.add_post("/api/diagnostics/llm", self._handle_diagnose_llm)
        self._app.router.add_post("/api/settings/schedule", self._handle_post_schedule)
        self._app.router.add_post("/api/settings/digest", self._handle_post_digest)
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
        """What is configured now, and which providers this machine could switch to."""
        from cyris.adapters.notify import mask_discord_webhook_url
        from cyris.bootstrap import default_models
        from cyris.config import LLMProviderConfig

        current = self._llm_provider
        providers = []
        models = default_models()
        for name in models:
            # Constructing it is what resolves the key from the environment, so
            # `configured` reflects what a run would actually find, not a guess.
            probe_cfg = LLMProviderConfig(provider=name)
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
        return web.json_response(
            {
                "provider": current.provider if current else None,
                "model": (current.model if current else "") or "",
                "providers": providers,
                "schedule": self._schedule,
                "max_featured": self._max_featured,
                "notify_webhook": mask_discord_webhook_url(self._notify_webhook),
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

        probe = await probe_llm(candidate)
        if probe.status != "ok":
            return web.json_response({"ok": False, "error": probe.detail}, status=400)

        try:
            self._settings.set({"llm_provider.provider": provider, "llm_provider.model": model})
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            return web.json_response({"ok": False, "error": str(e)}, status=500)

        self._llm_provider = candidate
        logger.info("LLM provider set to %s · %s", provider, model or "(default model)")
        return web.json_response(
            {
                "ok": True,
                "provider": provider,
                "model": model,
                "detail": probe.detail,
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

        self._schedule = times
        logger.info("Digest schedule set to %s", ", ".join(times))
        return web.json_response({"ok": True, "times": times, "note": "Effective next tick."})

    async def _handle_post_digest(self, request: web.Request) -> web.Response:
        """Set how many featured sections lead the page.

        Graded D because it is a reader's preference, not a measurement: how many
        headlines you want above the fold is not a number this codebase can derive.
        """
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
            max_featured = int(body.get("max_featured"))
        except (TypeError, ValueError):
            return web.json_response(
                {"ok": False, "error": "max_featured must be a whole number"}, status=400
            )
        if max_featured < 1:
            return web.json_response(
                {"ok": False, "error": "max_featured must be at least 1"}, status=400
            )

        try:
            self._settings.set({"digest.max_featured": max_featured})
        except Exception as e:  # noqa: BLE001 - the reason belongs in the response
            return web.json_response({"ok": False, "error": str(e)}, status=500)

        self._max_featured = max_featured
        logger.info("Featured cap set to %d", max_featured)
        return web.json_response(
            {"ok": True, "max_featured": max_featured, "note": "Effective next digest."}
        )

    async def _handle_post_notify(self, request: web.Request) -> web.Response:
        """Store a Discord webhook only after Discord confirms it exists."""
        from cyris.adapters.notify import mask_discord_webhook_url
        from cyris.config import DISCORD_WEBHOOK_ENV_VAR

        if self._settings is None:
            return web.json_response(
                {"ok": False, "error": "this deployment has no settings store to write"},
                status=409,
            )
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        url = (body.get("discord_webhook_url") or "").strip()
        if not url:
            return web.json_response(
                {
                    "ok": False,
                    "error": (
                        "paste a Discord webhook URL. Notifications cannot be turned off "
                        f"from /settings: to stop them, remove the {DISCORD_WEBHOOK_ENV_VAR} "
                        "Worker secret and any [notify] value in cyris.toml"
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

        self._notify_webhook = url
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
        """What the pipeline is actually fetching, and from which home.

        `email_match` rides along because it is source data (grade D);
        Cloudflare Email Routing is grade B and stays in the dashboard.
        """
        sources, origin = self._effective_sources()
        return web.json_response(
            {
                "origin": origin,
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

    def _effective_sources(self) -> tuple[dict[str, SourceConfig], str]:
        """The live table when there is one, else the startup snapshot.

        Re-reading matters after the first write: `sources_origin` was resolved
        once at startup, and an empty table then meant "sources.yaml".
        """
        if self._source_store is None:
            return self._sources, self._sources_origin or "unknown"
        live = self._source_store.list_sources()
        return (live, "d1") if live else (self._sources, self._sources_origin or "unknown")

    def _seed_before_writing(self) -> None:
        """Put today's effective list in D1 before the table's first edit.

        An empty table means "use sources.yaml", so writing a single source into
        one would flip the pipeline to D1 with that source alone and silently
        stop every feed the file serves. Seeding first makes the first edit mean
        what it looks like: `cyris sources push`, then the change.
        """
        if not self._source_store.list_sources() and self._sources:
            self._source_store.replace_all(self._sources)

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
            self._seed_before_writing()
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
            self._seed_before_writing()
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
