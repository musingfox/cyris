"""What the deployed digest site is made of, kept in D1.

A Pages deployment is a **full snapshot**: any path absent from the manifest is
gone from the site. So publishing needs to know every file the archive contains,
which is why the archive used to have to exist on disk.

It does not. Cloudflare's asset store is account-wide and content-addressed, and
it already holds every byte cyris has ever deployed — `check-missing` answers
"I have all of them" for the whole archive. The only thing that must survive
between runs is the **list**: path → hash, a few KB. That is this table.

The bytes have a home too, and it is the deployed site: it serves back exactly
what was uploaded, byte for byte (verified 2026-08-27). So on the rare occasion
Cloudflare has evicted an old asset, it is re-fetched from the live URL rather
than from a copy cyris was keeping just in case.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from cyris.adapters.store.d1 import D1Queryable, chunk_rows

logger = logging.getLogger(__name__)


class D1PagesManifest:
    """The deployed site's path → hash map."""

    def __init__(self, client: D1Queryable) -> None:
        self._db = client

    def load(self) -> dict[str, str]:
        rows = self._db.query("SELECT path, hash FROM pages_manifest").rows
        return {row["path"]: row["hash"] for row in rows}

    def save(self, manifest: dict[str, str]) -> None:
        """Replace the manifest wholesale — it describes one deployment, not a history.

        Delete-then-insert rather than upsert: a path that left the site has to
        leave this table too, or the next deploy resurrects a file whose bytes
        Cloudflare may no longer hold and the whole deploy fails on one stale row.
        """
        if not manifest:
            raise ValueError("refusing to store an empty manifest — it would empty the site")
        now = datetime.now(UTC).isoformat(timespec="seconds")
        rows = [[path, digest, now] for path, digest in sorted(manifest.items())]
        self._db.query("DELETE FROM pages_manifest")
        for chunk in chunk_rows(rows, 3):
            values = ", ".join("(?, ?, ?)" for _ in chunk)
            self._db.query(
                f"INSERT INTO pages_manifest (path, hash, updated_at) VALUES {values}",
                [value for row in chunk for value in row],
            )
        logger.info("Pages manifest: %d file(s)", len(manifest))
