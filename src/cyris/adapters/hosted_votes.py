"""A hosted tenant's votes: the queue its Durable Object holds, drained at the start of a run.

A reader's vote on a hosted page reaches the tenant's `hosted_votes` table through the hosted
Worker (`workers/app/src/hosted_vote.js`), which takes the tenant from the reader's session and
refuses a URL the tenant's `stored_articles` lacks. The Worker only queues the vote. This module
is the other half and the one writer of what a vote means: it hands the queue to
`apply_promotions`, the rule `promote-sync` writes the self-hosted queue with, so the stamp on
`triaged_at`, the topic vote and their one-click window are written by one piece of code, and a
hosted run's similarity reads a hosted vote exactly as a self-hosted run reads a synced one.
docs/decisions/0039-a-hosted-vote-is-queued-by-the-worker-and-applied-by-the-run.md
"""

from __future__ import annotations

from cyris.adapters.promotions import PromotedArticle, SyncResult, apply_promotions
from cyris.adapters.store.d1 import D1Queryable
from cyris.service_layer.ports import ArticleRepository, TopicRepository

_PENDING = "SELECT id, url, vote, ts FROM hosted_votes ORDER BY id"
# Rows are taken in `id` order with no limit, so `id <= max` is exactly the rows read: a vote
# queued while the run applies the others keeps its row for the next run.
_ACK = "DELETE FROM hosted_votes WHERE id <= ?"


def sync_hosted_votes(
    db: D1Queryable, store: ArticleRepository, topics: TopicRepository | None = None
) -> SyncResult:
    """Apply the votes queued in the tenant's database, then delete exactly those rows.

    The delete follows the write, as `sync_promotions`' ACK does, so a failure leaves the votes
    queued for the next run.
    """
    rows = db.query(_PENDING).rows
    if not rows:
        return SyncResult(0)
    promotions = [PromotedArticle(url=row["url"], vote=row["vote"], ts=row["ts"]) for row in rows]
    result = apply_promotions(promotions, store, topics)
    db.query(_ACK, [rows[-1]["id"]])
    return result
