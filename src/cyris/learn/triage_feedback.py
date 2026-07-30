"""Collect triage feedback from article store."""

from datetime import UTC, datetime, timedelta

from cyris.adapters.store.article_store import ArticleStore
from cyris.domain.models import ArticleState, TriageFeedbackData


def collect_triage_feedback(
    store: ArticleStore,
    days: int,
    min_triaged: int = 3,
) -> TriageFeedbackData:
    """Extract triage feedback from article store.

    Args:
        store: ArticleStore instance.
        days: Number of days to look back for triaged articles.
        min_triaged: Minimum total triaged articles required.

    Returns:
        TriageFeedbackData with accepted and rejected articles.

    Raises:
        ValueError: If total triaged articles < min_triaged.
    """
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=days)

    # Get all accepted and rejected articles
    all_accepted = store.list_articles(state=ArticleState.ACCEPTED, limit=10000)
    all_rejected = store.list_articles(state=ArticleState.REJECTED, limit=10000)

    # Filter by triaged_at timestamp within days range
    accepted_in_range = []
    rejected_in_range = []

    # Only triaged_at-stamped articles are human labels; the rest are the
    # pipeline's own verdicts, and learning from those is a self-reinforcing loop.
    for article in all_accepted:
        if article.triaged_at is None:
            continue
        timestamp = article.triaged_at
        # Ensure timezone-aware comparison
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        if timestamp >= cutoff:
            accepted_in_range.append(article)

    for article in all_rejected:
        if article.triaged_at is None:
            continue
        timestamp = article.triaged_at
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        if timestamp >= cutoff:
            rejected_in_range.append(article)

    total_triaged = len(accepted_in_range) + len(rejected_in_range)
    if total_triaged < min_triaged:
        raise ValueError(
            f"Insufficient triage feedback: only {total_triaged} triaged articles "
            f"(minimum {min_triaged} required)"
        )

    return TriageFeedbackData(
        accepted_articles=accepted_in_range,
        rejected_articles=rejected_in_range,
        date_range_start=cutoff.isoformat(),
        date_range_end=now.isoformat(),
    )
