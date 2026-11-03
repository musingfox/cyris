"""Tests for `plain_text` and `page_text`, feed-entry and web-page HTML to body text."""

import pytest
from captioned_post import FIRST_SENTENCE, POST_HTML

from cyris.adapters.fetch.html_text import page_text, plain_text

pytestmark = pytest.mark.unit

TABLE_POST = (
    "<p>Here are the quarterly results for the three regions we track closely.</p>"
    "<table><tr><th>Region</th><th>Revenue</th></tr>"
    "<tr><td>Alpha</td><td>120</td></tr>"
    "<tr><td>Beta</td><td>340</td></tr></table>"
    "<p>That is all for now, thanks for reading along.</p>"
)


def test_table_cells_stay_in_the_text():
    text = plain_text(TABLE_POST)

    assert text.startswith("Here are the quarterly results")
    for cell in ("Region", "Revenue", "Alpha", "120", "Beta", "340"):
        assert cell in text


def test_the_captioned_post_still_opens_with_its_first_sentence():
    assert plain_text(POST_HTML).startswith(FIRST_SENTENCE)


# The inputs below are too short or bare for trafilatura, so they reach the fallback.


@pytest.mark.parametrize(
    "prose",
    [
        "Price < $5 and > $1 this week only.",
        "if x <3 then y >5, ship it.",
    ],
)
def test_prose_with_bare_angle_brackets_stays_whole(prose):
    assert plain_text(prose) == prose


def test_a_tag_the_feed_cut_off_leaks_no_markup():
    text = plain_text('<p>A teaser cut off mid tag <img src="https://a.test/x.png" alt="pic')

    assert text.startswith("A teaser cut off mid tag")
    assert "<img" not in text
    assert "src=" not in text
    assert "a.test" not in text


def test_an_attribute_holding_a_closing_bracket_does_not_end_the_tag():
    assert plain_text('<p>Teaser <img alt="a > b" src="x.png"> end.</p>') == "Teaser end."


def test_the_fallback_decodes_entities_once():
    assert plain_text("<p>Tom &amp; Jerry</p>") == "Tom & Jerry"


def test_the_fallback_keeps_neighbouring_paragraphs_apart():
    assert plain_text("<td>Tom</td><td>Jerry</td>") == "Tom Jerry"


def test_a_figure_caption_ahead_of_the_body_does_not_open_the_text():
    """Only an extractor drops the caption; a plain tag strip would start with it."""
    body = "The body sentence starts here and goes on for a while."
    text = plain_text(
        '<figure><img src="https://img.test/x.png">'
        "<figcaption>caption words here</figcaption></figure>"
        f"<p>{body}</p><p>A second paragraph follows the first.</p>"
    )

    assert text.startswith(body)
    assert "caption words" not in text


def test_a_parser_assertion_falls_back_instead_of_failing_the_read(monkeypatch):
    """CPython 3.12 before 3.12.16 asserts in html.parser on `<![` plus a non-name."""

    def older_parser_feed(self, data):
        raise AssertionError("unexpected call to parse_marked_section()")

    monkeypatch.setattr("cyris.adapters.fetch.html_text._TextCollector.feed", older_parser_feed)
    assert plain_text("<p>Tom &amp; Jerry</p>") == "Tom & Jerry"


def test_a_page_with_no_article_reads_as_no_text_not_its_tags():
    """A whole page has no fallback: stripped, its markup would be navigation and footer."""
    page = "<html><body><script>var x = 1;</script></body></html>"

    assert plain_text(page) != ""
    assert page_text(page) == ""


def test_a_page_given_as_bytes_is_decoded_by_its_own_meta_charset():
    body = "這一段正文說明了別人制定的標準，如何塑造我們選擇去做的工作。" * 6
    page = (
        '<html><head><meta charset="big5"></head><body>'
        f"<article><p>{body}</p></article></body></html>"
    ).encode("big5")

    assert body[:30] in page_text(page)
