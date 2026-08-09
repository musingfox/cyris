"""Tests for the vote-similarity use case."""

from datetime import UTC, datetime

import pytest

from cyris.domain.models import ArticleState, StoredArticle, Tier
from cyris.domain.similarity import normalize
from cyris.service_layer.vote_similarity import judge_by_votes


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

    report = await judge_by_votes(store, FakeEmbedder(), candidates)

    assert report.suppressed_urls == ["c1"]
    assert report.downvote_seeds == 1


async def test_pipeline_verdicts_are_not_seeds():
    """48 of 50 lottery rows were pipeline-accepted; seeding on those inverts the filter."""
    store = FakeStore([article("d", "Lottery draw", ArticleState.REJECTED, triaged=False)])

    report = await judge_by_votes(store, FakeEmbedder(), [article("c1", "Lottery again")])

    assert not report.ran
    assert report.skipped_reason == "no human-voted articles yet"


async def test_an_upvoted_neighbour_is_not_suppressed():
    store = FakeStore(
        [
            article("d", "Lottery draw", ArticleState.REJECTED, triaged=True),
            article("u", "Lottery analysis", ArticleState.ACCEPTED, triaged=True),
        ]
    )

    report = await judge_by_votes(store, FakeEmbedder(), [article("c1", "Lottery again")])

    assert report.suppressed_urls == []
    assert report.upvote_seeds == 1


async def test_embedding_failure_lets_the_digest_through():
    class Broken:
        async def embed(self, texts):
            raise RuntimeError("429 forever")

    store = FakeStore([article("d", "Lottery", ArticleState.REJECTED, triaged=True)])

    report = await judge_by_votes(store, Broken(), [article("c1", "Lottery")])

    assert not report.ran
    assert report.suppressed_urls == []
    assert "embedding failed" in report.skipped_reason


async def test_an_already_voted_article_is_not_re_judged():
    """It would match its own seed at 1.0 and report a decision already made."""
    voted = article("d", "Lottery draw", ArticleState.REJECTED, triaged=True)
    store = FakeStore([voted])

    report = await judge_by_votes(store, FakeEmbedder(), [voted])

    assert not report.ran
    assert report.skipped_reason == "every candidate was already voted on"


async def test_no_candidates_short_circuits_before_any_api_call():
    embedder = FakeEmbedder()

    report = await judge_by_votes(FakeStore([]), embedder, [])

    assert not report.ran
    assert embedder.calls == 0


@pytest.mark.parametrize("threshold,expected", [(0.99, []), (0.4, ["c1"])])
async def test_threshold_moves_the_boundary(threshold, expected):
    """The partial match at cosine 0.5 falls on either side depending on the setting."""
    store = FakeStore([article("d", "Lottery", ArticleState.REJECTED, triaged=True)])
    candidates = [article("c1", "Partial match")]

    report = await judge_by_votes(store, FakeEmbedder(), candidates, threshold=threshold)

    assert report.suppressed_urls == expected
