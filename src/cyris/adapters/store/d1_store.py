"""D1-backed ArticleStore: the same 13 methods, one SQL query each.

Behaviour matches `ArticleStore` with one deliberate difference: dedup is by URL
across the whole table, not across the last 8 days of partitions. The window in
the JSON store is a scan-cost optimisation, not a rule — a URL primary key does
what it was approximating, and does it exactly.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from cyris.adapters.store.d1 import D1Queryable, chunk_rows
from cyris.domain.models import Article, ArticleState, SaveResult, StoredArticle

logger = logging.getLogger(__name__)

COLUMNS = (
    "url",
    "original_id",
    "title",
    "content",
    "author",
    "published_at",
    "source_name",
    "source_tier",
    "source_tags",
    "ref_urls",
    "state",
    "first_seen_at",
    "digest_date",
    "rejection_reason",
    "score",
    "language",
    "scored_at",
    "triaged_at",
    "exported_at",
)

# `WHERE url IN (?, ?, ...)` plus whatever the statement sets, kept under 100.
_URLS_PER_STATEMENT = 90

_SORT_EXPRESSIONS = {
    "first_seen_at": "first_seen_at {dir}",
    "published_at": "published_at {dir}",
    "title": "title {dir}",
    # `IS NULL ASC` is not a typo: unscored articles sort last in both
    # directions, which is what the JSON store's ±inf sort key does.
    "score": "score IS NULL ASC, score {dir}",
    # Chinese first: the same zh/en/other ordering the JSON store applies.
    "language": "CASE language WHEN 'zh' THEN 0 WHEN 'en' THEN 1 ELSE 2 END {dir}",
}


def _iso(value: datetime | None) -> str | None:
    """Normalise to UTC ISO8601 with microseconds, so string ordering is time ordering."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _to_row(article: StoredArticle) -> list[Any]:
    return [
        article.url,
        article.original_id,
        article.title,
        article.content,
        article.author,
        _iso(article.published_at),
        article.source_name,
        str(article.source_tier),
        json.dumps(article.source_tags),
        json.dumps(article.ref_urls),
        str(article.state),
        _iso(article.first_seen_at),
        article.digest_date,
        article.rejection_reason,
        article.score,
        article.language,
        _iso(article.scored_at),
        _iso(article.triaged_at),
        _iso(article.exported_at),
    ]


def _from_row(row: dict[str, Any]) -> StoredArticle:
    data = dict(row)
    data["source_tags"] = json.loads(row.get("source_tags") or "[]")
    data["ref_urls"] = json.loads(row.get("ref_urls") or "[]")
    return StoredArticle.model_validate(data)


