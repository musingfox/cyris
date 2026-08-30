"""Lightweight triage web server for article classification."""

import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import web
from pydantic import ValidationError

from cyris.domain.models import ArticleState, SourceConfig
from cyris.domain.triage import RejectReason
from cyris.service_layer.ports import ArticleRepository

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

_IMG_TAG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)


def _extract_first_image(html: str) -> str | None:
    """Extract the first <img src> URL from HTML content."""
    match = _IMG_TAG_RE.search(html)
    if match:
        src = match.group(1)
        if src.startswith("http"):
            return src
    return None


def _enrich_article(data: dict) -> dict:
    """Add computed fields: domain, favicon_url, image_url."""
    url = data.get("url", "")
    parsed = urlparse(url)
    domain = parsed.netloc or ""

    data["domain"] = domain
    data["favicon_url"] = (
        f"https://www.google.com/s2/favicons?domain={domain}&sz=64" if domain else ""
    )
    data["image_url"] = _extract_first_image(data.get("content", ""))
    return data


class TriageServer:
    """Serves triage UI and REST API for article accept/reject."""

    def __init__(
        self,
        store: ArticleRepository,
        host: str = "127.0.0.1",
        port: int = 8766,
        settings=None,
        llm_provider=None,
        schedule: list[str] | None = None,
        sources: dict[str, SourceConfig] | None = None,
        sources_origin: str = "",
        source_store=None,
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
        # Already resolved by `load_effective_config` — D1's `sources` table when
        # it has rows, `sources.yaml` otherwise. The page reports which, so a
        # half-migrated deployment does not look like a stale one.
        self._sources = sources or {}
        self._sources_origin = sources_origin
        # The write surface (§7 #15). Absent on a `backend = "json"` deployment,
        # where `sources.yaml` is the only home and the list stays read-only.
        self._source_store = source_store
        self._app = web.Application()
        self._app.router.add_get("/api/articles", self._handle_list)
        self._app.router.add_get("/api/stats", self._handle_stats)
        self._app.router.add_post("/api/articles/accept", self._handle_accept)
        self._app.router.add_post("/api/articles/reject", self._handle_reject)
        self._app.router.add_post("/api/articles/undo", self._handle_undo)
        self._app.router.add_get("/api/settings", self._handle_get_settings)
        self._app.router.add_post("/api/settings", self._handle_post_settings)
        self._app.router.add_post("/api/settings/schedule", self._handle_post_schedule)
        self._app.router.add_get("/api/sources", self._handle_get_sources)
        self._app.router.add_post("/api/sources", self._handle_post_source)
        self._app.router.add_delete("/api/sources/{name}", self._handle_delete_source)
        self._app.router.add_get("/settings", self._handle_settings_page)
        # Two paths, one page: `/` is the deck on localhost, and `/triage` is what
        # it answers on behind the Worker, where `/` belongs to the digest.
        self._app.router.add_get("/", self._handle_index)
        self._app.router.add_get("/triage", self._handle_index)
        self._app.router.add_static("/static", STATIC_DIR)
        self._runner: web.AppRunner | None = None

    async def _handle_index(self, request: web.Request) -> web.Response:
        index_path = STATIC_DIR / "index.html"
        return web.FileResponse(index_path)

    async def _handle_list(self, request: web.Request) -> web.Response:
        try:
            limit = int(request.query.get("limit", "20"))
            offset = int(request.query.get("offset", "0"))
        except ValueError:
            return web.json_response({"error": "invalid limit or offset"}, status=400)

        if limit < 1 or limit > 100:
            return web.json_response({"error": "limit must be 1-100"}, status=400)

        # Parse state filter
        # - "all" = no filter (return all states)
        # - specific state = filter by that state
        # - no param = backward compat (all non-rejected)
        state_param = request.query.get("state", "")
        state_filter: ArticleState | list[ArticleState] | None = None
        exclude_rejected = False

        if state_param == "all":
            state_filter = None  # No filter
        elif state_param:
            try:
                state_filter = ArticleState(state_param)
            except ValueError:
                return web.json_response({"error": f"invalid state: {state_param}"}, status=400)
        else:
            # No state param: backward compat (exclude rejected)
            exclude_rejected = True

        # Load articles
        articles = self._store.list_articles(
            state=state_filter,
            sort_by="score",
            descending=True,
            limit=limit,
            offset=offset,
        )

        # Exclude rejected articles when no state filter specified
        if exclude_rejected:
            articles = [a for a in articles if a.state != ArticleState.REJECTED]

        # Secondary sort: Chinese first on score ties (stable sort preserves score order)
        def _lang_key(a):
            lang_order = {"zh": 0, "en": 1}
            return lang_order.get(a.language, 2)

        articles.sort(key=_lang_key)
        # Re-sort by score descending (stable sort preserves language order within ties)
        articles.sort(
            key=lambda a: a.score if a.score is not None else float("-inf"),
            reverse=True,
        )

        # Compute total count of all matching articles (not just the batch)
        if state_param == "all":
            all_articles = self._store.list_articles(state=None)
            total = len(all_articles)
        elif state_filter:
            all_articles = self._store.list_articles(state=state_filter)
            total = len(all_articles)
        elif exclude_rejected:
            all_articles = self._store.list_articles(state=None)
            total = len([a for a in all_articles if a.state != ArticleState.REJECTED])
        else:
            total = len(articles)

        data = [_enrich_article(a.model_dump(mode="json")) for a in articles]
        return web.json_response({"articles": data, "total": total})

    async def _handle_accept(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        url = body.get("url")
        if not url:
            return web.json_response({"ok": False, "error": "url required"}, status=400)

        # Update state first
        updated = self._store.accept([url])
        if not updated:
            return web.json_response({"ok": False, "error": "article not found"}, status=404)
        self._store.update_triage_timestamp([url], datetime.now(UTC))

        return web.json_response({"ok": True})

    async def _handle_stats(self, request: web.Request) -> web.Response:
        counts = self._store.count_by_state()
        pending = counts.get(ArticleState.PENDING, 0)
        accepted = counts.get(ArticleState.ACCEPTED, 0)
        rejected = counts.get(ArticleState.REJECTED, 0)
        return web.json_response(
            {
                "pending": pending,
                "accepted": accepted,
                "rejected": rejected,
                "total": pending + accepted + rejected,
            }
        )

    async def _handle_reject(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        url = body.get("url")
        if not url:
            return web.json_response({"ok": False, "error": "url required"}, status=400)

        reason = body.get("reason", RejectReason.NOT_INTERESTED.value)
        if reason not in (RejectReason.ALREADY_KNOWN, RejectReason.NOT_INTERESTED):
            return web.json_response({"ok": False, "error": "invalid reason"}, status=400)

        updated = self._store.reject([url], reason=RejectReason(reason))
        if not updated:
            return web.json_response({"ok": False, "error": "article not found"}, status=404)
        self._store.update_triage_timestamp([url], datetime.now(UTC))

        return web.json_response({"ok": True})

    async def _handle_undo(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        url = body.get("url")
        if not url:
            return web.json_response({"ok": False, "error": "url required"}, status=400)

        updated = self._store.reset_to_pending(url)
        if not updated:
            return web.json_response({"ok": False, "error": "article not found"}, status=404)

        return web.json_response({"ok": True})

    async def _handle_get_settings(self, request: web.Request) -> web.Response:
        """What is configured now, and which providers this machine could switch to."""
        from cyris.bootstrap import _DEFAULT_MODELS
        from cyris.config import LLMProviderConfig

        current = self._llm_provider
        providers = []
        for name in _DEFAULT_MODELS:
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
                    "default_model": _DEFAULT_MODELS[name],
                    "configured": ready,
                }
            )
        return web.json_response(
            {
                "provider": current.provider if current else None,
                "model": (current.model if current else "") or "",
                "providers": providers,
                "schedule": self._schedule,
                "writable": self._settings is not None,
            }
        )

    async def _handle_post_settings(self, request: web.Request) -> web.Response:
        """Validate against the live provider, then write the D1 settings row.

        The order is the whole point. A bad provider or a mistyped model saved
        here would not surface until the next scheduled digest, hours later and
        after the fetch — so nothing is stored until a real call comes back.
        See `service_layer.doctor.probe_llm`.
        """
        from pydantic import ValidationError

        from cyris.config import LLMProviderConfig
        from cyris.service_layer.doctor import probe_llm

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
        return web.FileResponse(STATIC_DIR / "settings.html")

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        logger.info("Triage UI at http://%s:%d", self._host, self._port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
