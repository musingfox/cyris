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

import json
import logging
from datetime import UTC, datetime, timedelta

from cyris.adapters.store.d1 import D1Queryable

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp_after(stored_latest: str | None) -> str:
    """A stamp that sorts after every stored one, even when the clock repeated or stepped back.

    Legacy rows carry second-precision stamps; `+` sorts below `.`, so any
    microsecond stamp of the same second still sorts above them.
    """
    now = _now()
    if stored_latest is not None:
        now = max(now, datetime.fromisoformat(stored_latest) + timedelta(microseconds=1))
    return now.isoformat(timespec="microseconds")


class D1PagesManifest:
    """The deployed site's path → hash map."""

    def __init__(self, client: D1Queryable) -> None:
        self._db = client

    def load(self) -> dict[str, str]:
        """The one generation whose rows carry the newest stamp, never a mix of two."""
        rows = self._db.query(
            "SELECT path, hash FROM pages_manifest "
            "WHERE updated_at = (SELECT MAX(updated_at) FROM pages_manifest)"
        ).rows
        return {row["path"]: row["hash"] for row in rows}

    def save(self, manifest: dict[str, str]) -> None:
        """Replace the manifest wholesale — it describes one deployment, not a history.

        Three statements, in this order, and the order is the atomicity. The
        insert writes the whole new generation under a stamp newer than every
        stored row, so `load()` returns it from that moment; a failure before
        it leaves the old generation current. The delete then removes rows no
        reader sees. A failure there leaves the new generation current.

        Two runs can save at once, and neither may touch a newer generation: the
        insert only overwrites a row whose stamp is older than its own, and the
        delete only removes rows older than its own. An older save that lands
        late leaves rows under its stamp that no reader sees, and the next save
        removes them.

        The insert is one statement, because D1 runs one statement atomically.
        Rejected: chunked multi-statement inserts, which need a commit marker and
        so a schema change; REST multi-statement transactionality, which is not
        documented (only the Worker `batch()` is a transaction); and
        insert-then-delete-by-path, which leaves the old union the new. The
        manifest travels as one JSON list in one bound parameter, and no compound
        SELECT is used, so D1's 5-term limit holds by construction.

        Size ceiling: the manifest is one JSON string of ~80 B a row, so 20,000 files
        (the upload token's cap) is ~1.6 MB, against D1's 2 MB string limit. D1's
        100,000-byte statement limit may also count the bound parameters, which would
        bring the wall much nearer: production held 260 rows, ~25 KB, on 2026-10-18,
        and at ~4 files a day the 100 KB line falls around 2027-04. Both are
        unverified against the REST endpoint. At the wall the save fails, the old
        manifest stays complete, and publishing stops; the fix is to prune the archive
        tail, not to add storage.

        Replaced, not merged: a path that left the site has to leave this table
        too, or the next deploy resurrects a file whose bytes Cloudflare may no
        longer hold and the whole deploy fails on one stale row.
        """
        if not manifest:
            raise ValueError("refusing to store an empty manifest — it would empty the site")
        latest_rows = self._db.query("SELECT MAX(updated_at) AS latest FROM pages_manifest").rows
        stamp = _stamp_after(latest_rows[0]["latest"])
        pairs = json.dumps([{"p": path, "h": digest} for path, digest in sorted(manifest.items())])
        # `WHERE true` is SQLite's required disambiguation for an upsert over a SELECT.
        self._db.query(
            "INSERT INTO pages_manifest (path, hash, updated_at) "
            "SELECT json_extract(value,'$.p'), json_extract(value,'$.h'), ? FROM json_each(?) "
            "WHERE true ON CONFLICT(path) DO UPDATE SET hash = excluded.hash, "
            "updated_at = excluded.updated_at WHERE excluded.updated_at > updated_at",
            [stamp, pairs],
        )
        self._db.query("DELETE FROM pages_manifest WHERE updated_at < ?", [stamp])
        logger.info("Pages manifest: %d file(s)", len(manifest))
