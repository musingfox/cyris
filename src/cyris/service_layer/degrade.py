"""Degraded-mode fallbacks for when the LLM is unavailable.

The LLM (no key configured, auth error, quota exhausted, network failure) is the
digest's ideal judge, but it must never take the whole run down. fetch + store
always complete; LLM steps fall back to plain excerpts here instead of crashing.
Future embedding / RAG judgement can slot in as a smarter fallback than excerpts.
"""

import html
import logging
import re
from collections import defaultdict

from cyris.domain.models import Article, DigestItem, DigestSection

logger = logging.getLogger(__name__)


def excerpt(content: str, length: int = 200) -> str:
    """Plain-text excerpt from the start of the content — the no-LLM stand-in for a summary.

    Feed content is raw HTML; strip tags and decode entities so fallbacks
    never leak markup into the digest.
    """
    text = re.sub(r"<[^>]+>", " ", content or "")
    text = " ".join(html.unescape(text).split())
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
    )


def headlines_from_articles(
    articles: list[Article], scores: dict[str, float] | None = None
) -> list[DigestItem]:
    """Filter-tier fallback: keep every article as a headline with a plain excerpt.

    Without the LLM we cannot judge what to discard, so nothing is dropped here;
    the final per-digest cap in domain.selection still bounds the output size.
    """
    return [_item(a, scores) for a in articles]


def excerpt_sections_from_articles(
    articles: list[Article], scores: dict[str, float] | None = None
) -> list[DigestSection]:
    """Summarize-tier fallback: group by first tag, emit a plain excerpt per article."""
    groups: dict[str, list[Article]] = defaultdict(list)
    for a in articles:
        tag = a.source_tags[0] if a.source_tags else "general"
        groups[tag].append(a)
    return [
        DigestSection(heading=tag, items=[_item(a, scores) for a in group])
        for tag, group in groups.items()
    ]
