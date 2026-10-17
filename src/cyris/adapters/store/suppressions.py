"""Each run's suppressions in D1: the downvote a candidate sat near, and until when it holds."""

from cyris.adapters.store.d1 import D1Queryable, chunk_rows
from cyris.domain.suppression import REASON, Suppression

_PARAMS = 10  # run_at, period, model, candidate_url, reason, down_url, down, up, score, until

# The one definition of a live hold, shared by the run and the interest map: its clock
# has not run out and no human has decided its article since. LEFT JOIN, so a hold whose
# article row is gone still counts until its clock does. LIVE_HOLD_WHERE binds `now`.
LIVE_HOLD_FROM = "suppressions s LEFT JOIN stored_articles a ON a.url = s.candidate_url"
LIVE_HOLD_WHERE = "s.until > ? AND a.triaged_at IS NULL"


class D1Suppressions:
    """Writes the suppression rows. A suppressed article stays pending in `stored_articles`."""

    def __init__(self, client: D1Queryable, model: str) -> None:
        self._db = client
        self._model = model

    def record(self, run_at: str, period: str, rows: list[Suppression]) -> None:
        values = [
            [
                run_at,
                period,
                self._model,
                s.candidate_url,
                REASON,
                s.nearest_down_url,
                s.down_cosine,
                s.up_cosine,
                s.outlier_score,
                s.until,
            ]
            for s in rows
        ]
        # OR REPLACE so a statement D1Client retries after a lost response
        # rewrites its own rows instead of failing on the primary key.
        for chunk in chunk_rows(values, _PARAMS):
            self._db.query(
                "INSERT OR REPLACE INTO suppressions (run_at, period, model, candidate_url, "
                "reason, nearest_down_url, down_cosine, up_cosine, outlier_score, until) VALUES "
                + ", ".join("(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)" for _ in chunk),
                [value for row in chunk for value in row],
            )

    def active(self, now: str) -> dict[str, Suppression]:
        """Each candidate with a live hold at `now`, with its latest hold."""
        rows = self._db.query(
            "SELECT s.candidate_url AS candidate_url, s.nearest_down_url AS nearest_down_url, "
            "s.down_cosine AS down_cosine, s.up_cosine AS up_cosine, "
            "s.outlier_score AS outlier_score, s.until AS until "
            f"FROM {LIVE_HOLD_FROM} WHERE {LIVE_HOLD_WHERE} ORDER BY s.until",
            [now],
        ).rows
        latest: dict[str, Suppression] = {}
        for row in rows:
            latest[row["candidate_url"]] = Suppression(**row)
        return latest
