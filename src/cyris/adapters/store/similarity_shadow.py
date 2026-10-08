"""Each run's vote-similarity pool in D1: per candidate, the nearest vote on each side."""

from cyris.adapters.store.d1 import D1Queryable, chunk_rows
from cyris.domain.similarity import SimilarityVerdict

_PARAMS = 8  # run_at, period, model, candidate_url, up_url, up_cosine, down_url, down_cosine


class D1SimilarityShadow:
    """Raw nearest-neighbour data, no verdict: the suppression decision is not stored.

    A side with no seed has no nearest URL, and its cosine is stored as NULL
    rather than the 0.0 the domain uses for "nothing to compare against".
    """

    def __init__(self, client: D1Queryable, model: str) -> None:
        self._db = client
        self._model = model

    def record(self, run_at: str, period: str, verdicts: list[SimilarityVerdict]) -> None:
        rows = [
            [
                run_at,
                period,
                self._model,
                v.url,
                v.nearest_up_url,
                None if v.nearest_up_url is None else v.up_similarity,
                v.nearest_down_url,
                None if v.nearest_down_url is None else v.down_similarity,
            ]
            for v in verdicts
        ]
        # OR REPLACE so a statement D1Client retries after a lost response
        # rewrites its own rows instead of failing on the primary key.
        for chunk in chunk_rows(rows, _PARAMS):
            self._db.query(
                "INSERT OR REPLACE INTO vote_similarity_shadow (run_at, period, model, "
                "candidate_url, up_url, up_cosine, down_url, down_cosine) VALUES "
                + ", ".join("(?, ?, ?, ?, ?, ?, ?, ?)" for _ in chunk),
                [value for row in chunk for value in row],
            )
