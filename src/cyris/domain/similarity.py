"""Vote-seeded similarity: judge an article by how close it sits to what was voted on.

The unit of preference here is deliberately narrower than a feed and wider than a
single article. A downvote on a lottery draw means "this kind of article", not
"this source" — suppressing the finance newswire wholesale would take 1,193 unrelated
articles with it, while the class the vote actually points at is 71.

Re-measured 2026-08-10 with gemini-embedding-001 over the whole 5,724-article store
(the 2026-08-09 pass sampled one source, which put the boundary in the wrong place):
seeded with the two real downvoted titles, cosine ranks the entire 71-article class
above everything else — in-class minimum 0.690 against out-of-class maximum 0.673.
The absolute cutoff measured there does not carry to another model, so the cutoff is
now relative to the window. See
docs/decisions/0006-embedding-thresholds-are-per-model-calibrations.md.

ponytail: pure-stdlib dot products. ~400 candidates x a handful of seeds is a few
million multiply-adds, which Python does in about a second. Reach for numpy only
when the whole corpus needs embedding, not for one digest's candidates.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

# A candidate is an outlier when its robust z-score against the window's down
# similarities exceeds this (Iglewicz and Hoaglin's recommended cut for the modified
# z-score). Relative, so a growing vote set cannot raise the count the way a fixed
# cosine did (measured 2026-08: 2 downvotes suppressed 8 articles, 24 suppressed 45).
# See docs/decisions/0017-suppression-is-a-relative-outlier-with-a-reason-and-a-clock.md.
OUTLIER_MARGIN = 3.5

# Below about ten candidates the median and MAD rest on two or three articles, so one
# article moves the cut. Production windows are hundreds of candidates. A floor on
# sample size, not a cosine, so it does not go stale as votes grow.
MIN_OUTLIER_WINDOW = 10

# Scales the median absolute deviation to a standard deviation for a normal sample.
_MAD_SCALE = 0.6745


def normalize(vector: list[float]) -> list[float]:
    """Scale to unit length so a dot product is the cosine."""
    norm = math.sqrt(math.fsum(x * x for x in vector))
    if norm == 0.0:
        return list(vector)
    return [x / norm for x in vector]


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two already-normalized vectors."""
    return math.fsum(x * y for x, y in zip(a, b, strict=True))


def max_similarity(candidate: list[float], seeds: list[list[float]]) -> float:
    """Closeness to the nearest seed, not to their average.

    A reader's downvotes do not form one blob: lottery draws and, say, sports
    recaps are separate neighbourhoods, and averaging them lands the centroid
    between the two where it matches neither. Nearest-seed keeps each vote acting
    on its own region.
    """
    if not seeds:
        return 0.0
    return max(cosine(candidate, seed) for seed in seeds)


def nearest(candidate: list[float], seeds: dict[str, list[float]]) -> tuple[str | None, float]:
    """The nearest seed's key and its cosine; `max_similarity` with the winner named."""
    if not seeds:
        return None, 0.0
    scored = ((url, cosine(candidate, seed)) for url, seed in seeds.items())
    return max(scored, key=lambda pair: pair[1])


def nearest_k(vector: list[float], seeds: dict[str, list[float]], k: int) -> list[str]:
    """The `k` closest seed keys, closest first; ties keep the seeds' order."""
    scored = [(key, cosine(vector, seed)) for key, seed in seeds.items()]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return [key for key, _ in scored[:k]]


@dataclass(frozen=True)
class SimilarityVerdict:
    """What the vote signal says about one candidate article."""

    url: str
    down_similarity: float
    up_similarity: float
    suppressed: bool
    nearest_up_url: str | None = None
    nearest_down_url: str | None = None
    outlier_score: float | None = None

    @property
    def net(self) -> float:
        """Positive when the article leans towards what was upvoted."""
        return self.up_similarity - self.down_similarity


def judge(
    candidates: dict[str, list[float]],
    upvoted: list[list[float]],
    downvoted: list[list[float]],
    *,
    upvoted_urls: list[str] | None = None,
    downvoted_urls: list[str] | None = None,
) -> list[SimilarityVerdict]:
    """Score every candidate against both vote classes, and flag the outliers.

    A candidate is suppressed when its down similarity is an outlier among the
    candidates judged together (see OUTLIER_MARGIN; a window smaller than
    MIN_OUTLIER_WINDOW scores none) and it is not closer to an
    upvote. An article close to both is not suppressed: the upvote is the more
    specific signal, and a reader who liked something very like it should not lose it
    to a rejection of a broader neighbourhood.

    Args:
        candidates: url -> normalized embedding.
        upvoted: normalized embeddings of accepted articles.
        downvoted: normalized embeddings of rejected articles.
        upvoted_urls: one URL per `upvoted` vector, to name each verdict's nearest
            upvote; without it the verdict names none.
        downvoted_urls: the same for `downvoted`.

    Returns:
        One verdict per candidate, ordered by descending net preference.
    """
    rows = []
    for url, vector in candidates.items():
        down_url, down = _closest(vector, downvoted, downvoted_urls)
        up_url, up = _closest(vector, upvoted, upvoted_urls)
        rows.append((url, down_url, down, up_url, up))
    scores = _outlier_scores([down for _, _, down, _, _ in rows])
    verdicts = [
        SimilarityVerdict(
            url=url,
            down_similarity=down,
            up_similarity=up,
            suppressed=score is not None and score > OUTLIER_MARGIN and up < down,
            nearest_up_url=up_url,
            nearest_down_url=down_url,
            outlier_score=score,
        )
        for (url, down_url, down, up_url, up), score in zip(rows, scores, strict=True)
    ]
    return sorted(verdicts, key=lambda v: v.net, reverse=True)


def _outlier_scores(values: list[float]) -> list[float | None]:
    """Robust z-score of each value against the others.

    None for every value when the window is below MIN_OUTLIER_WINDOW or the spread is zero.
    """
    if len(values) < MIN_OUTLIER_WINDOW:
        return [None] * len(values)
    middle = statistics.median(values)
    spread = statistics.median(abs(v - middle) for v in values)
    if spread == 0.0:
        return [None] * len(values)
    return [_MAD_SCALE * (v - middle) / spread for v in values]


def _closest(
    vector: list[float], seeds: list[list[float]], urls: list[str] | None
) -> tuple[str | None, float]:
    if urls is None:
        return None, max_similarity(vector, seeds)
    return nearest(vector, dict(zip(urls, seeds, strict=True)))
