"""A local install's `feed_health` row follows the rss Worker's rules.

`workers/rss/test/feed_health_cases.json` is the one list both sides read: node runs it against
`recordHealth` in `workers/rss/src/buffer.js` (`feed-health-cases.test.js`), and this runs it
against `D1FeedHealthWriter`, over the real schema. Each case is a sequence of polls and the
rows they leave.
"""

import json
from datetime import datetime
from pathlib import Path

import pytest
from fakes import SqliteD1

from cyris.adapters.store.feed_health import D1FeedHealthWriter

pytestmark = pytest.mark.unit

CASES = json.loads(
    (Path(__file__).parents[1] / "workers/rss/test/feed_health_cases.json").read_text("utf-8")
)["cases"]


def stored_rows(db: SqliteD1) -> dict[str, dict]:
    rows = db.query("SELECT * FROM feed_health ORDER BY name").rows
    return {row.pop("name"): row for row in rows}


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_a_sequence_of_polls_leaves_the_rows_the_worker_leaves(case: dict) -> None:
    db = SqliteD1()
    writer = D1FeedHealthWriter(db)

    for poll in case["polls"]:
        outcomes = [(o["name"], o["error"]) for o in poll["outcomes"]]
        writer.record(outcomes, datetime.fromisoformat(poll["at"]))

    assert stored_rows(db) == case["rows"]
