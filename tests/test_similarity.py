"""Tests for vote-seeded similarity judging."""

import inspect
import math

import pytest

import cyris.domain.similarity as similarity
from cyris.domain.similarity import (
    MIN_OUTLIER_WINDOW,
    OUTLIER_MARGIN,
    cosine,
    judge,
    max_similarity,
    nearest,
    nearest_k,
    normalize,
)

pytestmark = pytest.mark.unit


def unit(*parts: float) -> list[float]:
    return normalize(list(parts))


def test_normalize_makes_a_unit_vector():
    v = normalize([3.0, 4.0])
    assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0)


def test_normalize_leaves_a_zero_vector_alone():
    """An all-zero embedding would otherwise divide by zero."""
    assert normalize([0.0, 0.0]) == [0.0, 0.0]


def test_cosine_of_identical_vectors_is_one():
    v = unit(1.0, 2.0, 3.0)
    assert math.isclose(cosine(v, v), 1.0, abs_tol=1e-9)


def test_max_similarity_takes_the_nearest_seed_not_the_average():
    """Averaging two distant seeds lands between them, matching neither."""
    candidate = unit(1.0, 0.0)
    seeds = [unit(1.0, 0.0), unit(0.0, 1.0)]

    assert math.isclose(max_similarity(candidate, seeds), 1.0, abs_tol=1e-9)


def test_max_similarity_without_seeds_is_zero():
    assert max_similarity(unit(1.0, 0.0), []) == 0.0


def _window(cosines: list[float]) -> dict[str, list[float]]:
    """Candidates whose cosine to the seed [1, 0] is each value, and nothing else."""
    return {f"u{i}": [c, math.sqrt(1 - c * c)] for i, c in enumerate(cosines)}


_DOWN_BASE = [0.40, 0.41, 0.42, 0.43, 0.44, 0.45, 0.46, 0.47, 0.48]


def test_the_outlier_of_a_window_is_suppressed():
    verdicts = judge(_window([*_DOWN_BASE, 0.58]), [], [[1.0, 0.0]])

    assert [v.url for v in verdicts if v.suppressed] == ["u9"]
    [outlier] = [v for v in verdicts if v.url == "u9"]
    assert math.isclose(outlier.outlier_score, 0.6745 * (0.58 - 0.445) / 0.025, rel_tol=1e-6)


def test_a_value_just_under_the_margin_is_not_suppressed():
    verdicts = judge(_window([*_DOWN_BASE, 0.574]), [], [[1.0, 0.0]])

    assert not any(v.suppressed for v in verdicts)
    assert OUTLIER_MARGIN == 3.5


def test_the_same_outlier_is_suppressed_whatever_the_window_level():
    raised = [c + 0.15 for c in [*_DOWN_BASE, 0.58]]

    base = judge(_window([*_DOWN_BASE, 0.58]), [], [[1.0, 0.0]])
    shifted = judge(_window(raised), [], [[1.0, 0.0]])

    assert [v.url for v in base if v.suppressed] == ["u9"]
    assert [v.url for v in shifted if v.suppressed] == ["u9"]


def test_a_window_of_equal_similarities_suppresses_nothing():
    verdicts = judge(_window([0.9] * 20), [], [[1.0, 0.0]])

    assert not any(v.suppressed for v in verdicts)
    assert all(v.outlier_score is None for v in verdicts)


def test_an_outlier_with_an_upvote_at_least_as_close_is_kept():
    verdicts = judge(
        {**_window([*_DOWN_BASE, 0.58]), "up": [0.60, math.sqrt(1 - 0.36)]},
        upvoted=[[0.60, math.sqrt(1 - 0.36)]],
        downvoted=[[1.0, 0.0]],
    )

    assert not next(v for v in verdicts if v.url == "u9").suppressed


def test_a_window_below_the_minimum_suppresses_nothing():
    verdicts = judge(_window([*_DOWN_BASE[:8], 0.70]), [], [[1.0, 0.0]])

    assert len(verdicts) == MIN_OUTLIER_WINDOW - 1
    assert not any(v.suppressed for v in verdicts)
    assert all(v.outlier_score is None for v in verdicts)


