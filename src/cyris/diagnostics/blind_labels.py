"""Blind labels on the filter's candidates: draw the sample, then score the filter
and the embedding preference against what the reader answered.

Off the pipeline, like the rest of `diagnostics/`: nothing here changes a digest.
The sample is stratified by news versus non-news and by the pipeline's verdict,
kept versus rejected, so the rejected rows a reader never sees are labeled too.
"""

from __future__ import annotations

import random
import statistics
from collections import Counter
from dataclasses import dataclass

from cyris.adapters.store.blind_labels import LabeledRow, PoolRow, SampleRow, Vote
from cyris.domain.models import ArticleState
from cyris.domain.similarity import judge
from cyris.service_layer.ports import Embedder

# (news, pipeline-rejected), in the fixed order a shortfall is handed round.
STRATA = ((True, False), (True, True), (False, False), (False, True))


def stratum_of(news: bool, state: ArticleState) -> tuple[bool, bool]:
    return news, state == ArticleState.REJECTED


def draw_sample(pool: list[PoolRow], *, size: int, min_rejected: int, seed: int) -> list[SampleRow]:
    """Draw `size` rows, an equal share from each stratum, in a seeded order.

    A stratum with fewer rows than its share gives the rest to the others, one
    row at a time in `STRATA` order. The order the page shows them in is
    shuffled across strata, because an order grouped by stratum would tell the
    reader the verdict the page hides. The same pool and seed draw the same
    sample in the same order, whatever order the pool arrives in.
    """
    if not pool:
        raise ValueError("no filter candidate in the pool")
    cells: dict[tuple[bool, bool], list[PoolRow]] = {key: [] for key in STRATA}
    for row in sorted(pool, key=lambda r: r.url):
        cells[stratum_of(row.news, row.state)].append(row)

    quota = dict.fromkeys(STRATA, 0)
    remaining = min(size, len(pool))
    while remaining:
        for key in STRATA:
            if remaining and quota[key] < len(cells[key]):
                quota[key] += 1
                remaining -= 1

    rejected = sum(quota[key] for key in STRATA if key[1])
    if rejected < min_rejected:
        raise ValueError(
            f"the pool yields {rejected} pipeline-rejected rows, and the sample needs "
            f"at least {min_rejected}: widen the pool or lower the minimum"
        )

    rng = random.Random(seed)
    drawn = [(row, len(cells[key])) for key in STRATA for row in rng.sample(cells[key], quota[key])]
    rng.shuffle(drawn)
    return [
        SampleRow(
            url=row.url,
            position=position,
            news=row.news,
            pipeline_state=row.state,
            stratum_size=stratum_size,
        )
        for position, (row, stratum_size) in enumerate(drawn)
    ]


@dataclass(frozen=True)
class FilterScore:
    labeled: int
    precision: float | None
    recall: float | None


def filter_score(rows: list[LabeledRow]) -> FilterScore:
    """The filter's keep against the labels, an upvote being the positive.

    Each stratum was drawn at its own rate, so each labeled row stands for its
    stratum's pool size over the rows of that stratum labeled here: the result
    estimates the pool, not the sample. None where the ratio has nothing below it.
    """
    labeled_in = Counter(stratum_of(r.news, r.pipeline_state) for r in rows)
    kept = kept_up = up = 0.0
    for r in rows:
        key = stratum_of(r.news, r.pipeline_state)
        weight = r.stratum_size / labeled_in[key]
        if not key[1]:
            kept += weight
            kept_up += weight * r.up
        up += weight * r.up
    return FilterScore(
        labeled=len(rows),
        precision=kept_up / kept if kept else None,
        recall=kept_up / up if up else None,
    )


def auc(scores: list[float], positives: list[bool]) -> float | None:
    """How often a positive outscores a negative, a tie counting half; None with one class."""
    pos = [s for s, p in zip(scores, positives, strict=True) if p]
    neg = [s for s, p in zip(scores, positives, strict=True) if not p]
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def bootstrap_auc(
    scores: list[float], positives: list[bool], *, resamples: int, seed: int
) -> tuple[float, float] | None:
    """The 2.5th and 97.5th percentiles of the AUC over item resamples, drawn with `seed`.

    A resample that draws one class alone has no AUC and is left out.
    """
    if auc(scores, positives) is None:
        return None
    rng = random.Random(seed)
    n = len(scores)
    estimates = []
    for _ in range(resamples):
        picks = [rng.randrange(n) for _ in range(n)]
        value = auc([scores[i] for i in picks], [positives[i] for i in picks])
        if value is not None:
            estimates.append(value)
    low, *_, high = statistics.quantiles(estimates, n=40, method="inclusive")
    return low, high


async def preference_scores(
    embedder: Embedder, items: dict[str, str], votes: list[Vote]
) -> dict[str, float]:
    """Each item's cosine to its nearest upvote minus its nearest downvote, by title.

    `items` maps URL to title; `votes` are the seeds, which the caller has already
    stripped of every sampled URL so no label scores itself. Titles only, as every
    measurement in ADR-0006 embedded. An item the embedder answers empty for is
    left out.
    """
    seed_vectors = await embedder.embed([v.title for v in votes])
    item_vectors = await embedder.embed(list(items.values()))
    up = {v.url: vec for v, vec in zip(votes, seed_vectors, strict=True) if vec and v.up}
    down = {v.url: vec for v, vec in zip(votes, seed_vectors, strict=True) if vec and not v.up}
    candidates = {url: vec for url, vec in zip(items, item_vectors, strict=True) if vec}
    verdicts = judge(
        candidates,
        list(up.values()),
        list(down.values()),
        upvoted_urls=list(up),
        downvoted_urls=list(down),
    )
    return {v.url: v.net for v in verdicts}
