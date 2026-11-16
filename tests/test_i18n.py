"""How a requested language reaches a catalog, and how a message is formatted (ADR-0027)."""

import json
from pathlib import Path

import pytest
from messageformat2.errors import FormatError

from cyris import i18n

pytestmark = pytest.mark.unit


@pytest.fixture
def shipped():
    """Each test starts and ends with the shipped catalogs read fresh."""
    yield
    for cached in (i18n.catalogs, i18n._message, i18n._translator):
        cached.cache_clear()


@pytest.fixture
def locales(tmp_path: Path, monkeypatch, shipped):
    """Catalogs of a test's own, in place of the shipped ones."""

    def write(catalogs: dict[str, dict]) -> None:
        for tag, entries in catalogs.items():
            (tmp_path / f"{tag}.json").write_text(json.dumps(entries), encoding="utf-8")
        monkeypatch.setattr(i18n, "LOCALES", tmp_path)
        for cached in (i18n.catalogs, i18n._message, i18n._translator):
            cached.cache_clear()

    return write


@pytest.mark.parametrize(
    ("requested", "catalog"),
    [
        ("zh-Hant", "zh-Hant"),
        ("zh-TW", "zh-Hant"),
        ("zh-HK", "zh-Hant"),
        ("zh-MO", "zh-Hant"),
        ("en", "en"),
        ("en-GB", "en"),
    ],
)
def test_a_tag_reaches_its_catalog_by_cldr_matching(requested, catalog, shipped) -> None:
    assert i18n.match(requested) == catalog


@pytest.mark.parametrize("requested", ["ja", "fr", "zh", "zh-Hans"])
def test_a_tag_no_catalog_is_close_to_matches_none(requested, shipped) -> None:
    assert i18n.match(requested) is None
    assert i18n.resolve(requested) == "en"


@pytest.mark.parametrize("value", ["English", "繁體中文", "", "*", "zh_TW!"])
def test_a_value_that_is_not_a_tag_is_worded_in_english(value, shipped) -> None:
    assert i18n.match(value) is None
    assert i18n.resolve(value) == "en"


@pytest.mark.parametrize(
    ("header", "catalog"),
    [
        ("zh-TW,zh;q=0.9,en;q=0.8", "zh-Hant"),
        ("fr", None),
        ("fr, en;q=0.5", "en"),
        ("en;q=0.5, zh-TW", "zh-Hant"),
        ("zh-TW;q=0, en;q=0.1", "en"),
        ("*", None),
        ("*, zh-HK;q=0.2", "zh-Hant"),
        ("English, zh-TW;q=0.5", "zh-Hant"),
        ("zh-TW;q=oops, en;q=0.3", "en"),
        ("en;q=1.5, zh-TW", "zh-Hant"),
        ("zh-TW;q=-0.5, en;q=0.3", "en"),
        ("", None),
    ],
)
def test_accept_language_takes_the_first_tag_that_matches_by_weight(
    header, catalog, shipped
) -> None:
    assert i18n.accept_language(header) == catalog


def test_equal_weights_keep_the_order_sent(shipped) -> None:
    assert i18n.accept_language("zh-TW;q=0.5, en;q=0.5") == "zh-Hant"
    assert i18n.accept_language("en;q=0.5, zh-TW;q=0.5") == "en"


def test_a_translator_is_one_per_catalog_not_per_tag(shipped) -> None:
    assert i18n.translator("zh-TW") is i18n.translator("zh-Hant")
    assert i18n.translator("ja") is i18n.translator("en")


def test_a_key_the_catalog_lacks_falls_back_to_english_in_english(locales) -> None:
    locales(
        {
            "en": {"a.count": "{$n :number} items", "a.both": "Both"},
            "zh-Hant": {"a.both": {"text": "兩者", "source": "Both"}},
        }
    )
    t = i18n.translator("zh-Hant")

    assert t("a.both") == "兩者"
    assert t("a.count", n=1234567) == "1,234,567 items"
    assert t.origin("a.count") == "en"
    assert t.origin("a.both") == "zh-Hant"


def test_a_message_is_formatted_in_its_catalogs_locale(locales) -> None:
    locales(
        {
            "en": {"a.n": "{$n :number}"},
            "de": {"a.n": {"text": "{$n :number}", "source": "{$n :number}"}},
        }
    )
    assert i18n.translator("de")("a.n", n=1234.5) == "1.234,5"
    assert i18n.translator("en")("a.n", n=1234.5) == "1,234.5"


def test_a_key_english_lacks_raises_even_where_a_translation_has_it(locales) -> None:
    locales({"en": {}, "zh-Hant": {"a.only": {"text": "只有", "source": "Only"}}})

    with pytest.raises(KeyError, match="a.only"):
        i18n.translator("zh-Hant")("a.only")


def test_a_missing_variable_raises(locales) -> None:
    locales({"en": {"a.hello": "Hello {$name}"}})

    with pytest.raises(FormatError):
        i18n.translator("en")("a.hello")


def test_a_placeholder_gets_no_bidi_isolation(locales) -> None:
    locales({"en": {"a.hello": "Hello {$name}"}})

    assert i18n.translator("en")("a.hello", name="Ada") == "Hello Ada"


def test_a_plural_follows_the_catalogs_cldr_categories(locales) -> None:
    message = ".input {$n :integer}\n.match $n\none {{{$n} issue}}\n* {{{$n} issues}}"
    locales({"en": {"a.issues": message}})
    t = i18n.translator("en")

    assert (t("a.issues", n=1), t("a.issues", n=2)) == ("1 issue", "2 issues")


def test_browser_messages_carry_each_text_with_its_catalogs_tag_and_no_source(locales) -> None:
    locales(
        {
            "en": {"a.one": "One", "a.two": "Two"},
            "zh-Hant": {"a.one": {"text": "一", "source": "One"}},
        }
    )

    messages = i18n.browser_messages(i18n.translator("zh-TW"), ("a.one", "a.two"))

    assert messages == {
        "a.one": {"tag": "zh-Hant", "text": "一"},
        "a.two": {"tag": "en", "text": "Two"},
    }
