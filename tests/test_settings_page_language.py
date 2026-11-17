"""/settings is worded from the catalog of the language it is rendered in (ADR-0027)."""

import json
from html.parser import HTMLParser
from pathlib import Path

import pytest

import cyris
from cyris.entrypoints.triage_server import render_settings_page

pytestmark = pytest.mark.unit

LOCALES = Path(cyris.__file__).parent / "locales"
# The attributes a reader hears or sees as words.
WORDED_ATTRIBUTES = ("aria-label", "placeholder", "title")
# Placeholders that show the shape of a value, commands and names, which no language words.
VALUE_SHAPES = {
    "Clef",
    "https://discord.com/api/webhooks/...",
    "you@example.com",
    "digest@your-domain",
    "from:hi@example.com",
    "cyris vote-sim",
    "cyris topic-sim",
}


class _Words(HTMLParser):
    """Every text node and worded attribute of a page, outside its style, its scripts and the
    shared site bar, which this page does not word."""

    def __init__(self) -> None:
        super().__init__()
        self.words: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs) -> None:
        attributes = dict(attrs)
        if tag in ("style", "script") or attributes.get("class") == "site-bar":
            self._skip += 1
        if self._skip:
            return
        self.words += [attributes[a] for a in WORDED_ATTRIBUTES if attributes.get(a)]
        if tag == "meta" and attributes.get("name") == "description":
            self.words.append(attributes["content"])

    def handle_endtag(self, tag) -> None:
        if tag in ("style", "script", "header"):
            self._skip -= 1

    def handle_data(self, data) -> None:
        if not self._skip and data.strip():
            self.words.append(data.strip())


def _words(page: str) -> list[str]:
    parser = _Words()
    parser.feed(page)
    return parser.words


def _catalog(tag: str) -> dict:
    return json.loads((LOCALES / f"{tag}.json").read_text(encoding="utf-8"))


def test_the_english_page_keeps_its_copy() -> None:
    page = render_settings_page("en")

    assert '<html lang="en">' in page
    assert '<meta name="i18n-catalog" content="/static/i18n/en.json">' in page
    assert '<h1 class="display">How cyris <em>runs</em></h1>' in page
    assert '<label class="label" for="timezone">Timezone</label>' in page
    assert "Preview what it would drop with <code>cyris vote-sim</code>; turning it on" in page
    assert "<code>cyris topic-sim</code> ranks the window&#39;s titles" in page
    assert '<button type="button" data-filter="newsletter" aria-pressed="false">Newsletter' in page


def test_the_traditional_chinese_page_is_worded_in_chinese() -> None:
    page = render_settings_page("zh-TW")

    assert '<html lang="zh-Hant">' in page
    assert '<meta name="i18n-catalog" content="/static/i18n/zh-Hant.json">' in page
    assert "<title>Cyris 設定</title>" in page
    assert '<h1 class="display">cyris 如何<em>運作</em></h1>' in page
    assert '<label class="label" for="timezone">時區</label>' in page
    assert "可用 <code>cyris vote-sim</code> 預覽會排除哪些" in page
    assert '<option value="0.875">較小</option><option value="1">預設</option>' in page
    assert '<button type="button" data-filter="newsletter" aria-pressed="false">電子報' in page


def test_the_chinese_page_shows_no_english_its_catalog_translates() -> None:
    english, chinese = _catalog("en"), _catalog("zh-Hant")
    # English stays where the Chinese catalog has no other text: a name such as RSS, or a
    # key another surface has not translated yet.
    untranslated = {
        text for key, text in english.items() if key not in chinese or chinese[key]["text"] == text
    }
    english_words = set(_words(render_settings_page("en")))

    left = english_words & set(_words(render_settings_page("zh-Hant")))

    assert left - untranslated - VALUE_SHAPES == set()


@pytest.mark.parametrize("tag", ["en", "zh-Hant"])
def test_an_element_inside_a_sentence_is_the_templates_own(tag) -> None:
    page = render_settings_page(tag)

    assert page.count("<code>cyris vote-sim</code>") == 1
    assert page.count("<code>cyris topic-sim</code>") == 1
    assert page.count("<em>") == 1
    assert "{#" not in page
    assert "\ue000" not in page
