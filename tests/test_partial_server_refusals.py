"""A partial server's refusals of the SQL-store features name both SQL backends.

Blind labels, the interest map and the source, topic and area tables live in the
D1 tables, which a local install keeps in a SQLite file. A refusal that names only
`"d1"` sends a local reader to Cloudflare, which a local install exists to avoid.
"""

import json

import pytest
from aiohttp.test_utils import make_mocked_request

from cyris.entrypoints.triage_server import TriageServer

pytestmark = pytest.mark.unit


def _names_both_sql_backends(text: str) -> None:
    assert '"sqlite"' in text, text
    assert '"d1"' in text, text


async def test_the_labels_api_names_sqlite() -> None:
    response = await TriageServer()._handle_get_labels(make_mocked_request("GET", "/api/labels"))

    assert response.status == 409
    _names_both_sql_backends(json.loads(response.text)["error"])


async def test_the_interest_map_names_sqlite() -> None:
    response = await TriageServer()._handle_interest_page(make_mocked_request("GET", "/interest"))

    assert response.status == 409
    _names_both_sql_backends(response.text)


async def test_the_source_table_refusal_names_sqlite() -> None:
    request = make_mocked_request("DELETE", "/api/sources/x", match_info={"name": "x"})
    response = await TriageServer()._handle_delete_source(request)

    assert response.status == 409
    _names_both_sql_backends(json.loads(response.text)["error"])
