"""Receipts that the reader-facing pages follow docs/design/ui-language.md."""

import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from css_rules import parse_style_block

from cyris.adapters.store.article_store import ArticleStore
from cyris.entrypoints.triage_server import TriageServer


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
