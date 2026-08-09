"""Vote-seeded similarity: judge an article by how close it sits to what was voted on.

The unit of preference here is deliberately narrower than a feed and wider than a
single article. A downvote on a lottery draw means "this kind of article", not
"this source" — suppressing 中央社財經 wholesale would take 1,202 unrelated
articles with it, while the class the vote actually points at is ~69.

Measured 2026-08-09 over the live store with gemini-embedding-001: seeded with the
two real downvoted titles, cosine ranks the whole 69-article class above every one
of a 900-article sample — in-class minimum 0.737 against out-of-class maximum
0.666. DEFAULT_THRESHOLD sits in that gap. See docs/vote-signal-measurement.md.

ponytail: pure-stdlib dot products. ~400 candidates x a handful of seeds is a few
million multiply-adds, which Python does in about a second. Reach for numpy only
when the whole corpus needs embedding, not for one digest's candidates.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Midpoint of the measured gap (0.737 in-class low / 0.666 out-of-class high).
# Sitting in the middle rather than at either edge keeps the same margin on both
# sides, since neither bound is guaranteed to hold on unseen articles.
DEFAULT_THRESHOLD = 0.70


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


@dataclass(frozen=True)
class SimilarityVerdict:
    """What the vote signal says about one candidate article."""

    url: str
    down_similarity: float
    up_similarity: float
    suppressed: bool

    @property
    def net(self) -> float:
        """Positive when the article leans towards what was upvoted."""
        return self.up_similarity - self.down_similarity


def judge(
    candidates: dict[str, list[float]],
    upvoted: list[list[float]],
    downvoted: list[list[float]],
    threshold: float = DEFAULT_THRESHOLD,
) -> list[SimilarityVerdict]:
    """Score every candidate against both vote classes.

    An article close to both is not suppressed: the upvote is the more specific
    signal, and a reader who liked something very like it should not lose it to a
    rejection of a broader neighbourhood.

    Args:
        candidates: url -> normalized embedding.
        upvoted: normalized embeddings of accepted articles.
        downvoted: normalized embeddings of rejected articles.
        threshold: cosine at or above which a downvote match suppresses.

    Returns:
        One verdict per candidate, ordered by descending net preference.
    """
    verdicts = []
    for url, vector in candidates.items():
        down = max_similarity(vector, downvoted)
        up = max_similarity(vector, upvoted)
        verdicts.append(
            SimilarityVerdict(
                url=url,
                down_similarity=down,
                up_similarity=up,
                suppressed=down >= threshold and up < down,
            )
        )
    return sorted(verdicts, key=lambda v: v.net, reverse=True)
