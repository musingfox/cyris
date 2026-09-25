"""One registry of runtime settings: the required set, the page's fields and its routes."""

import re
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

import cyris.entrypoints
from cyris.config import GRADE_D_KEYS, SETTINGS_FIELDS
from cyris.entrypoints.triage_server import TriageServer, render_settings_page

pytestmark = pytest.mark.unit

SETTINGS_JS = Path(cyris.entrypoints.__file__).parent / "static" / "settings.js"


@pytest.mark.parametrize("key", GRADE_D_KEYS)
def test_every_field_control_is_on_the_page(key: str) -> None:
    page = render_settings_page()
    for control in SETTINGS_FIELDS[key]["controls"]:
        assert f'id="{control}"' in page, f"{key}: no #{control}"


async def test_the_page_reads_its_fields_from_the_api() -> None:
    client = TestClient(TestServer(TriageServer()._app))
    await client.start_server()
    data = await (await client.get("/api/settings")).json()
    await client.close()

    assert data["fields"] == SETTINGS_FIELDS


def test_the_script_types_no_key_list_of_its_own() -> None:
    # A key named in a list literal is a second registry the page could drift from.
    # An index such as values["digest.max_featured"] reads a value; it is no list.
    listed = re.findall(r'(?<![\w\])])\[\s*"[a-z_]+\.[a-z_]+"', SETTINGS_JS.read_text())
    mapped = re.findall(r'^\s*"[a-z_-]+": "[a-z_]+\.[a-z_]+",$', SETTINGS_JS.read_text(), re.M)
    keyed = re.findall(r'^\s*"[a-z_]+\.[a-z_]+": \{', SETTINGS_JS.read_text(), re.M)
    assert (listed, mapped, keyed) == ([], [], [])
