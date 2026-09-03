"""The Worker route fixture must cover triage_server and router.js path literals."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "workers/app/test/routes.json"
TRIAGE = ROOT / "src/cyris/entrypoints/triage_server.py"
ROUTER = ROOT / "workers/app/src/router.js"
WRANGLER = ROOT / "workers/app/wrangler.toml"
README = ROOT / "README.md"
APP_README = ROOT / "workers/app/README.md"
ARCHITECTURE = ROOT / "docs/architecture.md"


def load_fixture(text: str | None = None) -> list[dict]:
    return json.loads(text if text is not None else FIXTURE.read_text())


# Path literals in router.js that are not routes this Worker serves. Every other
# "/…" literal must appear in routes.json — an allow-list of prefixes would let a
# route outside those prefixes (say /admin/purge) reach the container unnamed.
ROUTER_PATH_OPT_OUT = {
    # Appended to CYRIS_PROMOTE_WORKER_URL: a path on the *promote* Worker
    # upstream, never matched against this Worker's own request URL.
    "/promote",
}


def triage_route_literals(src: str) -> list[str]:
    # add_route takes the method first, so the path is its second string.
    return re.findall(
        r'add_(?:get|post|put|patch|delete|head|options|view|static)\("([^"]+)"', src
    ) + re.findall(r'add_route\(\s*"[^"]+"\s*,\s*"([^"]+)"', src)


def router_path_literals(src: str) -> list[str]:
    return [lit for lit in re.findall(r'"(/[^"]*)"', src) if lit not in ROUTER_PATH_OPT_OUT]


def assert_triage_covered(fixture: list[dict], triage_src: str) -> None:
    named = {row.get("container_route") for row in fixture}
    named |= {row["path"] for row in fixture}
    named.discard(None)
    missing = [route for route in triage_route_literals(triage_src) if route not in named]
    assert not missing, "uncovered triage routes: " + ", ".join(missing)


def _router_lit_covered(lit: str, paths: list[str]) -> bool:
    for path in paths:
        if lit == path:
            return True
        if len(lit) > 1 and lit.endswith("/") and path.startswith(lit):
            return True
        if len(path) > 1 and path.endswith("/") and lit.startswith(path):
            return True
    return False


def assert_router_covered(fixture: list[dict], router_src: str) -> None:
    paths = [row["path"] for row in fixture]
    missing = [
        lit for lit in router_path_literals(router_src) if not _router_lit_covered(lit, paths)
    ]
    assert not missing, "uncovered router path " + ", ".join(missing)


def assert_fork_neutral(text: str) -> None:
    assert "musingfox" not in text, "musingfox"
    assert "cyris-digest" not in text, "cyris-digest"
    assert "routes =" not in text, "routes ="
    assert "workers_dev = true" in text
    assert "preview_urls = false" in text
    assert "workers_dev = false" not in text, "workers_dev = false"


def test_fixture_covers_current_triage_and_router():
    fixture = load_fixture()
    assert_triage_covered(fixture, TRIAGE.read_text())
    assert_router_covered(fixture, ROUTER.read_text())


def test_missing_schedule_row_is_named():
    fixture = [
        row
        for row in load_fixture()
        if row.get("container_route") != "/api/settings/schedule"
        and row["path"] != "/api/settings/schedule"
    ]
    with pytest.raises(AssertionError, match=r"/api/settings/schedule"):
        assert_triage_covered(fixture, TRIAGE.read_text())


def test_new_router_path_without_fixture_row_is_named():
    src = ROUTER.read_text() + '\nconst extra = "/admin/purge";\n'
    with pytest.raises(AssertionError, match=r"/admin/purge"):
        assert_router_covered(load_fixture(), src)


def test_add_route_registration_without_fixture_row_is_named():
    added = '\n        self._app.router.add_route("PUT", "/api/nuke", self._handle_index)\n'
    src = TRIAGE.read_text() + added
    with pytest.raises(AssertionError, match=r"/api/nuke"):
        assert_triage_covered(load_fixture(), src)


def test_wrangler_toml_is_fork_neutral():
    assert_fork_neutral(WRANGLER.read_text())


def test_personal_hostname_in_wrangler_fails():
    with pytest.raises(AssertionError):
        assert_fork_neutral(WRANGLER.read_text() + "\ndigest.musingfox.me\n")


def test_workers_dev_false_in_wrangler_fails():
    with pytest.raises(AssertionError):
        assert_fork_neutral("workers_dev = false\npreview_urls = false\n")


def test_app_readme_names_deployer_keys():
    text = APP_README.read_text()
    assert "CYRIS_UI_ACCESS_HOST" in text
    assert "DIGEST_ORIGIN" in text


def test_architecture_grade_b_table_names_access_host():
    text = ARCHITECTURE.read_text()
    section = text.split("### Every setting, graded", 1)[1]
    table, _, _ = section.partition("###")
    assert "CYRIS_UI_ACCESS_HOST" in table


def test_what_needs_what_does_not_require_a_domain_for_access():
    text = README.read_text()
    table = text.split("### What needs what", 1)[1].split("### Where RSS comes from", 1)[0]
    assert "Access needs **your own domain**" not in table
