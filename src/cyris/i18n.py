"""The UI's wording: MessageFormat 2 catalogs in the language a page is worded in (ADR-0027).

`src/cyris/locales/<tag>.json` is one catalog per language. `en.json` maps each key to an MF2
message; every other catalog maps a key to `{"text", "source"}`, `source` being the English
message the text was translated from. A requested tag reaches a catalog by CLDR language
matching, one tag at a time, and anything that matches none is worded in English.
"""

import functools
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import langcodes
from babel import Locale
from babel.dates import format_datetime, format_skeleton
from langcodes.tag_parser import LanguageTagError
from markupsafe import Markup, escape
from messageformat2 import Message

LOCALES = Path(__file__).parent / "locales"
# The fallback of every lookup, and the catalog each other catalog's `source` repeats.
ENGLISH = "en"
# An issue is named by the hour its run started, never by its period, since a day may hold any
# number of issues: `11/17 0800` in every catalog, the hour rounded down (owner, 2026-11-17). The
# day is a CLDR skeleton in the catalog's locale; the hour an LDML pattern with a literal `00`.
ISSUE_DAY = "Md"
ISSUE_HOUR = "HH'00'"

# What a caller supplies for a message's markup: each markup name's element and attributes,
# as `{"link": ("a", {"href": url})}` for `{#link}…{/link}`. The catalog never holds HTML.
Elements = Mapping[str, tuple[str, Mapping[str, str]]]
# Markup stands in the formatted text as private-use characters, which escaping leaves alone.
# Only markup may write them: they are removed from the catalog text and from every value.
_PLACEHOLDER = re.compile("\ue000(open|close|standalone):([^\ue001]*)\ue001")
_DELIMITERS = str.maketrans("", "", "\ue000\ue001")


@dataclass(frozen=True)
class Catalog:
    """One language's messages, by key, with the Babel locale they are formatted in."""

    tag: str
    locale: Locale
    texts: Mapping[str, str]


def _texts(tag: str, entries: dict) -> dict[str, str]:
    if tag == ENGLISH:
        return entries
    return {key: entry["text"] for key, entry in entries.items()}


@functools.cache
def catalogs() -> dict[str, Catalog]:
    """Every shipped catalog by its tag, read once per process."""
    found = {}
    for path in sorted(LOCALES.glob("*.json")):
        tag = path.stem
        entries = json.loads(path.read_text(encoding="utf-8"))
        found[tag] = Catalog(tag, Locale.parse(tag, sep="-"), _texts(tag, entries))
    return found


def match(tag: str) -> str | None:
    """The catalog `tag` reaches by CLDR language matching, or None.

    A value that is not a well-formed tag, a legacy plain language name among them, matches none.
    """
    try:
        return langcodes.closest_supported_match(tag, list(catalogs()))
    except LanguageTagError:
        return None


def resolve(language: str) -> str:
    """The tag of the catalog a page asked for in `language` is worded from."""
    return match(language) or ENGLISH


def _weight(params: list[str]) -> float:
    for param in params:
        name, _, value = param.strip().partition("=")
        if name.strip().lower() == "q":
            try:
                weight = float(value)
            except ValueError:
                return 0.0
            return weight if 0.0 <= weight <= 1.0 else 0.0
    return 1.0


def accept_language(header: str) -> str | None:
    """The first catalog an `Accept-Language` header's tags reach, by `q`; None when none does.

    RFC 9110 §12.5.4: highest `q` first, equal weights in the order sent; `*` and `q=0` are not
    a request for any one language.
    """
    weighted = []
    for part in header.split(","):
        tag, *params = part.strip().split(";")
        tag = tag.strip()
        weight = _weight(params)
        if tag and tag != "*" and weight > 0:
            weighted.append((weight, tag))
    for _, tag in sorted(weighted, key=lambda pair: -pair[0]):
        if (found := match(tag)) is not None:
            return found
    return None


def _placeholder(kind: str, name: str, options: Mapping[str, object]) -> str:
    return f"\ue000{kind}:{name}\ue001"


def _element(elements: Elements, kind: str, name: str) -> str:
    tag, attributes = elements[name]
    if kind == "close":
        return f"</{tag}>"
    return f"<{tag}{''.join(f' {key}="{escape(value)}"' for key, value in attributes.items())}>"