class D1ArticleStore:
    """ArticleRepository over Cloudflare D1."""

    def __init__(self, client: D1Queryable) -> None:
        self._db = client

    # ---- write ----------------------------------------------------------

    def save(self, articles: list[Article], now: datetime | None = None) -> SaveResult:
        """Insert new articles, ignoring URLs already stored."""
        if not articles:
            return SaveResult(saved_count=0, skipped_count=0)

        now = now or datetime.now(UTC)
        rows = [_to_row(StoredArticle.from_article(a, first_seen_at=now)) for a in articles]

        placeholders = "(" + ", ".join("?" * len(COLUMNS)) + ")"
        saved = 0
        for chunk in chunk_rows(rows, len(COLUMNS)):
            sql = (
                f"INSERT OR IGNORE INTO stored_articles ({', '.join(COLUMNS)}) "
                f"VALUES {', '.join(placeholders for _ in chunk)}"
            )
            saved += self._db.query(sql, [v for row in chunk for v in row]).changes

        logger.info("Saved %d new articles to D1 (%d skipped)", saved, len(articles) - saved)
        return SaveResult(saved_count=saved, skipped_count=len(articles) - saved)

    def import_articles(
        self, articles: list[StoredArticle], on_progress: Callable[[int, int], None] | None = None
    ) -> int:
        """Copy rows in as-is, keeping state, scores and triage stamps.

        `INSERT OR IGNORE`, so re-running a migration never overwrites a decision
        already made in D1 — the local file it came from may be stale by then.
        That also makes an interrupted migration resumable: run it again and it
        picks up where it stopped.
        """
        if not articles:
            return 0
        placeholders = "(" + ", ".join("?" * len(COLUMNS)) + ")"
        chunks = chunk_rows([_to_row(a) for a in articles], len(COLUMNS))
        imported = 0
        for done, chunk in enumerate(chunks, start=1):
            sql = (
                f"INSERT OR IGNORE INTO stored_articles ({', '.join(COLUMNS)}) "
                f"VALUES {', '.join(placeholders for _ in chunk)}"
            )
            imported += self._db.query(sql, [v for row in chunk for v in row]).changes
            if on_progress:
                on_progress(done, len(chunks))
        return imported

    def update_states(
        self, url_to_state: dict[str, tuple[ArticleState, str | None]], digest_date: str
    ) -> int:
        """Apply the digest's verdicts, leaving human-stamped rows alone."""
        try:
            datetime.strptime(digest_date, "%Y-%m-%d")
        except ValueError as e:
            raise ValueError(f"Invalid digest_date format: {digest_date}") from e

        if not url_to_state:
            return 0

        by_verdict: dict[tuple[ArticleState, str | None], list[str]] = {}
        for url, verdict in url_to_state.items():
            by_verdict.setdefault(verdict, []).append(url)

        updated = 0
        for (state, reason), urls in by_verdict.items():
            for i in range(0, len(urls), _URLS_PER_STATEMENT):
                batch = urls[i : i + _URLS_PER_STATEMENT]
                sql = (
                    "UPDATE stored_articles SET state = ?, digest_date = ?, rejection_reason = ? "
                    f"WHERE triaged_at IS NULL AND url IN ({', '.join('?' * len(batch))})"
                )
                updated += self._db.query(sql, [str(state), digest_date, reason, *batch]).changes

        logger.info("Updated states for %d articles", updated)
        return updated

    def update_article_state(
        self, url: str, state: ArticleState, reason: str | None = None
    ) -> bool:
        """Update one article's state. Undoing a triage clears the human stamp too."""
        sets = "state = ?, digest_date = ?, rejection_reason = ?"
        params: list[Any] = [str(state), date.today().isoformat(), reason]
        if state == ArticleState.PENDING:
            # Otherwise the update_states guard would keep the row from ever being judged.
            sets += ", triaged_at = NULL"
        params.append(url)
        sql = f"UPDATE stored_articles SET {sets} WHERE url = ?"
        return self._db.query(sql, params).changes > 0

    def accept(self, urls: list[str]) -> int:
        return sum(self.update_article_state(url, ArticleState.ACCEPTED) for url in urls)

    def reject(self, urls: list[str], reason: str) -> int:
        return sum(
            self.update_article_state(url, ArticleState.REJECTED, reason=reason) for url in urls
        )

    def reset_to_pending(self, url: str) -> bool:
        return self.update_article_state(url, ArticleState.PENDING)

    def update_scores(
        self, url_to_score_lang: dict[str, tuple[float, str]], scan_days: int = 30
    ) -> int:
        """Write scores back. `scan_days` is ignored — SQL has no partitions to scan."""
        del scan_days
        if not url_to_score_lang:
            return 0

        rows = [[url, score, lang] for url, (score, lang) in url_to_score_lang.items()]
        updated = 0
        for chunk in chunk_rows(rows, 3):
            # One UPDATE per chunk instead of one per URL: a scoring run touches
            # ~150 articles, and that many round trips is minutes, not seconds.
            # A VALUES list, never `SELECT ? UNION ALL SELECT ?`: D1 caps a
            # compound SELECT at 5 terms, so the UNION form died on any batch
            # past five articles — measured against the live database.
            values = ", ".join("(?, ?, ?)" for _ in chunk)
            sql = (
                f"WITH v(url, score, language) AS (VALUES {values}) "
                "UPDATE stored_articles SET score = v.score, language = v.language "
                "FROM v WHERE stored_articles.url = v.url"
            )
            updated += self._db.query(sql, [value for row in chunk for value in row]).changes
        return updated

    def update_triage_timestamp(self, urls: list[str], triaged_at: datetime) -> int:
        """Stamp rows as human-decided. This is what vote similarity seeds from."""
        if not urls:
            return 0
        updated = 0
        for i in range(0, len(urls), _URLS_PER_STATEMENT):
            batch = urls[i : i + _URLS_PER_STATEMENT]
            sql = (
                "UPDATE stored_articles SET triaged_at = ? "
                f"WHERE url IN ({', '.join('?' * len(batch))})"
            )
            updated += self._db.query(sql, [_iso(triaged_at), *batch]).changes
        logger.info("Updated triage timestamps for %d articles", updated)
        return updated

    def delete_articles(
        self, state: ArticleState | list[ArticleState], older_than_days: int | None = None
    ) -> int:
        if older_than_days is not None and older_than_days < 0:
            raise ValueError("older_than_days must be non-negative")

        states = [str(s) for s in (state if isinstance(state, list) else [state])]
        sql = (
            f"DELETE FROM stored_articles "
            f"WHERE state IN ({', '.join('?' * len(states))}) AND triaged_at IS NULL"
        )
        params: list[Any] = list(states)
        if older_than_days is not None:
            sql += " AND first_seen_at < ?"
            params.append(_iso(datetime.now(UTC) - timedelta(days=older_than_days)))
        deleted = self._db.query(sql, params).changes
        if deleted:
            logger.info("Deleted %d articles", deleted)
        return deleted

    # ---- read -----------------------------------------------------------

    def load_by_time_range(
        self, start: datetime, end: datetime, state_filter: ArticleState | None = None
    ) -> list[StoredArticle]:
        """Articles first seen in [start, end), oldest first."""
        sql = (
            f"SELECT {', '.join(COLUMNS)} FROM stored_articles "
            "WHERE first_seen_at >= ? AND first_seen_at < ?"
        )
        params: list[Any] = [_iso(start), _iso(end)]
        if state_filter is not None:
            sql += " AND state = ?"
            params.append(str(state_filter))
        sql += " ORDER BY first_seen_at ASC"
        return [_from_row(r) for r in self._db.query(sql, params).rows]

    def list_articles(
        self,
        state: ArticleState | list[ArticleState] | None = None,
        limit: int = 100,
        offset: int = 0,
        sort_by: str = "first_seen_at",
        descending: bool = True,
    ) -> list[StoredArticle]:
        # ponytail: callers pass limit=100_000 to mean "everything". Fine while the
        # store holds thousands; if it ever holds millions, page it.
        if sort_by not in _SORT_EXPRESSIONS:
            raise ValueError(f"Invalid sort_by field: {sort_by}")

        sql = f"SELECT {', '.join(COLUMNS)} FROM stored_articles"
        params: list[Any] = []
        if state is not None:
            states = [str(s) for s in (state if isinstance(state, list) else [state])]
            sql += f" WHERE state IN ({', '.join('?' * len(states))})"
            params.extend(states)

        direction = "DESC" if descending else "ASC"
        order = _SORT_EXPRESSIONS[sort_by].format(dir=direction)
        sql += f" ORDER BY {order} LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return [_from_row(r) for r in self._db.query(sql, params).rows]

    def get_by_urls(self, urls: list[str]) -> list[StoredArticle]:
        if not urls:
            return []
        found: list[StoredArticle] = []
        for i in range(0, len(urls), _URLS_PER_STATEMENT):
            batch = urls[i : i + _URLS_PER_STATEMENT]
            sql = (
                f"SELECT {', '.join(COLUMNS)} FROM stored_articles "
                f"WHERE url IN ({', '.join('?' * len(batch))})"
            )
            found.extend(_from_row(r) for r in self._db.query(sql, batch).rows)
        return found

    def count_by_state(self) -> dict[ArticleState, int]:
        sql = "SELECT state, COUNT(*) AS n FROM stored_articles GROUP BY state"
        return {ArticleState(r["state"]): r["n"] for r in self._db.query(sql).rows}
