"""The shared settings page built into the hosted Worker is the one TriageServer serves (ADR-0043).

`scripts/build_shared_settings.py` renders `settings.html.j2` per catalog, at build time, with the
`/static/` files the page loads and the two parts of `GET /api/settings` only the Python side
answers. Each is held here to what a bare TriageServer serves for the same path, so the hosted
page is the local page, byte for byte. The built file is committed, and held here to what the
script builds now. What the Worker does with the file is
`workers/app/test/hosted_shared_settings.test.js`'s.
"""

import base64
import hashlib
import importlib.util
import json
import re
from pathlib import Path

import pytest
from aiohttp.test_utils import make_mocked_request

from cyris.entrypoints.triage_server import STATIC_DIR, TriageServer, render_settings_page
from cyris.i18n import catalogs

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build", ROOT / "scripts/build_shared_settings.py")
build_script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_script)


@pytest.fixture(scope="module")
def built() -> dict:
    return build_script.build()


async def _get(path: str) -> bytes:
    """What a bare TriageServer answers `path` with, through its own router: a route's body, or
    the file its `/static/` resource maps the path to."""
    app = TriageServer()._app
    match = await app.router.resolve(make_mocked_request("GET", path))
    if "filename" in match:
        return (STATIC_DIR / match["filename"]).read_bytes()
    response = await match.handler(make_mocked_request("GET", path, match_info=match, app=app))
    assert response.status == 200, path
    return response.body


def _hash(text: str) -> str:
    return "sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()


def test_every_catalog_has_the_page_triageserver_renders_in_it(built) -> None:
    assert set(built["pages"]) == set(catalogs())
    for tag, page in built["pages"].items():
        assert page["html"] == render_settings_page(tag)


def test_each_page_names_the_hash_of_each_inline_block_and_no_other(built) -> None:
    for page in built["pages"].values():
        scripts = re.findall(r"<script>(.*?)</script>", page["html"], re.DOTALL)
        styles = re.findall(r"<style>(.*?)</style>", page["html"], re.DOTALL)
        assert scripts and styles
        assert page["script_hashes"] == [_hash(block) for block in scripts]
        assert page["style_hashes"] == [_hash(block) for block in styles]


async def test_each_file_is_what_triageserver_serves_at_its_path(built) -> None:
    for path, file in built["files"].items():
        assert file["body"].encode() == await _get(path), path


def test_the_files_are_what_the_pages_load_and_what_their_modules_import(built) -> None:
    loaded = {
        path
        for page in built["pages"].values()
        for path in re.findall(r'(?:src|href)="(/static/[^"]+)"', page["html"])
    }
    catalog_files = {f"/static/i18n/{tag}.json" for tag in catalogs()}

    assert loaded <= set(built["files"])
    assert catalog_files <= set(built["files"])
    assert {"/static/i18n.js", "/static/vendor/messageformat.js"} <= set(built["files"])
    assert all(path.startswith("/static/") for path in built["files"])


async def test_the_contract_parts_only_python_answers_are_triageservers(built) -> None:
    app = TriageServer()._app
    request = make_mocked_request("GET", "/api/settings", app=app)
    match = await app.router.resolve(request)
    answer = json.loads((await match.handler(request)).text)

    assert built["sent_as_shown"] == answer["sent_as_shown"]
    assert built["languages"] == answer["languages"]


def test_no_file_outside_static_is_built() -> None:
    with pytest.raises(ValueError, match="outside"):
        build_script._static("/static/../triage_server.py")


def test_the_build_writes_the_module_the_worker_imports(tmp_path) -> None:
    out = tmp_path / "built.json"

    assert build_script.main(["--out", str(out)]) == 0
    assert json.loads(out.read_text("utf-8"))["pages"].keys() == set(catalogs())
    assert build_script.OUT == ROOT / "workers/app/src/shared_settings.build.json"
    assert STATIC_DIR.is_dir()


def test_the_committed_build_is_what_the_script_builds_now(tmp_path) -> None:
    """The Worker, vitest and every other test import the committed file, never a fresh build, so
    a template, `static/` or catalog change committed without it reaches none of them."""
    out = tmp_path / "built.json"

    assert build_script.main(["--out", str(out)]) == 0
    assert out.read_text("utf-8") == build_script.OUT.read_text("utf-8"), (
        f"{build_script.OUT.relative_to(ROOT)} is stale: run "
        "`uv run python scripts/build_shared_settings.py` and commit the file it writes"
    )
