"""Use case: list this run's candidates that sit close to a topic the reader tracks.

No LLM is called. Each topic's description is embedded once, every candidate
title at most once, and a title vote similarity already embedded this run is
reused rather than paid for again.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from cyris.domain.models import DigestItem, DigestSection, StoredArticle, TrackedTopic
from cyris.domain.tracking import hits
from cyris.service_layer.ports import Embedder

logger = logging.getLogger(__name__)


@dataclass
class TrackingReport:
    # One per topic with a hit, in the topics' order; its items nearest first.
    sections: list[DigestSection] = field(default_factory=list)
    # Hit count by topic name, for every topic judged, zero included.
    hits: dict[str, int] = field(default_factory=dict)
    # Why a topic was not judged, by topic name.
    skipped: dict[str, str] = field(default_factory=dict)


def _item(article: StoredArticle) -> DigestItem:
    return DigestItem(
        title=article.title,
        summary="",
        sources=[article.source_name],
        urls=[article.url],
        ref_urls=article.ref_urls,
    )


async def track_topics(
    embedder: Embedder,
    model: str,
    topics: list[TrackedTopic],
    candidates: list[StoredArticle],
    *,
    known: dict[str, list[float]] | None = None,
) -> TrackingReport:
    """Judge `candidates` against every topic whose threshold was set for `model`.

    Degrades to skipped topics rather than raising: a digest must still go out
    if the embedding API is down.

    Args:
        embedder: the run's embedder, which embeds with `model`.
        model: the embedding model this run uses.
        topics: the reader's tracked topics.
        candidates: the articles this run digests.
        known: url -> vector for titles this run already embedded with `model`.
    """
    report = TrackingReport()
    active = []
    for topic in topics:
        if topic.model == model:
            active.append(topic)
        else:
            report.skipped[topic.name] = (
                f"threshold set for {topic.model}; this run embeds with {model}"
            )
    if not active:
        return report

    known = known or {}
    titles = list(dict.fromkeys(a.title for a in candidates if not known.get(a.url)))
    texts = [t.description for t in active] + titles
    try:
        vectors = await embedder.embed(texts)
        # Each vector is paired back to its text by position below.
        if len(vectors) != len(texts):
            raise ValueError("the embedder answered a different number of vectors than texts")
    except Exception as e:
        logger.warning("Topic tracking unavailable, digest continues without it: %s", e)
        report.skipped.update({t.name: f"embedding failed: {e}" for t in active})
        return report

    seeds = vectors[: len(active)]
    by_title = dict(zip(titles, vectors[len(active) :], strict=True))
    by_url = {a.url: known.get(a.url) or by_title[a.title] for a in candidates}
    articles = {a.url: a for a in candidates}
    for topic, seed in zip(active, seeds, strict=True):
        if not seed:
            report.skipped[topic.name] = "its description embedded to no vector"
            continue
        found = hits(seed, by_url, topic.threshold)
        report.hits[topic.name] = len(found)
        if found:
            report.sections.append(
                DigestSection(heading=topic.name, items=[_item(articles[url]) for url, _ in found])
            )
    logger.info(
        "Topic tracking: %d candidate(s) against %d topic(s), %d hit(s)",
        len(by_url),
        len(active),
        sum(report.hits.values()),
    )
    return report