def test_the_same_shape_at_the_minimum_window_suppresses_the_outlier():
    verdicts = judge(_window([*_DOWN_BASE, 0.70]), [], [[1.0, 0.0]])

    assert len(verdicts) == MIN_OUTLIER_WINDOW == 10
    assert [v.url for v in verdicts if v.suppressed] == ["u9"]


def test_no_downvote_seed_names_no_neighbour_and_suppresses_nothing():
    verdicts = judge(_window([0.5, 0.6]), [], [])

    assert not any(v.suppressed for v in verdicts)
    assert all(v.nearest_down_url is None for v in verdicts)


def test_judge_takes_no_cutoff_parameter():
    assert "threshold" not in inspect.signature(judge).parameters
    assert not hasattr(similarity, "DEFAULT_THRESHOLD")


def test_a_distant_article_survives():
    [verdict] = judge({"u": unit(0.0, 1.0)}, upvoted=[], downvoted=[unit(1.0, 0.0)])

    assert not verdict.suppressed


def test_a_stronger_upvote_match_rescues_an_article():
    """The upvote is the more specific signal; it must not lose to a broader reject."""
    candidate = unit(1.0, 0.05, 0.0)
    [verdict] = judge({"u": candidate}, upvoted=[candidate], downvoted=[unit(1.0, 0.0, 0.0)])

    assert not verdict.suppressed


def test_verdicts_are_ordered_by_net_preference():
    liked, disliked = unit(1.0, 0.0), unit(0.0, 1.0)
    verdicts = judge({"bad": disliked, "good": liked}, upvoted=[liked], downvoted=[disliked])

    assert [v.url for v in verdicts] == ["good", "bad"]


def test_nearest_names_the_closest_seed_and_its_cosine():
    candidate = unit(1.0, 0.2)
    seeds = {"far": unit(0.0, 1.0), "near": unit(1.0, 0.0)}

    url, similarity = nearest(candidate, seeds)

    assert url == "near"
    assert similarity == max_similarity(candidate, list(seeds.values()))


def test_nearest_without_seeds_names_nothing():
    assert nearest(unit(1.0, 0.0), {}) == (None, 0.0)


def test_a_verdict_built_without_seed_urls_names_none():
    [verdict] = judge({"u": unit(1.0, 0.0)}, upvoted=[unit(1.0, 0.0)], downvoted=[])

    assert (verdict.nearest_up_url, verdict.nearest_down_url) == (None, None)


def test_seed_urls_name_each_side_s_nearest_without_changing_the_verdict():
    candidate = unit(1.0, 0.3, 0.1)
    up = [unit(0.0, 0.0, 1.0), unit(1.0, 0.2, 0.0)]
    down = [unit(1.0, 0.3, 0.0), unit(0.0, 1.0, 0.0)]

    [plain] = judge({"c": candidate}, up, down)
    [named] = judge(
        {"c": candidate},
        up,
        down,
        upvoted_urls=["up-far", "up-near"],
        downvoted_urls=["down-near", "down-far"],
    )

    assert (named.nearest_up_url, named.nearest_down_url) == ("up-near", "down-near")
    assert (named.up_similarity, named.down_similarity, named.suppressed) == (
        plain.up_similarity,
        plain.down_similarity,
        plain.suppressed,
    )


_K_SEEDS = {"a": [1.0, 0.0], "b": [0.0, 1.0], "c": [0.8, 0.6]}


def test_nearest_k_names_the_k_closest_seeds_closest_first():
    assert nearest_k([1.0, 0.0], _K_SEEDS, 2) == ["a", "c"]


def test_nearest_k_returns_every_seed_when_fewer_than_k():
    assert nearest_k([1.0, 0.0], _K_SEEDS, 5) == ["a", "c", "b"]


def test_nearest_k_without_seeds_is_empty():
    assert nearest_k([1.0, 0.0], {}, 5) == []


def test_nearest_k_ties_keep_the_seeds_insertion_order():
    seeds = {"x": [0.6, 0.8], "y": [0.6, 0.8]}
    assert nearest_k([1.0, 0.0], seeds, 1) == ["x"]


def test_nearest_k_rejects_a_seed_of_another_length():
    with pytest.raises(ValueError):
        nearest_k([1.0, 0.0], {"a": [1.0]}, 1)
