"""A hosted tenant's site: the pages its runs publish, kept in the tenant's own Durable Object.

Hosted mode's publisher. `bootstrap.build_deps` wires it into the seam the Pages publisher fills
(`publish_site`, `page_serves`, `site_filenames`) when the store carries a tenant id, so a hosted
run renders its pages as every run does and then writes them through the gateway into the
tenant's `hosted_pages` table (`adapters/store/hosted_schema.sql`), never to a Pages project.
The hosted Worker reads them back from the same object and serves them under `/t/<tenant>/`
after checking the reader's sign-in.
docs/decisions/0029-a-hosted-tenants-pages-live-in-its-durable-object.md.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import PurePosixPath

from cyris.adapters.store.d1 import D1Queryable

logger = logging.getLogger(__name__)

# The path the hosted Worker serves a tenant's site under: `/t/<tenant>/<page>`. `SITE_ROUTE` in
# workers/app/src/hosted_pages.js reads the same shape; tests/test_hosted_config.py holds them.
SITE_PATH = "/t/{tenant}"

# What a page is served as, by its suffix: the site holds HTML pages and its favicon.
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".svg": "image/svg+xml",
}

# A digest page, "/2026-11-18-morning.html": written after every other page of a publish, so a
# digest page in the table means its raw page and the archive already went in before it.
_DIGEST_PAGE = re.compile(r"^/\d{4}-\d{2}-\d{2}-[^/]+(?<!-raw)\.html$")

_UPSERT = (
    "INSERT INTO hosted_pages (path, body, content_type, updated_at) VALUES (?, ?, ?, ?) "
    "ON CONFLICT(path) DO UPDATE SET body = excluded.body, "
    "content_type = excluded.content_type, updated_at = excluded.updated_at"
)


# The tenant's settings page under its site: the page every edition shares, which the hosted
# Worker serves (ADR-0043). `SHARED_PAGE` in workers/app/src/hosted_shared_settings.js;
# tests/test_hosted_config.py holds them.
SETTINGS_PAGE = "/settings"


def tenant_site(domain: str, tenant: str) -> str:
    """The tenant's site on the hosted Worker, as host and path: `<domain>/t/<tenant>`."""
    return domain + SITE_PATH.format(tenant=tenant)


def tenant_settings_path(tenant: str) -> str:
    """Where a tenant's pages link Settings: its own page on the hosted Worker, by path."""
    return SITE_PATH.format(tenant=tenant) + SETTINGS_PAGE


def _slash(path: str) -> str:
    return path if path.startswith("/") else f"/{path}"


class HostedPages:
    """The pages of one hosted tenant's site, in its Durable Object behind the gateway."""

    def __init__(
        self,
        client: D1Queryable,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._db = client
        self._now = now

    def publish(self, new_files: dict[str, bytes], slug: str) -> bool:
        """Write `new_files` into the tenant's site; True once every page is in it.

        Each body is a bound value, never part of the statement, because a raw page is over a
        statement's 100 KB. `slug` names the issue the run published; the write itself is the
        receipt, since the Worker serves exactly the row it reads.
        """
        stamp = self._now().isoformat(timespec="seconds")
        rows = []
        for path, data in new_files.items():
            path = _slash(path)
            content_type = CONTENT_TYPES.get(PurePosixPath(path).suffix)
            if content_type is None:
                logger.error("Hosted pages: the site serves no page like %s; nothing written", path)
                return False
            try:
                body = data.decode("utf-8")
            except UnicodeDecodeError:
                logger.error("Hosted pages: %s is not UTF-8 text; nothing written", path)
                return False
            rows.append((path, body, content_type))
        rows.sort(key=lambda row: bool(_DIGEST_PAGE.match(row[0])))
        try:
            for path, body, content_type in rows:
                written = self._db.query(_UPSERT, [path, body, content_type, stamp]).changes
                if written != 1:
                    logger.error("Hosted pages: writing %s changed %d rows, not 1", path, written)
                    return False
        except Exception as e:
            logger.error("Hosted pages: the write for %s failed: %s", slug, e)
            return False
        logger.info("Stored %d page(s) of %s in the tenant's site", len(rows), slug)
        return True

    def serves(self, path: str, expected: bytes) -> bool:
        """Whether the site holds exactly `expected` at `path`, the bytes the Worker would serve."""
        rows = self._db.query("SELECT body FROM hosted_pages WHERE path = ?", [_slash(path)]).rows
        return bool(rows) and rows[0]["body"].encode("utf-8") == expected

    def filenames(self) -> list[str]:
        """Every page and asset the site holds, by file name, as the Pages manifest lists them."""
        rows = self._db.query("SELECT path FROM hosted_pages ORDER BY path").rows
        return [row["path"].lstrip("/") for row in rows]
