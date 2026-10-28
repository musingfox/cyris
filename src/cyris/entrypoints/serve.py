"""The digest's HTTP surface: the pages `[html_output]` wrote, and nothing else.

This is the archive with a URL for a deployment without one — no Pages project,
no app Worker fronting it — for readers on a network the machine is already on.
It opens no store and takes no vote: `GET /api/vote` is answered like any other
unknown path, so the pages show no vote buttons and no gated links; that surface
belongs to `cyris triage-ui`.

`visitor.theme` — a reader of `digest.theme` — makes the pages live: a palette
saved elsewhere is injected into the next page view, the same override the app
Worker injects on Cloudflare and triage-ui injects locally. `None` keeps every
page as it was written.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from aiohttp import web

from cyris.adapters.output.themes import theme_style
from cyris.entrypoints.triage_server import PAGE_NAME

logger = logging.getLogger(__name__)

# Every relative name a written page links to: the pages themselves name
# issues and the index; the templates pin the favicon beside them.
FILE_PAGES = ("favicon.svg",)


class PageServer:
    """The output directory as three routes: `/`, `/<name>` and 404s.

    Re-read from disk per request, so a run's new issue appears without this
    server noticing it; a serving process also holds no store, no settings and
    no tokens worth exposing to whoever can reach the interface it binds.
    """

    def __init__(
        self,
        output_dir: str | Path,
        theme: Callable[[], str] | None = None,
    ) -> None:
        self._dir = Path(output_dir)
        self._theme = theme or (lambda: "default")

    def build_app(self) -> web.Application:
        """The routes alone, for a caller that drives its own runner or tests."""
        if not self._dir.is_dir():
            raise ValueError(f"[html_output] output_dir is not a directory: {self._dir}")
        app = web.Application()
        app.router.add_get("/", self._handle_page)
        app.router.add_get("/{name:.+}", self._handle_page)
        return app

    async def start(self, host: str, port: int) -> web.AppRunner:
        runner = web.AppRunner(self.build_app())
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        try:
            await site.start()
        except OSError as e:
            await runner.cleanup()
            raise RuntimeError(f"cannot listen on {host}:{port}: {e}") from e
        logger.info("Serving %s on http://%s:%s/", self._dir, host, port)
        return runner

    async def _handle_page(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info.get("name", "index.html")
        if "/" in name or "\\" in name or ".." in name:  # a path, not a page
            raise web.HTTPNotFound()
        if "." not in name:  # the extensionless URL Pages serves
            name += ".html"
        stem = name.removesuffix(".html") or "index"
        if name not in FILE_PAGES and not PAGE_NAME.match(stem):
            raise web.HTTPNotFound()
        path = self._dir / name
        if not path.is_file():
            raise web.HTTPNotFound()
        # A palette set elsewhere reaches the next page view; the tokens as
        # written are the default, and that page passes untouched. Either way
        # the reader revalidates: a cached copy would keep the old palette
        # through a save, which is the one thing a theme switch cannot show.
        if not theme_style(self._theme()):
            return web.FileResponse(path, headers={"Cache-Control": "no-cache"})
        page = path.read_text(encoding="utf-8")
        style = theme_style(self._theme())
        return web.Response(
            text=page.replace("</head>", f"<style>{style}</style>\n</head>", 1),
            content_type="text/html",
            headers={"Cache-Control": "no-cache"},
        )


async def run_serve(
    host: str,
    port: int,
    output_dir: str | Path,
    stop: asyncio.Event,
    theme: Callable[[], str] | None = None,
) -> None:
    """Serve until `stop` is set or the caller is cancelled."""
    runner = await PageServer(output_dir, theme=theme).start(host, port)
    try:
        await stop.wait()
    finally:
        await runner.cleanup()
