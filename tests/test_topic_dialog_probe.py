"""The topic dialog probe's fixture and registry, which need no browser.

`scripts/topic_dialog_probe.py` drives the dialog in Chromium, Firefox and WebKit; that run
is a reviewer's gate, not a test. What is tested is the part that needs none: its page holds
a topic long enough to scroll with an excerpt on every member, every check carries a
sabotage whose text is still on that page, and the checks cover each acceptance criterion.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import topic_dialog_probe  # noqa: E402

pytestmark = pytest.mark.integration


def test_the_probe_page_opens_its_long_topic_from_the_first_card():
    page = topic_dialog_probe.render_page()
    long_topic = topic_dialog_probe.content().news_clusters[0]

    assert topic_dialog_probe.CARD == "#topic-1"
    assert 'id="topic-1-dialog"' in page
    assert len(long_topic.members) == topic_dialog_probe.LONG_MEMBERS
    for member in long_topic.members:
        assert 0 < len(member.summary) <= 80
        assert member.summary in page


@pytest.mark.parametrize("check", topic_dialog_probe.CHECKS, ids=lambda check: check.id)
def test_every_sabotage_still_finds_its_text_on_the_page(check):
    page = topic_dialog_probe.render_page()

    assert page.count(check.sabotage.old) == 1
    assert check.sabotage.apply(page) != page


def test_the_checks_are_named_once_and_cover_every_browser_criterion():
    ids = [check.id for check in topic_dialog_probe.CHECKS]

    assert len(ids) == len(set(ids))
    assert {check.criterion for check in topic_dialog_probe.CHECKS} == {
        "1",
        "2",
        "3",
        "4",
        "6",
        "7",
    }
    assert {check.context for check in topic_dialog_probe.CHECKS} == {
        "desktop",
        "reduced",
        "phone",
        "noscript",
    }


def test_importing_the_probe_needs_no_browser():
    assert "playwright" not in sys.modules
