"""Tracked interest topics: pure domain model."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cyris.domain.models import Article


class TrackedTopic(BaseModel):
    """A user-tracked topic of interest for proactive monitoring."""

    model_config = ConfigDict(extra="forbid")

    name: str
    keywords: list[str] = Field(default_factory=list)
    created: date
    status: Literal["active", "inactive"] = "active"


def keyword_prescreen(articles: list[Article], topics: list[TrackedTopic]) -> dict[str, list[str]]:
    """Pure domain: return {url: [topic_names]} for articles whose title or tags
    contain (casefold substring) any active topic's name or its keywords.
    """
    if not articles or not topics:
        return {}

    # build active terms: topic_name -> list of lower terms (name + kws)
    topic_terms: dict[str, list[str]] = {}
    for t in topics:
        if t.status != "active":
            continue
        terms = [t.name.lower()]
        terms.extend(k.lower() for k in t.keywords)
        topic_terms[t.name] = list(dict.fromkeys(terms))

    hits: dict[str, list[str]] = {}
    for a in articles:
        title_l = a.title.lower()
        tags_l = [t.lower() for t in a.source_tags]
        matched: list[str] = []
        for tname, terms in topic_terms.items():
            for term in terms:
                if term in title_l or any(term in tag for tag in tags_l):
                    matched.append(tname)
                    break
        if matched:
            hits[a.url] = matched
    return hits
