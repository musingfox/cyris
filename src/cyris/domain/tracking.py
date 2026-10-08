"""Topic tracking: which candidates sit close enough to a topic the reader wrote.

The seed is the reader's own sentence, not a keyword list. The tracker removed on
2026-08-24 matched listed words, so every phrasing nobody listed was a miss, and
each match then cost an LLM call to confirm. A sentence's vector reaches the
phrasings no list names, and judging it is arithmetic.
"""

from __future__ import annotations

from cyris.domain.similarity import cosine


def hits(
    seed: list[float], candidates: dict[str, list[float]], threshold: float
) -> list[tuple[str, float]]:
    """Each candidate whose cosine to `seed` reaches `threshold`, nearest first.

    Args:
        seed: the topic's normalized embedding.
        candidates: key -> normalized embedding; an empty vector never hits.
        threshold: cosine at or above which a candidate is a hit.
    """
    scored = [(key, cosine(seed, vector)) for key, vector in candidates.items() if vector]
    return sorted(
        ((key, score) for key, score in scored if score >= threshold),
        key=lambda pair: pair[1],
        reverse=True,
    )
