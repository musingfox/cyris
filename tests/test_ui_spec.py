"""Receipts that the reader-facing pages follow docs/design/ui-language.md."""

import functools
import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from css_rules import COMPONENT_SELECTORS, PROTOTYPE, parse_style_block, receipt_fixtures

from cyris.adapters.output.html_digest import HtmlDigestWriter
from cyris.adapters.store.article_store import ArticleStore
from cyris.entrypoints.triage_server import TriageServer


def _render_partial(name: str) -> str:
    return HtmlDigestWriter("unused-by-these-tests").env.get_template(name).render()


FOCUS = {"outline: 1px solid var(--accent)", "outline-offset: 2px"}

REDUCED_MOTION = "@media (prefers-reduced-motion: reduce) | *, *::before, *::after"
STILL = {
    "transition: none !important",
    "animation: none !important",
    "scroll-behavior: auto !important",
}

# Section 4's interaction rules, which the components partial carries alongside
# the components themselves.
INTERACTION_KEYS = frozenset({":focus-visible", REDUCED_MOTION})


def _component_key_problems(css: str) -> list[str]:
    keys = set(parse_style_block(f"<style>{css}</style>"))
    expected = COMPONENT_SELECTORS | INTERACTION_KEYS
    return [f"extra {key}" for key in sorted(keys - expected)] + [
        f"missing {key}" for key in sorted(expected - keys)
    ]


def _focus_problems(html: str) -> list[str]:
    declarations = parse_style_block(html).get(":focus-visible")
    if declarations is None:
        return ["missing :focus-visible"]
    return [] if declarations == FOCUS else [f":focus-visible is {sorted(declarations)}"]


@pytest.fixture
async def triage(tmp_path: Path) -> AsyncIterator[TestClient]:
    client = TestClient(TestServer(TriageServer(ArticleStore(tmp_path))._app))
    await client.start_server()
    yield client
    await client.close()


async def _stylesheet_hrefs(client: TestClient, path: str) -> list[str]:
    response = await client.get(path)
    assert response.status == 200
    links = re.findall(r"<link\b[^>]*>", await response.text())
    hrefs = [
        match.group(1)
        for link in links
        if 'rel="stylesheet"' in link and (match := re.search(r'href="(/static/[^"]+)"', link))
    ]
    return hrefs


async def _served_rules(client: TestClient, href: str) -> dict[str, set[str] | list[str]]:
    response = await client.get(href)
    assert response.status == 200, f"{href} answered {response.status}"
    return parse_style_block(f"<style>{await response.text()}</style>")


async def test_the_deck_links_the_shared_stylesheet_then_its_own(triage: TestClient) -> None:
    assert await _stylesheet_hrefs(triage, "/triage") == ["/static/style.css", "/static/deck.css"]


async def test_settings_links_only_the_shared_stylesheet(triage: TestClient) -> None:
    assert await _stylesheet_hrefs(triage, "/settings") == ["/static/style.css"]


async def test_the_deck_stylesheet_carries_the_deck_rules(triage: TestClient) -> None:
    rules = await _served_rules(triage, "/static/deck.css")
    assert "transform: translateX(-150vw) rotate(-20deg)" in rules[".card.fly-left"]
    assert {"overflow: hidden", "touch-action: pan-y"} <= set(rules["body"])


async def test_the_shared_stylesheet_carries_no_deck_rule(triage: TestClient) -> None:
    rules = await _served_rules(triage, "/static/style.css")
    deck_only = {".card", "#toast", "#undo-toast", ".nav-btn", "#filter-tabs .tab", ".hidden"}
    assert sorted(deck_only & set(rules)) == []


async def test_the_shared_body_rule_has_no_deck_layout(triage: TestClient) -> None:
    body = (await _served_rules(triage, "/static/style.css"))["body"]
    assert {"background: var(--bg)", "color: var(--text)"} <= set(body)
    properties = {declaration.split(":", 1)[0] for declaration in body}
    assert not properties & {"display", "overflow", "touch-action", "min-height", "font-size"}


@pytest.mark.parametrize("page", [0, 1, 2], ids=["index", "digest", "raw"])
def test_every_digest_page_shows_keyboard_focus(page: int) -> None:
    assert _focus_problems(receipt_fixtures()[page]) == []


async def test_settings_and_the_deck_show_keyboard_focus(triage: TestClient) -> None:
    assert (await _served_rules(triage, "/static/style.css"))[":focus-visible"] == FOCUS


def test_a_page_without_a_focus_rule_is_reported() -> None:
    assert _focus_problems("<style>a { color: var(--text); }</style>") == ["missing :focus-visible"]


@pytest.mark.parametrize("page", [0, 1, 2], ids=["index", "digest", "raw"])
def test_every_digest_page_stops_motion_on_request(page: int) -> None:
    assert parse_style_block(receipt_fixtures()[page])[REDUCED_MOTION] == STILL


async def test_settings_and_the_deck_stop_motion_on_request(triage: TestClient) -> None:
    assert (await _served_rules(triage, "/static/style.css"))[REDUCED_MOTION] == STILL


def test_no_keyframes_override_is_left_under_reduced_motion() -> None:
    _, digest, _ = receipt_fixtures()
    prefix = "@media (prefers-reduced-motion: reduce) | @keyframes"
    assert [key for key in parse_style_block(digest) if key.startswith(prefix)] == []


def test_the_components_partial_defines_exactly_the_listed_components() -> None:
    assert _component_key_problems(_render_partial("_components.css.j2")) == []


@pytest.mark.parametrize("key", sorted(COMPONENT_SELECTORS))
def test_each_component_matches_the_prototype(key: str) -> None:
    partial = parse_style_block(f"<style>{_render_partial('_components.css.j2')}</style>")
    assert partial[key] == parse_style_block(PROTOTYPE.read_text())[key]


def test_a_rule_outside_the_component_list_is_reported_as_extra() -> None:
    planted = _render_partial("_components.css.j2") + "\n.row { padding: 0; }"
    assert _component_key_problems(planted) == ["extra .row"]


def test_the_digest_score_pill_is_the_component() -> None:
    _, digest, _ = receipt_fixtures()
    assert parse_style_block(digest)[".pill.score"] == {
        "color: var(--accent)",
        "border-color: var(--accent-dim)",
        "background: var(--accent-tint)",
    }


def test_raw_states_are_the_component() -> None:
    _, _, raw = receipt_fixtures()
    rules = parse_style_block(raw)
    assert rules[".state.rejected"] == {"color: var(--text-faint)"}
    assert rules[".state.pending"] == {"color: var(--text-dim)"}
    assert "#6b4a4a" not in raw


@functools.cache
def _parsed(page: str) -> dict[str, set[str] | list[str]]:
    index, digest, raw = receipt_fixtures()
    return parse_style_block({"index": index, "digest": digest, "raw": raw}[page])


def test_the_raw_state_column_fits_the_larger_state_label() -> None:
    raw = _parsed("raw")
    assert "grid-template-columns: 96px 46px 1fr auto" in raw[".article"]
    assert "grid-template-columns: 96px 1fr auto" in raw["@media (max-width: 640px) | .article"]