@functools.cache
def _message(tag: str, key: str) -> Message | None:
    text = catalogs()[tag].texts.get(key)
    return None if text is None else Message(text.translate(_DELIMITERS))


class Translator:
    """`t(key, **variables)`: one catalog's message, else English's, formatted in its own locale.

    A key English lacks is a programming error and raises KeyError; a variable the message
    names and the caller does not pass raises the formatter's error.

    With `elements`, the message is HTML: its text escaped, each markup span drawn as the
    element `elements` names for it. A markup name `elements` lacks raises KeyError.
    """

    def __init__(self, tag: str) -> None:
        self.tag = tag

    def __call__(
        self, key: str, /, *, elements: Elements | None = None, **variables: object
    ) -> str:
        source = self.origin(key)
        message, locale = _message(source, key), catalogs()[source].locale
        if elements is None:
            return message.format(variables, locale, bidi_isolation=False)
        inert = {
            name: value.translate(_DELIMITERS) if isinstance(value, str) else value
            for name, value in variables.items()
        }
        text = message.format(inert, locale, markup=_placeholder, bidi_isolation=False)
        return Markup(
            _PLACEHOLDER.sub(lambda span: _element(elements, span[1], span[2]), str(escape(text)))
        )

    def origin(self, key: str) -> str:
        """The tag of the catalog `key` is worded from: this one, or English's fallback."""
        if key not in catalogs()[ENGLISH].texts:
            raise KeyError(f"{key!r} is not in {ENGLISH}.json")
        return self.tag if _message(self.tag, key) is not None else ENGLISH


@functools.cache
def _translator(tag: str) -> Translator:
    return Translator(tag)


def translator(language: str) -> Translator:
    """The `t` for a page asked for in `language`, one per catalog rather than per raw tag."""
    return _translator(resolve(language))


def browser_messages(t: Translator, keys: tuple[str, ...]) -> dict[str, dict[str, str]]:
    """The messages a browser script formats: each key's MF2 text and its catalog's tag.

    No `source`: the browser formats, it never checks a translation.
    """
    messages = {}
    for key in keys:
        tag = t.origin(key)
        messages[key] = {"tag": tag, "text": catalogs()[tag].texts[key]}
    return messages


@dataclass(frozen=True)
class BrowserFile:
    """One catalog's browser messages as served: the JSON body and its strong ETag."""

    body: bytes
    etag: str


@functools.cache
def browser_file(tag: str, keys: tuple[str, ...]) -> BrowserFile:
    """The browser messages for catalog `tag`, English filling each key it lacks.

    One file per catalog, so the browser keeps one copy for every page and revalidates it.
    The ETag is the body's hash: it changes exactly when a message or its tag does.
    """
    messages = browser_messages(_translator(tag), keys)
    body = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    encoded = body.encode()
    return BrowserFile(encoded, f'"{hashlib.sha256(encoded).hexdigest()[:32]}"')


def _locale(t: Translator) -> Locale:
    return catalogs()[t.tag].locale


def issue_day(t: Translator, day: str, started_at: datetime | None) -> str:
    """The day an issue is named by: its run's start, else its stored date `day`, or `day` as it is.

    `started_at` carries the offset of `general.timezone` when the run started, and is shown in it.
    """
    if started_at is not None:
        return format_skeleton(ISSUE_DAY, started_at, tzinfo=started_at.tzinfo, locale=_locale(t))
    try:
        stored = date.fromisoformat(day)
    except ValueError:
        return day
    return format_skeleton(ISSUE_DAY, stored, locale=_locale(t))


def issue_hour(t: Translator, started_at: datetime | None) -> str:
    """The hour an issue's run started, `0800` for 08:05, or "" for an issue stored without it."""
    if started_at is None:
        return ""
    return format_datetime(started_at, ISSUE_HOUR, tzinfo=started_at.tzinfo, locale=_locale(t))


def issue_name(t: Translator, day: str, started_at: datetime | None) -> str:
    """An issue's name: its day, a space and its hour, `11/17 0800`, else its day alone."""
    named = issue_day(t, day, started_at)
    if started_at is None:
        return named
    return f"{named} {issue_hour(t, started_at)}"
