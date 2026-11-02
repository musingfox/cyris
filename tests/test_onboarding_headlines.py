"""The opening's headline pool: each sample sits under a feed the catalog lists.

`docs/design/ui-language.md` §6 *onboarding* draws each headline under its feed's
name, and never shows one title twice at once; the page enforces the second by
drawing only titles not already on screen, which holds only while no two entries
share a title.
"""

import json
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.guard]

SRC = Path(__file__).resolve().parents[1] / "src" / "cyris"


def _headlines() -> list[dict]:
    return json.loads((SRC / "onboarding_headlines.json").read_text())["headlines"]


def test_every_entry_is_a_feed_and_a_title() -> None:
    entries = _headlines()
    assert entries, "the pool is empty"
    bad = [e for e in entries if set(e) != {"feed", "title"} or not e["title"].strip()]
    assert bad == []


def test_every_feed_is_one_the_catalog_lists() -> None:
    catalog = {e["name"] for e in json.loads((SRC / "onboarding_feeds.json").read_text())["feeds"]}
    assert sorted({e["feed"] for e in _headlines()} - catalog) == []


def test_no_two_entries_share_a_title() -> None:
    titles = [e["title"] for e in _headlines()]
    assert len(titles) == len(set(titles))
