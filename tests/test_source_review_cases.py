"""A local `cyris triage-ui` answers a page's source decision as the two Workers do.

The cases are `workers/app/test/source_review_cases.json`, which
`workers/app/test/source_review_contract.test.js` holds the app Worker and the hosted Worker to.
The server is built as `cyris triage-ui` builds it, over a real SQLite file, and each request
goes through the application's own dispatch, as `tests/test_local_votes.py` sends its votes.
ADR-0050.
"""

import json
from pathlib import Path

import pytest
from test_local_votes import _database, _send, _server

from cyris.adapters.store.source_store import D1SourceStore

pytestmark = pytest.mark.integration

CONTRACT = json.loads(
    (Path(__file__).parents[1] / "workers/app/test/source_review_cases.json").read_text()
)


def _door(tmp_path: Path):
    client = _database(tmp_path / "cyris.sqlite3")
    for row in CONTRACT["rows"]:
        client.query(
            "INSERT INTO sources (name, url, type, config) VALUES (?, ?, ?, ?)",
            [row["name"], row["url"], row["type"], json.dumps(row["config"])],
        )
    return _server(client, source_store=D1SourceStore(client))


async def _reviews(server) -> dict:
    return json.loads((await _send(server, "GET")).text)["reviews"]


async def test_the_probe_lists_each_sources_review(tmp_path: Path) -> None:
    assert await _reviews(_door(tmp_path)) == CONTRACT["probe"]["reviews"]


@pytest.mark.parametrize("case", CONTRACT["cases"], ids=lambda c: c["case"])
async def test_each_decision_is_answered_and_written_as_the_workers_do(
    tmp_path: Path, case: dict
) -> None:
    server = _door(tmp_path)

    response = await _send(server, "POST", case["body"])

    assert (response.status, json.loads(response.text)) == (case["status"], case["answer"])
    assert await _reviews(server) == case["reviews"]
