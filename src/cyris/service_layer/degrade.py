"""Degraded-mode fallbacks for when the LLM is unavailable.

The LLM (no key configured, auth error, quota exhausted, network failure) is the
digest's ideal judge, but it must never take the whole run down. fetch + store
always complete; LLM steps fall back to plain excerpts here instead of crashing.
Future embedding / RAG judgement can slot in as a smarter fallback than excerpts.
"""

import html
import json
import logging
import re
from collections import defaultdict
from functools import cache
from importlib.resources import files

from cyris.domain.models import Article, DigestItem, DigestSection

logger = logging.getLogger(__name__)

# A dateline is a wire, a place, a date and at most a reporter's name, so a bracket
# that runs past this before its closing word is an aside, which stays. The day of the
# month is what tells it from a short bracket that merely ends in 電, such as （台電）.
DATELINE_MAX_CHARS = 40


@cache
def _dateline_re() -> re.Pattern[str]:
    raw = (files(__package__) / "datelines.json").read_text(encoding="utf-8")
    endings = "|".join(re.escape(word) for word in json.loads(raw)["endings"])
    body = rf"(?=[^（）()]{{0,{DATELINE_MAX_CHARS}}}[）)])[^（）()]*?\d{{1,2}}日[^（）()]*?"
    return re.compile(rf"^[（(]{body}(?:{endings})[）)]\s*")


def strip_dateline(text: str) -> str:
    """The text less a wire dateline in brackets at its very start, such as （中央社…電）."""
    return _dateline_re().sub("", text, count=1)


def excerpt(content: str, length: int = 200, *, drop_dateline: bool = False) -> str:
    """Plain-text excerpt from the start of the content — the no-LLM stand-in for a summary.

    Feed content is raw HTML; strip tags and decode entities so fallbacks
    never leak markup into the digest. `drop_dateline` strips a leading wire
    dateline from the plain text before it is cut, so it costs none of `length`.
    """
    text = re.sub(r"<[^>]+>", " ", content or "")
    text = " ".join(html.unescape(text).split())
    if drop_dateline:
        text = strip_dateline(text)
    if len(text) <= length:
        return text
    return text[:length].rstrip() + "…"


def _item(a: Article, scores: dict[str, float] | None) -> DigestItem:
    return DigestItem(
        title=a.title,
        summary=excerpt(a.content),
        sources=[a.source_name],
        urls=[a.url] if a.url else [],
        score=(scores.get(a.url) if scores and a.url else None),
        ref_urls=a.ref_urls,
        passed_through=True,
    )


def headlines_from_articles(
    articles: list[Article], scores: dict[str, float] | None = None
) -> list[DigestItem]:
    """Skim-lane fallback: keep every article as a headline with a plain excerpt.

    Without the LLM we cannot judge what to discard, so nothing is dropped here;
    the final per-digest cap in domain.selection still bounds the output size.
    """
    return [_item(a, scores) for a in articles]


def excerpt_sections_from_articles(
    articles: list[Article], scores: dict[str, float] | None = None
) -> list[DigestSection]:
    """Read-lane fallback: group by first tag, emit a plain excerpt per article."""
    groups: dict[str, list[Article]] = defaultdict(list)
    for a in articles:
        tag = a.source_tags[0] if a.source_tags else "general"
        groups[tag].append(a)
    return [
        DigestSection(heading=tag, items=[_item(a, scores) for a in group])
        for tag, group in groups.items()
    ]
