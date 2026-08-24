"""Lightweight triage web server for article classification."""

import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import web

from cyris.adapters.output.article_export import ArticleExporter
from cyris.domain.models import ArticleState
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
        vault_path: Path | None = None,
        host: str = "127.0.0.1",
        port: int = 8766,
    ) -> None:
        self._store = store
        self._vault_path = vault_path
        self._host = host
        self._port = port
        self._app = web.Application()
        self._app.router.add_get("/api/articles", self._handle_list)
        self._app.router.add_get("/api/stats", self._handle_stats)
        self._app.router.add_post("/api/articles/accept", self._handle_accept)
        self._app.router.add_post("/api/articles/reject", self._handle_reject)
        self._app.router.add_post("/api/articles/undo", self._handle_undo)
        self._app.router.add_get("/", self._handle_index)
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

        # Try to export if vault_path is configured
        exported = False
        if self._vault_path:
            try:
                articles = self._store.get_by_urls([url])
                if articles:
                    ArticleExporter().export_to_vault(articles, self._vault_path, folder="Reading")
                    exported = True
                    logger.info("Exported article to vault: %s", url)
            except Exception as e:
                logger.warning("Failed to export article %s: %s", url, e)

        return web.json_response({"ok": True, "exported": exported})

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

        # Update state first
        updated = self._store.reject([url], reason=RejectReason.MANUAL_TRIAGE)
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

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        logger.info("Triage UI at http://%s:%d", self._host, self._port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
