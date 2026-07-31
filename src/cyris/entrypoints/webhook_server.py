"""Email webhook server for newsletter processing."""

import json
import logging
from collections.abc import Awaitable, Callable

from aiohttp import web

from cyris.adapters.fetch.email_parser import parse_newsletter
from cyris.adapters.fetch.newsletter import newsletter_article
from cyris.domain.models import Article, SourceConfig

logger = logging.getLogger(__name__)


class EmailWebhookServer:
    """Webhook server for receiving newsletter emails."""

    def __init__(
        self,
        host: str,
        port: int,
        path: str,
        webhook_secret: str,
        sources: dict[str, SourceConfig],
        on_received: Callable[[list[Article]], Awaitable[None]],
    ):
        self._host = host
        self._port = port
        self._path = path
        self._secret = webhook_secret
        self._sources = sources
        self._on_received = on_received
        self._app = web.Application()
        self._app.router.add_post(path, self._handle_webhook)
        self._runner: web.AppRunner | None = None

    def _match_source(self, from_email: str) -> SourceConfig | None:
        """Find source config matching the sender email."""
        for source in self._sources.values():
            if source.email_match and from_email in source.email_match:
                return source
        return None

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        """Handle incoming webhook POST request."""
        # Validate secret
        secret = request.headers.get("X-Webhook-Secret", "")
        if secret != self._secret:
            return web.Response(status=401, text="Unauthorized")

        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return web.Response(status=400, text="Invalid JSON")

        from_email = payload.get("from", "")
        source = self._match_source(from_email)
        if source is None:
            return web.Response(status=404, text="Unknown sender")

        try:
            parsed = parse_newsletter(payload, source.name)
            article = newsletter_article(parsed, source)
            articles = [article] if article is not None else []
            await self._on_received(articles)
            return web.Response(status=200, text=f"Processed {len(articles)} articles")
        except Exception:
            logger.exception("Webhook processing failed")
            return web.Response(status=500, text="Internal error")

    async def start(self) -> None:
        """Start the webhook server."""
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        logger.info("Listening on %s:%d%s", self._host, self._port, self._path)

    async def stop(self) -> None:
        """Stop the webhook server."""
        if self._runner:
            await self._runner.cleanup()
