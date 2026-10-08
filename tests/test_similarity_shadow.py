import pytest
from fakes import CountingD1, SqliteD1

from cyris.adapters.store.similarity_shadow import D1SimilarityShadow
from cyris.domain.similarity import SimilarityVerdict

pytestmark = pytest.mark.unit

RUN_AT = "2026-10-08T00:00:01+00:00"


def verdict(url: str, *, up_url: str | None = "u", down_url: str | None = "d") -> SimilarityVerdict:
    return SimilarityVerdict(
        url=url,
        down_similarity=0.25,
        up_similarity=0.75,
        suppressed=False,
        nearest_up_url=up_url,
        nearest_down_url=down_url,
    )


def test_one_row_per_verdict_with_the_nearest_of_each_side() -> None:
    db = SqliteD1()

    D1SimilarityShadow(db, "gemini-embedding-001").record(
        RUN_AT, "morning", [verdict("c1"), verdict("c2", up_url=None)]
    )

    assert db.query("SELECT * FROM vote_similarity_shadow ORDER BY candidate_url").rows == [
        {
            "run_at": RUN_AT,
            "period": "morning",
            "model": "gemini-embedding-001",
            "candidate_url": "c1",
            "up_url": "u",
            "up_cosine": 0.75,
            "down_url": "d",
            "down_cosine": 0.25,
        },
        {
            "run_at": RUN_AT,
            "period": "morning",
            "model": "gemini-embedding-001",
            "candidate_url": "c2",
            "up_url": None,
            "up_cosine": None,
            "down_url": "d",
            "down_cosine": 0.25,
        },
    ]


def test_a_pool_wider_than_one_statement_is_written_in_batches() -> None:
    """Eight columns a row under D1's 100 bound parameters: twelve rows a statement."""
    db = CountingD1()

    D1SimilarityShadow(db, "m").record(RUN_AT, "evening", [verdict(f"c{i}") for i in range(30)])

    assert db.query_count == 3
    assert db.query("SELECT COUNT(*) AS n FROM vote_similarity_shadow").rows == [{"n": 30}]


def test_no_verdicts_send_no_statement() -> None:
    db = CountingD1()

    D1SimilarityShadow(db, "m").record(RUN_AT, "morning", [])

    assert db.query_count == 0
