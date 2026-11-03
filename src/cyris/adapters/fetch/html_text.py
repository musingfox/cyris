"""Feed-entry HTML to the body text prompts and language detection read."""

from __future__ import annotations

import html
import logging
import re
from html.parser import HTMLParser

import trafilatura

# trafilatura logs a bare teaser it declines to parse at ERROR, once per entry, though
# `plain_text` handles that case; the run log is read for real errors.
logging.getLogger("trafilatura").setLevel(logging.CRITICAL)

# What an unterminated tag at the end of the input starts with; a bare `<` in prose
# (`x <3`, `a < b`) does not match.
_OPEN_TAG = re.compile(r"<[A-Za-z/!?]")


class _TextCollector(HTMLParser):
    """Collects the text between tags, with character references decoded."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    # A tag stands for a word break: `</p><p>` must not glue two paragraphs into one word.
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        self.parts.append(" ")


def _strip_tags(markup: str) -> str:
    collector = _TextCollector()
    try:
        collector.feed(markup)
    except AssertionError:
        # CPython 3.12 before 3.12.16 asserts on `<![` followed by a non-name; the base
        # image's patch level is not pinned, and one entry must not fail the whole read.
        return " ".join(re.sub(r"<[^>]+>", " ", html.unescape(markup)).split())
    # A feed that cuts its summary mid-tag leaves the tag unterminated in the parser's
    # buffer: drop it rather than let `close` flush it as text.
    if not _OPEN_TAG.match(collector.rawdata):
        collector.close()
    return " ".join("".join(collector.parts).split())


def plain_text(markup: str) -> str:
    """The body text of a feed entry's HTML, for prompts and language detection.

    Both feed adapters call this: everything downstream wants words, and a post that
    opens with a few thousand characters of `<picture srcset>` otherwise fills the whole
    snippet. `RssSource` reads the story image out of the raw HTML first;
    `CloudflareRssSource` gets `image_url` from the Worker and never needs the markup.
    """
    if not markup:
        return ""
    extracted = trafilatura.extract(
        markup, favor_recall=True, include_comments=False, include_tables=True
    )
    if extracted:
        return extracted.strip()
    # trafilatura returns None for a fragment too short or too bare to look like an
    # article (a one-line teaser); an empty content would read as "no body" to the
    # summary prompt, so keep the words the old way instead.
    return _strip_tags(markup)
