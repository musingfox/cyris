"""Use case: judge this run's candidates against what the reader has voted on.

This runs over *every* candidate, not just the scored ones. The scorer skips news
and fan tiers (`scoring.select_scorable`), and the class that provoked the first
downvote — a newswire's lottery draw reports — is news-tagged, so a score adjustment would
never have reached it. Vote similarity therefore has to be its own pass.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from cyris.domain.models import ArticleState, StoredArticle
from cyris.domain.similarity import DEFAULT_THRESHOLD, SimilarityVerdict, judge, nearest_k
from cyris.service_layer.ports import ArticleRepository, Embedder

logger = logging.getLogger(__name__)


@dataclass
class VoteSimilarityReport:
    verdicts: dict[str, SimilarityVerdict] = field(default_factory=dict)
    suppressed_urls: list[str] = field(default_factory=list)
    upvote_seeds: int = 0
    downvote_seeds: int = 0
    skipped_reason: str = ""
    # url -> title vector for each candidate judged, so a later pass this run
    # (topic tracking) need not embed the same titles again.
    candidate_vectors: dict[str, list[float]] = field(default_factory=dict)
    # The same for the vote seeds and their stored titles, so a later pass can name
    # a candidate's neighbours without embedding the votes again.
    up_seed_vectors: dict[str, list[float]] = field(default_factory=dict)
    down_seed_vectors: dict[str, list[float]] = field(default_factory=dict)
    seed_titles: dict[str, str] = field(default_factory=dict)

    def nearest_titles(self, url: str, k: int) -> tuple[list[str], list[str]]:
        """Titles of the `k` upvotes and `k` downvotes closest to a judged candidate."""
        vector = self.candidate_vectors[url]
        return (
            [self.seed_titles[u] for u in nearest_k(vector, self.up_seed_vectors, k)],
            [self.seed_titles[u] for u in nearest_k(vector, self.down_seed_vectors, k)],
        )

    @property
    def ran(self) -> bool:
        return not self.skipped_reason


def _voted(store: ArticleRepository, state: ArticleState, limit: int) -> list[StoredArticle]:
    """The most recently human-stamped articles in a state.

    `triaged_at` is what separates a reader's decision from the pipeline's own
    verdict, and the pipeline is demonstrably wrong on exactly the class the first
    vote was about: 48 of the 50 lottery rows it had seen were marked accepted.
    Seeding from unstamped rows would teach the filter the mistake it exists to fix.

    The whole state has to be pulled before filtering — `list_articles` orders by
    first_seen_at, so a recency-limited page of 3,000+ pipeline verdicts would
    contain no human votes at all. Same approach as `collect_triage_feedback`.
    """
    rows = [a for a in store.list_articles(state=state, limit=100_000) if a.triaged_at]
    return sorted(rows, key=lambda a: a.triaged_at, reverse=True)[:limit]


async def judge_by_votes(
    store: ArticleRepository,
    embedder: Embedder,
    candidates: list[StoredArticle],
    *,
    threshold: float | None = DEFAULT_THRESHOLD,
    max_seeds: int,
) -> VoteSimilarityReport:
    """Compare candidates against upvoted and downvoted articles.

    Degrades to an empty report rather than raising: a digest must still go out
    if the embedding API is down.
    """
    if threshold is None:
        return VoteSimilarityReport(skipped_reason="no calibrated threshold for this model")
    if not candidates:
        return VoteSimilarityReport(skipped_reason="no candidates")

    up = _voted(store, ArticleState.ACCEPTED, max_seeds)
    down = _voted(store, ArticleState.REJECTED, max_seeds)
    if not down and not up:
        return VoteSimilarityReport(skipped_reason="no human-voted articles yet")

    # An article the reader already ruled on is not a candidate for the same ruling:
    # it would match its own seed at cosine 1.0 and report a decision that was
    # already made, drowning out the generalisation that is the point here.
    seed_urls = {a.url for a in up + down}
    candidates = [a for a in candidates if a.url not in seed_urls]
    if not candidates:
        return VoteSimilarityReport(skipped_reason="every candidate was already voted on")

    try:
        seed_vectors = await embedder.embed([a.title for a in up + down])
        candidate_vectors = await embedder.embed([a.title for a in candidates])
        # Each vector is paired back to its article by position below.
        if len(seed_vectors) != len(up + down) or len(candidate_vectors) != len(candidates):
            raise ValueError("the embedder answered a different number of vectors than texts")
    except Exception as e:
        logger.warning("Vote similarity unavailable, digest continues unfiltered: %s", e)
        return VoteSimilarityReport(skipped_reason=f"embedding failed: {e}")

    # Paired before the empty vectors drop out, so each URL stays with its own vector.
    up_seeds = {a.url: v for a, v in zip(up, seed_vectors[: len(up)], strict=True) if v}
    down_seeds = {a.url: v for a, v in zip(down, seed_vectors[len(up) :], strict=True) if v}
    up_vectors = list(up_seeds.values())
    down_vectors = list(down_seeds.values())
    by_url = {a.url: v for a, v in zip(candidates, candidate_vectors, strict=True) if v}

    verdicts = judge(
        by_url,
        up_vectors,
        down_vectors,
        threshold,
        upvoted_urls=list(up_seeds),
        downvoted_urls=list(down_seeds),
    )
    suppressed = [v.url for v in verdicts if v.suppressed]
    logger.info(
        "Vote similarity: %d candidate(s) judged against %d up / %d down seed(s), %d suppressed",
        len(by_url),
        len(up_vectors),
        len(down_vectors),
        len(suppressed),
    )
    return VoteSimilarityReport(
        verdicts={v.url: v for v in verdicts},
        suppressed_urls=suppressed,
        upvote_seeds=len(up_vectors),
        downvote_seeds=len(down_vectors),
        candidate_vectors=by_url,
        up_seed_vectors=up_seeds,
        down_seed_vectors=down_seeds,
        seed_titles={a.url: a.title for a in up + down if a.url in up_seeds or a.url in down_seeds},
    )
