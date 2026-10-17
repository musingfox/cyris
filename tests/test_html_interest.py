"""The interest page: an SVG map and a table saying the same in words, with tags and counts only."""

import re
from pathlib import Path

import pytest

import cyris.adapters.output.html_digest
from cyris.adapters.output.html_digest import HtmlDigestWriter
from cyris.domain.interest_graph import InterestEdge, InterestGraph, InterestNode

pytestmark = pytest.mark.unit

GENERATED = "2026-10-17 08:00 UTC"
ARTICLE_URL = "https://example.test/a1"
SECRET = "tok-secret-123"


def _fixture() -> InterestGraph:
    return InterestGraph(
        nodes=[
            InterestNode("a", 2, 0, "up", 1.0),
            InterestNode("b", 0, 1, "down", 0.5),
            InterestNode("c", 1, 1, "mixed", 0.0),
        ],
        edges=[InterestEdge("a", "b"), InterestEdge("b", "c")],
    )


def _render(graph: InterestGraph, *, holds_active: bool = True) -> str:
    return HtmlDigestWriter("unused").render_interest(
        graph, holds_active=holds_active, generated_at=GENERATED, clock_days=7
    )


def _unlit() -> InterestGraph:
    return InterestGraph(nodes=[InterestNode("a", 1, 0, "up", 0.0)], edges=[])


def test_one_circle_per_node_and_one_line_per_edge() -> None:
    html = _render(_fixture())

    assert html.count("<circle") == 3
    assert html.count("<line") == 2
    assert re.search(r'<circle[^>]*data-polarity="up"', html)


def test_the_table_row_states_a_tag_in_words() -> None:
    html = _render(_fixture())

    row = re.search(r"<tr><td lang=\"\">b</td>(.*?)</tr>", html)
    assert row is not None
    cells = re.findall(r"<td[^>]*>(.*?)</td>", row.group(0))
    assert cells == ["b", "0", "1", "down", "3.5"]


def test_an_empty_graph_says_so_and_draws_no_map() -> None:
    html = _render(InterestGraph(nodes=[], edges=[]))

    assert "No voted article carries a tag yet." in html
    assert "<svg" not in html


def test_a_tag_that_is_markup_is_escaped() -> None:
    graph = InterestGraph(
        nodes=[InterestNode("<script>alert(1)</script>", 1, 0, "up", 0.0)], edges=[]
    )

    html = _render(graph)

    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert" not in html


def test_colours_come_from_the_shared_tokens_only() -> None:
    # The shared partials are included, not written here; only this page's own text is checked.
    template = Path(cyris.adapters.output.html_digest.__file__).parent / "templates"
    own = re.sub(r"\{%.*?%\}", "", (template / "interest.html.j2").read_text())

    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", own)
    assert "rgb(" not in own


def test_the_page_names_no_article_and_no_secret() -> None:
    html = _render(_fixture())

    assert ARTICLE_URL not in html
    assert SECRET not in html


def test_the_head_is_the_shared_page_head() -> None:
    html = _render(_fixture())

    assert '<link rel="icon" href="favicon.svg" type="image/svg+xml">' in html
    assert '<meta name="description" content=' in html
    assert '<meta name="color-scheme" content="dark">' in html
    assert html.startswith('<!DOCTYPE html>\n<html lang="en">')


def test_a_lit_node_gets_the_legend() -> None:
    assert "Lit = suppressed now" in _render(_fixture())


def test_an_unlit_map_with_active_holds_does_not_say_none_is_active() -> None:
    html = _render(_unlit(), holds_active=True)

    assert "No suppression is active." not in html
    assert "Suppressions are active, but none is near a tag on this map." in html


def test_an_unlit_map_with_no_active_hold_says_none_is_active() -> None:
    assert "No suppression is active." in _render(_unlit(), holds_active=False)
