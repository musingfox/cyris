"""Blind labels on the filter's candidates: draw the sample, then score the filter
and the embedding preference against what the reader answered.

Off the pipeline, like the rest of `diagnostics/`: nothing here changes a digest.
The sample is stratified by news versus non-news and by the pipeline's verdict,
kept versus rejected, so the rejected rows a reader never sees are labeled too.
"""

from __future__ import annotations

import random

from cyris.adapters.store.blind_labels import PoolRow, SampleRow
from cyris.domain.models import ArticleState

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
