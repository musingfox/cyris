"""The theme choices live in five places, pinned to be the same list.

`digest.theme` is a Literal in config, a palette in themes.py, the options on
the settings page's select, a style text in the Worker's theme.js, and —
delivered live, never baked — the `<style>` that three surfaces inject: the app
Worker on Cloudflare, triage-ui's archive and interest pages, and `cyris serve`.
A choice added in one place and not the others is a page the reader cannot
switch to, or a theme one surface cannot draw, so the lists must agree and the
default must answer no style at all.
"""

import json
import re
from pathlib import Path
from typing import get_args

import pytest
from aiohttp.test_utils import TestClient, TestServer

from cyris.adapters.output.themes import PALETTES, THEMES, theme_style
from cyris.config import DigestConfig
from cyris.entrypoints.serve import PageServer
from cyris.entrypoints.triage_server import TriageServer, render_settings_page

pytestmark = [pytest.mark.unit, pytest.mark.guard]

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src/cyris/adapters/output/templates"
WORKER_JS = ROOT / "workers/app/src/theme.js"


def _ids() -> list[str]:
    return [value for value, _label in THEMES]


def _js_styles() -> dict[str, str]:
    text = WORKER_JS.read_text(encoding="utf-8")
    start = text.index("export const THEME_STYLES = ") + len("export const THEME_STYLES = ")
    # raw_decode stops at the balanced object: no style text is safe to cut at.
    styles, _ = json.JSONDecoder().raw_decode(text[start:])
    return styles


def test_the_literal_the_palettes_and_the_page_offer_the_same_themes() -> None:
    setting = list(get_args(DigestConfig.model_fields["theme"].annotation))
    assert setting == _ids()
    assert _ids() == ["default"] + list(PALETTES)
    # The control id must differ from the category hash's own word: a hash
    # that scrolls to a control targets the control, not the category.
    page = re.search(
        r'<select class="select" id="page-theme">(.*?)</select>', render_settings_page(), re.S
    )
    assert page, "the settings page has no theme select"
    assert re.findall(r'<option value="([^"]*)"', page.group(1)) == _ids()


def test_the_worker_carries_the_same_style_the_python_side_renders() -> None:
    js = _js_styles()
    assert set(js) == set(PALETTES)
    for value, _label in THEMES[1:]:
        assert js[value] == theme_style(value), value


def test_default_answers_no_style_and_every_theme_answers_the_same_shape() -> None:
    assert theme_style("default") == ""
    assert theme_style("no-such-theme") == ""
    for value, _label in THEMES[1:]:
        style = theme_style(value)
        # Every palette overrides exactly the fifteen colour tokens, and the
        # three colour literals the shared partial keeps outside the tokens;
        # the page's own sizes are never restated.
        assert style.count("--") == 15, value
        assert ".btn.primary:where(:not(:disabled)):hover" in style, value
        assert ".site-bar{" in style, value
        assert ".brand-mark{" in style, value
        assert "--type-scale" not in style
        assert "--measure" not in style


async def _client(app) -> TestClient:
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    return test_client


@pytest.fixture
def pages(tmp_path: Path) -> Path:
    output_dir = tmp_path / "html"
    output_dir.mkdir()
    (output_dir / "index.html").write_text("<head></head>", encoding="utf-8")
    return output_dir


async def test_triage_injects_the_saved_theme_into_served_pages(pages: Path) -> None:
    server = TriageServer(values={"digest.theme": "nord"}, archive_dir=pages)
    client = await _client(server._app)
    try:
        served = await (await client.get("/")).text()
        assert "<style>" + theme_style("nord") + "</style>" in served

        # What a save on the Theme page does: the next read carries it.
        server._values["digest.theme"] = "tokyo_night"
        served = await (await client.get("/")).text()
        assert theme_style("tokyo_night") in served
        assert theme_style("nord") not in served
    finally:
        await client.close()


async def test_serve_injects_what_its_theme_reader_answers(pages: Path) -> None:
    reader = {"theme": "catppuccin"}
    client = await _client(PageServer(pages, theme=lambda: reader["theme"]).build_app())
    try:
        assert theme_style("catppuccin") in await (await client.get("/")).text()
        reader["theme"] = "default"
        assert theme_style("catppuccin") not in await (await client.get("/")).text()
    finally:
        await client.close()


def test_no_page_template_still_consumes_a_theme_variable() -> None:
    # The bake this feature started with waited for the next run; injection
    # replaced it, so the templates render tokens only, whatever a caller
    # happens to pass them.
    for name in ("digest.html.j2", "index.html.j2", "raw.html.j2", "interest.html.j2"):
        assert "theme_style" not in (TEMPLATES / name).read_text(encoding="utf-8"), name
