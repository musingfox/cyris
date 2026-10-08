"""Tests for vote-seeded similarity judging."""

import math

import pytest

from cyris.domain.similarity import (
    DEFAULT_THRESHOLD,
    cosine,
    judge,
    max_similarity,
    nearest,
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


def test_a_close_downvote_match_is_suppressed():
    lottery = unit(1.0, 0.0, 0.0)
    [verdict] = judge({"u": lottery}, upvoted=[], downvoted=[lottery])

    assert verdict.suppressed
    assert math.isclose(verdict.down_similarity, 1.0, abs_tol=1e-9)


def test_a_distant_article_survives():
    [verdict] = judge({"u": unit(0.0, 1.0)}, upvoted=[], downvoted=[unit(1.0, 0.0)])

    assert not verdict.suppressed


def test_a_stronger_upvote_match_rescues_an_article():
    """The upvote is the more specific signal; it must not lose to a broader reject."""
    candidate = unit(1.0, 0.05, 0.0)
    [verdict] = judge({"u": candidate}, upvoted=[candidate], downvoted=[unit(1.0, 0.0, 0.0)])

    assert not verdict.suppressed


def test_threshold_is_the_boundary_measured_on_real_data():
    """0.737 in-class low vs 0.666 out-of-class high — the default sits between."""
    assert 0.666 < DEFAULT_THRESHOLD < 0.737


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

    [plain] = judge({"c": candidate}, up, down, 0.5)
    [named] = judge(
        {"c": candidate},
        up,
        down,
        0.5,
        upvoted_urls=["up-far", "up-near"],
        downvoted_urls=["down-near", "down-far"],
    )

    assert (named.nearest_up_url, named.nearest_down_url) == ("up-near", "down-near")
    assert (named.up_similarity, named.down_similarity, named.suppressed) == (
        plain.up_similarity,
        plain.down_similarity,
        plain.suppressed,
    )
