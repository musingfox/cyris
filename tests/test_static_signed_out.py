"""The settings, labels and vote pages send each request with redirect manual."""

import re
from pathlib import Path

import pytest

import cyris.entrypoints
from cyris.adapters.output import html_digest

pytestmark = [pytest.mark.unit, pytest.mark.guard]

STATIC = Path(cyris.entrypoints.__file__).parent / "static"
TEMPLATES = Path(html_digest.__file__).parent / "templates"
SETTINGS = STATIC / "settings.js"
LABELS = STATIC / "labels.js"
PROMOTE = TEMPLATES / "_promote_script.html.j2"
ACCESS_NOT_SET_UP = "Access is not set up for this deployment"


def fetch_calls(source: str) -> list[str]:
    """The options text of each fetch( call, from its opening paren to the matching close."""
    calls = []
    for match in re.finditer(r"\bfetch\(", source):
        depth = 0
        for index in range(match.end() - 1, len(source)):
            if source[index] == "(":
                depth += 1
            elif source[index] == ")":
                depth -= 1
                if depth == 0:
                    calls.append(source[match.end() - 1 : index + 1])
                    break
    return calls


def unredirected_fetches(source: str) -> list[str]:
    return [call for call in fetch_calls(source) if "redirect" not in call]


@pytest.mark.parametrize(
    ("path", "expected"),
    [(SETTINGS, 5), (LABELS, 2), (PROMOTE, 1)],
    ids=["settings", "labels", "promote"],
)
def test_every_fetch_in_the_signed_in_pages_sets_redirect(path, expected):
    source = path.read_text()

    assert len(fetch_calls(source)) == expected
    assert unredirected_fetches(source) == []


def test_a_fetch_without_redirect_options_is_a_violation():
    assert unredirected_fetches("fetch('/api/x')") == ["('/api/x')"]


def test_settings_names_the_access_refusal_once():
    source = SETTINGS.read_text()

    assert source.count(ACCESS_NOT_SET_UP) == 1
    assert 'const ACCESS_NOT_SET_UP = new Error("' in source


@pytest.mark.parametrize("path", [SETTINGS, LABELS, PROMOTE], ids=["settings", "labels", "promote"])
def test_no_page_branches_on_a_401_no_worker_sends(path):
    """The app Worker sends no 401, and the promote Worker's refused bearer only passes through."""
    assert "status === 401" not in path.read_text()
