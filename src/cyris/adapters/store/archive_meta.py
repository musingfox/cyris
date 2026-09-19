"""What the archive page shows about past issues beyond their file names.

A read over tables other writers own; nothing here writes. The archive must list
every issue whatever this returns, because Pages recovery rebuilds from what the
live index lists — so a missing count is shown as no count, never as no row.
"""

from __future__ import annotations

from cyris.adapters.store.d1 import D1Queryable


class D1ArchiveMeta:
    """Per-issue facts the archive rows show, read from D1."""

    def __init__(self, client: D1Queryable) -> None:
        self._db = client

    def article_counts(self) -> dict[tuple[str, str], int]:
        """`{(date, period): articles_included}` from each issue's latest `usage_log` row.

        A re-run of a period appends a second row; the later one is what the
        published page shows. Ordered ascending so the last row read wins.
        """
        rows = self._db.query(
            "SELECT digest_date, period, articles_included FROM usage_log"
            " WHERE digest_date IS NOT NULL AND period IS NOT NULL ORDER BY logged_at"
        ).rows
        return {(row["digest_date"], row["period"]): row["articles_included"] for row in rows}
