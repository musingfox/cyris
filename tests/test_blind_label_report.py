"""Scoring the filter and the embedding preference against the blind labels."""

from types import SimpleNamespace

import pytest

from cyris.adapters.store.blind_labels import LabeledRow, Vote
from cyris.diagnostics.blind_labels import (
    auc,
    bootstrap_auc,
    filter_score,
    preference_scores,
)
from cyris.domain.models import ArticleState
from cyris.domain.similarity import normalize

pytestmark = pytest.mark.unit

KEPT, REJECTED = ArticleState.ACCEPTED, ArticleState.REJECTED


def labeled(n_up: int, n_down: int, state: ArticleState, *, size: int, news=False, tag=""):
    return [
        LabeledRow(
            url=f"{tag}{state}-{i}",
            title="",
            news=news,
            pipeline_state=state,
            stratum_size=size,
            up=i < n_up,
        )
        for i in range(n_up + n_down)
    ]


class TestFilterScore:
    def test_a_sample_drawn_whole_is_plain_precision_and_recall(self) -> None:
        rows = labeled(3, 1, KEPT, size=4) + labeled(1, 3, REJECTED, size=4)

        score = filter_score(rows)

        assert (score.labeled, score.precision, score.recall) == (8, 0.75, 0.75)

    def test_each_stratum_counts_by_its_share_of_the_pool(self) -> None:
        # Each labeled rejected row stands for ten in the pool, each kept row for one.
        rows = labeled(3, 1, KEPT, size=4) + labeled(1, 3, REJECTED, size=40)

        score = filter_score(rows)

        assert score.precision == 0.75
        assert score.recall == pytest.approx(3 / 13)

    def test_strata_are_weighted_apart_by_news_as_well(self) -> None:
        rows = labeled(1, 1, KEPT, size=2, news=True, tag="n") + labeled(
            1, 1, KEPT, size=20, news=False, tag="o"
        )

        # Kept and up: 1 x 1 news, 1 x 10 non-news, out of 2 + 20 kept.
        assert filter_score(rows).precision == pytest.approx(11 / 22)

    def test_a_pending_row_is_kept_like_an_accepted_one(self) -> None:
        rows = labeled(1, 0, ArticleState.PENDING, size=1) + labeled(1, 0, REJECTED, size=1)

        assert filter_score(rows).recall == 0.5

    def test_without_a_kept_row_or_an_upvote_the_ratio_is_undefined(self) -> None:
        assert filter_score(labeled(0, 2, REJECTED, size=2)).precision is None
        assert filter_score(labeled(0, 2, KEPT, size=2)).recall is None
        assert filter_score([]).labeled == 0


class TestAuc:
    def test_a_perfect_ranking_is_one_and_its_reverse_zero(self) -> None:
        assert auc([0.9, 0.8, 0.1], [True, True, False]) == 1.0
        assert auc([0.1, 0.2, 0.9], [True, True, False]) == 0.0

    def test_a_tie_between_classes_counts_half(self) -> None:
        assert auc([0.5, 0.5], [True, False]) == 0.5

    def test_one_class_alone_has_no_auc(self) -> None:
        assert auc([0.1, 0.2], [True, True]) is None

    def test_the_bootstrap_is_seeded_and_brackets_the_estimate(self) -> None:
        scores = [i / 20 for i in range(20)]
        positives = [i % 3 != 0 for i in range(20)]

        low, high = bootstrap_auc(scores, positives, resamples=500, seed=4)

        assert bootstrap_auc(scores, positives, resamples=500, seed=4) == (low, high)
        assert low <= auc(scores, positives) <= high
        assert low < high

    def test_one_class_alone_has_no_interval(self) -> None:
        assert bootstrap_auc([0.1, 0.2], [True, True], resamples=10, seed=1) is None


VECTORS = {
    "liked": [1.0, 0.0],
    "disliked": [0.0, 1.0],
    "near liked": [0.9, 0.1],
    "near disliked": [0.2, 0.9],
}


class FakeEmbedder:
    def __init__(self) -> None:
        self.payloads: list[list[str]] = []
        self.usage = SimpleNamespace(as_dict=dict)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.payloads.append(list(texts))
        return [normalize(VECTORS[t]) for t in texts]


async def test_preference_is_the_nearest_upvote_minus_the_nearest_downvote() -> None:
    embedder = FakeEmbedder()
    items = {"a": "near liked", "b": "near disliked"}
    votes = [Vote("u", "liked", up=True), Vote("d", "disliked", up=False)]

    scores = await preference_scores(embedder, items, votes)

    assert scores["a"] > 0 > scores["b"]
    up, down = normalize([1.0, 0.0]), normalize([0.0, 1.0])
    a = normalize(VECTORS["near liked"])
    assert scores["a"] == pytest.approx(
        sum(x * y for x, y in zip(a, up, strict=True))
        - sum(x * y for x, y in zip(a, down, strict=True))
    )
    assert embedder.payloads == [["liked", "disliked"], ["near liked", "near disliked"]]
