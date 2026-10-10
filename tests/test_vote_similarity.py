"""Tests for the vote-similarity use case."""

from datetime import UTC, datetime

import pytest

from cyris.domain.models import ArticleState, StoredArticle, Tier
from cyris.domain.similarity import normalize
from cyris.service_layer.vote_similarity import VoteSimilarityReport, judge_by_votes

pytestmark = pytest.mark.unit


def article(url: str, title: str, state=ArticleState.PENDING, triaged=False) -> StoredArticle:
    return StoredArticle(
        url=url,
        original_id=url,
        title=title,
        content="c",
        source_name="Src",
        source_tier=Tier.FILTER,
        published_at=datetime(2026, 8, 9, tzinfo=UTC),
        first_seen_at=datetime(2026, 8, 9, tzinfo=UTC),
        state=state,
        triaged_at=datetime(2026, 8, 9, tzinfo=UTC) if triaged else None,
    )


class FakeStore:
    def __init__(self, rows: list[StoredArticle]) -> None:
        self._rows = rows

    def list_articles(self, state=None, limit=100, **_kw) -> list[StoredArticle]:
        return [a for a in self._rows if state is None or a.state == state][:limit]


class FakeEmbedder:
    """Maps a title to a vector by its first character, so tests can control distance."""

    # P shares half its length with L, so a threshold sweep crosses it.
    AXES = {
        "L": [1.0, 0.0, 0.0],
        "T": [0.0, 1.0, 0.0],
        "X": [0.0, 0.0, 1.0],
        "P": [0.5, 0.0, 0.866],
    }

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [normalize(self.AXES.get(t[0], [1.0, 1.0, 1.0])) for t in texts]


async def test_a_candidate_like_a_downvote_is_suppressed():
    store = FakeStore([article("d", "Lottery draw", ArticleState.REJECTED, triaged=True)])
    candidates = [article("c1", "Lottery again"), article("c2", "Tech thing")]

    report = await judge_by_votes(store, FakeEmbedder(), candidates, max_seeds=200)

    assert report.suppressed_urls == ["c1"]
    assert report.downvote_seeds == 1


async def test_pipeline_verdicts_are_not_seeds():
    """48 of 50 lottery rows were pipeline-accepted; seeding on those inverts the filter."""
    store = FakeStore([article("d", "Lottery draw", ArticleState.REJECTED, triaged=False)])

    report = await judge_by_votes(
        store, FakeEmbedder(), [article("c1", "Lottery again")], max_seeds=200
    )

    assert not report.ran
    assert report.skipped_reason == "no human-voted articles yet"


async def test_an_upvoted_neighbour_is_not_suppressed():
    store = FakeStore(
        [
            article("d", "Lottery draw", ArticleState.REJECTED, triaged=True),
            article("u", "Lottery analysis", ArticleState.ACCEPTED, triaged=True),
        ]
    )

    report = await judge_by_votes(
        store, FakeEmbedder(), [article("c1", "Lottery again")], max_seeds=200
    )

    assert report.suppressed_urls == []
    assert report.upvote_seeds == 1


async def test_embedding_failure_lets_the_digest_through():
    class Broken:
        async def embed(self, texts):
            raise RuntimeError("429 forever")

    store = FakeStore([article("d", "Lottery", ArticleState.REJECTED, triaged=True)])

    report = await judge_by_votes(store, Broken(), [article("c1", "Lottery")], max_seeds=200)

    assert not report.ran
    assert report.suppressed_urls == []
    assert "embedding failed" in report.skipped_reason


async def test_a_short_embedding_answer_lets_the_digest_through():
    """An embedder that answers fewer vectors than texts cannot be paired back to URLs."""

    class Short:
        async def embed(self, texts):
            return [normalize([1.0, 0.0])] * (len(texts) - 1)

    store = FakeStore([article("d", "Lottery", ArticleState.REJECTED, triaged=True)])

    report = await judge_by_votes(store, Short(), [article("c1", "Lottery")], max_seeds=200)

    assert not report.ran
    assert "embedding failed" in report.skipped_reason


async def test_an_already_voted_article_is_not_re_judged():
    """It would match its own seed at 1.0 and report a decision already made."""
    voted = article("d", "Lottery draw", ArticleState.REJECTED, triaged=True)
    store = FakeStore([voted])

    report = await judge_by_votes(store, FakeEmbedder(), [voted], max_seeds=200)

    assert not report.ran
    assert report.skipped_reason == "every candidate was already voted on"


