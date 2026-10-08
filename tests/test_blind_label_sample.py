"""Drawing the blind-label sample: stratified, blind in its order, and repeatable."""

from collections import Counter

import pytest

from cyris.adapters.store.blind_labels import PoolRow
from cyris.diagnostics.blind_labels import draw_sample
from cyris.domain.models import ArticleState

pytestmark = pytest.mark.unit

KEPT, REJECTED = ArticleState.ACCEPTED, ArticleState.REJECTED


def cell(prefix: str, n: int, state: ArticleState, news: bool) -> list[PoolRow]:
    return [PoolRow(url=f"{prefix}{i:03}", state=state, news=news) for i in range(n)]


def pool(news_kept=100, news_rejected=100, other_kept=100, other_rejected=100) -> list[PoolRow]:
    return [
        *cell("nk", news_kept, KEPT, True),
        *cell("nr", news_rejected, REJECTED, True),
        *cell("ok", other_kept, KEPT, False),
        *cell("or", other_rejected, REJECTED, False),
    ]


def strata(rows) -> Counter:
    return Counter((r.news, r.pipeline_state == REJECTED) for r in rows)


def test_each_stratum_gets_an_equal_share() -> None:
    sample = draw_sample(pool(), size=100, min_rejected=30, seed=1)

    assert strata(sample) == dict.fromkeys(
        [(True, False), (True, True), (False, False), (False, True)], 25
    )


def test_a_short_stratum_gives_its_shortfall_to_the_others() -> None:
    sample = draw_sample(pool(news_rejected=5), size=100, min_rejected=30, seed=1)

    counts = strata(sample)
    assert counts[(True, True)] == 5
    assert sum(counts.values()) == 100
    assert sorted(counts[k] for k in counts if k != (True, True)) == [31, 32, 32]


def test_the_same_seed_draws_the_same_sample_in_the_same_order() -> None:
    first = draw_sample(pool(), size=100, min_rejected=30, seed=7)
    again = draw_sample(list(reversed(pool())), size=100, min_rejected=30, seed=7)
    other = draw_sample(pool(), size=100, min_rejected=30, seed=8)

    assert first == again
    assert [r.url for r in first] != [r.url for r in other]


def test_the_order_interleaves_the_strata_so_it_hides_the_verdict() -> None:
    sample = draw_sample(pool(), size=100, min_rejected=30, seed=1)

    assert [r.position for r in sample] == list(range(100))
    verdicts = [r.pipeline_state == REJECTED for r in sample]
    assert verdicts != sorted(verdicts) and verdicts != sorted(verdicts, reverse=True)
    assert True in verdicts[:10] and False in verdicts[:10]


def test_each_row_carries_its_verdict_and_its_strata_pool_size() -> None:
    rows = [*cell("nk", 3, ArticleState.PENDING, True), *cell("or", 40, REJECTED, False)]

    sample = draw_sample(rows, size=10, min_rejected=1, seed=1)

    by_url = {r.url: r for r in sample}
    assert {r.pipeline_state for r in sample if r.news} == {ArticleState.PENDING}
    assert {r.stratum_size for r in sample if r.news} == {3}
    assert {r.stratum_size for r in sample if not r.news} == {40}
    assert len(by_url) == 10


def test_a_pool_smaller_than_the_target_is_taken_whole() -> None:
    sample = draw_sample(pool(2, 3, 4, 5), size=100, min_rejected=8, seed=1)

    assert len(sample) == 14


def test_too_few_rejected_rows_for_the_minimum_is_refused_with_the_counts() -> None:
    with pytest.raises(ValueError, match=r"20 pipeline-rejected.*at least 30"):
        draw_sample(pool(news_rejected=10, other_rejected=10), size=100, min_rejected=30, seed=1)


def test_an_empty_pool_is_refused() -> None:
    with pytest.raises(ValueError, match="no filter candidate"):
        draw_sample([], size=100, min_rejected=0, seed=1)
