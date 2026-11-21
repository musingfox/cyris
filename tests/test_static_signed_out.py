"""The settings, labels and vote pages send each request with redirect manual."""

import json
import re
from pathlib import Path

import pytest

import cyris
import cyris.entrypoints
from cyris.adapters.output import html_digest

pytestmark = [pytest.mark.unit, pytest.mark.guard]

STATIC = Path(cyris.entrypoints.__file__).parent / "static"
TEMPLATES = Path(html_digest.__file__).parent / "templates"
SETTINGS = STATIC / "settings.js"
LABELS = STATIC / "labels.js"
PROMOTE = TEMPLATES / "_promote_script.html.j2"
LOCALES = Path(cyris.__file__).parent / "locales"
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
    english = json.loads((LOCALES / "en.json").read_text(encoding="utf-8"))

    assert source.count('t("settings.access_not_set_up")') == 1
    assert 'const ACCESS_NOT_SET_UP = new Error(t("settings.access_not_set_up"));' in source
    assert english["settings.access_not_set_up"].startswith(ACCESS_NOT_SET_UP)


@pytest.mark.parametrize("path", [SETTINGS, LABELS], ids=["settings", "labels"])
def test_no_page_branches_on_a_401_no_worker_sends(path):
    """The app Worker sends no 401, and the promote Worker's refused bearer only passes through."""
    assert "status === 401" not in path.read_text()


def test_the_vote_script_branches_on_a_401_only_for_the_hosted_workers_ended_session():
    """The hosted Worker answers a vote token whose session ended with 401 and a code (ADR-0039).

    The promote Worker's 401 still only passes through, so the script reads the code, never
    the status alone: a refused promote bearer is not a sign-in the reader can mend.
    """
    source = PROMOTE.read_text()

    assert source.count("status === 401") == 1
    branch = source.split("status === 401", 1)[1].split("status === 403", 1)[0]
    assert "body.code === 'vote_session_ended'" in branch