async def test_no_candidates_short_circuits_before_any_api_call():
    embedder = FakeEmbedder()

    report = await judge_by_votes(FakeStore([]), embedder, [], max_seeds=200)

    assert not report.ran
    assert embedder.calls == 0


@pytest.mark.parametrize("threshold,expected", [(0.99, []), (0.4, ["c1"])])
async def test_threshold_moves_the_boundary(threshold, expected):
    """The partial match at cosine 0.5 falls on either side depending on the setting."""
    store = FakeStore([article("d", "Lottery", ArticleState.REJECTED, triaged=True)])
    candidates = [article("c1", "Partial match")]

    report = await judge_by_votes(
        store, FakeEmbedder(), candidates, threshold=threshold, max_seeds=200
    )

    assert report.suppressed_urls == expected


async def test_no_calibrated_threshold_skips_before_embedding():
    """Another model's cutoff would judge this one's cosines on the wrong scale."""
    store = FakeStore([article("d", "Lottery draw", ArticleState.REJECTED, triaged=True)])
    embedder = FakeEmbedder()

    report = await judge_by_votes(
        store, embedder, [article("c1", "Lottery again")], threshold=None, max_seeds=200
    )

    assert not report.ran
    assert "threshold" in report.skipped_reason
    assert embedder.calls == 0


class SeedGapEmbedder(FakeEmbedder):
    """Embeds a title starting with E to nothing, as a provider does for a text it cannot read."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = await super().embed(texts)
        return [[] if t.startswith("E") else v for t, v in zip(texts, vectors, strict=True)]


async def test_each_verdict_names_the_seed_its_cosine_came_from_past_an_empty_vector():
    store = FakeStore(
        [
            article("u-tech", "Tech upvoted", ArticleState.ACCEPTED, triaged=True),
            article("d-empty", "Empty title", ArticleState.REJECTED, triaged=True),
            article("d-lottery", "Lottery draw", ArticleState.REJECTED, triaged=True),
        ]
    )

    report = await judge_by_votes(
        store, SeedGapEmbedder(), [article("c1", "Lottery again")], max_seeds=200
    )

    verdict = report.verdicts["c1"]
    assert (verdict.nearest_up_url, verdict.nearest_down_url) == ("u-tech", "d-lottery")
    assert report.downvote_seeds == 1


async def test_the_report_keeps_each_side_s_seed_vectors_and_titles():
    store = FakeStore(
        [
            article("u", "Liked title", ArticleState.ACCEPTED, triaged=True),
            article("d", "Disliked title", ArticleState.REJECTED, triaged=True),
        ]
    )
    embedder = FakeEmbedder()

    report = await judge_by_votes(store, embedder, [article("c", "Tech thing")], max_seeds=200)

    assert list(report.up_seed_vectors) == ["u"]
    assert list(report.down_seed_vectors) == ["d"]
    assert report.seed_titles == {"u": "Liked title", "d": "Disliked title"}
    assert embedder.calls == 2


async def test_a_pass_that_did_not_run_keeps_no_seeds():
    report = await judge_by_votes(
        FakeStore([]), FakeEmbedder(), [article("c", "Tech")], max_seeds=5
    )

    assert report.skipped_reason == "no human-voted articles yet"
    assert report.up_seed_vectors == {}
    assert report.seed_titles == {}


async def test_a_seed_with_an_empty_vector_is_kept_nowhere():
    store = FakeStore([article("u-empty", "Empty title", ArticleState.ACCEPTED, triaged=True)])

    report = await judge_by_votes(store, SeedGapEmbedder(), [article("c", "Tech")], max_seeds=5)

    assert "u-empty" not in report.up_seed_vectors
    assert "u-empty" not in report.seed_titles


def test_nearest_titles_names_the_closest_votes_each_way_closest_first():
    report = VoteSimilarityReport(
        candidate_vectors={"c": [1.0, 0.0]},
        up_seed_vectors={"u1": [0.0, 1.0], "u2": [1.0, 0.1], "u3": [1.0, 1.0]},
        down_seed_vectors={"d1": [1.0, 0.5], "d2": [0.0, 1.0]},
        seed_titles={"u1": "U1", "u2": "U2", "u3": "U3", "d1": "D1", "d2": "D2"},
    )

    assert report.nearest_titles("c", 2) == (["U2", "U3"], ["D1", "D2"])
