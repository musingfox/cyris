"""Topic matching: keyword prescreen, LLM confirm (batch), section assembly, event timeline upsert.

Prescreen (domain) -> confirm -> assemble tracked + record events (per contracts).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from cyris.domain.events import EventFile, TimelineEntry
from cyris.domain.models import DigestItem, DigestSection, StoredArticle, UsageStats
from cyris.service_layer.ports import LLMClient, complete_json

if TYPE_CHECKING:
    # EventStore is a single-impl store injected at runtime; imported only for type hints
    # so the service layer keeps no runtime dependency on adapters/.
    from cyris.adapters.store.event_store import EventStore
from cyris.service_layer.prompts import (
    DEFAULT_LANGUAGE,
    build_topic_confirm_prompt,
    build_topic_confirm_system_prompt,
)

logger = logging.getLogger(__name__)

BATCH_SIZE = 20


class TopicMatch(BaseModel):
    """Confirmed hit: one article to one tracked topic with a note."""

    url: str
    title: str
    source_name: str
    topic_name: str
    note: str
    score: float | None = None
    ref_urls: list[str] = Field(default_factory=list)


async def confirm_topic_matches(
    candidates: list[StoredArticle],
    prescreen_hits: dict[str, list[str]],
    llm: LLMClient,
    output_language: str = DEFAULT_LANGUAGE,
    style_prompt: str = "",
) -> tuple[list[TopicMatch], UsageStats]:
    """LLM batch semantic confirm of prescreen hits.

    Uses original_id for prompt/response matching. Silently drops unknown ids/topics.
    Batches at BATCH_SIZE. Raises ValueError on unparsable LLM response.
    """
    usage = UsageStats(model=llm.model)
    if not candidates or not prescreen_hits:
        return [], usage

    relevant = [c for c in candidates if c.url in prescreen_hits]
    if not relevant:
        return [], usage

    all_matches: list[TopicMatch] = []
    for i in range(0, len(relevant), BATCH_SIZE):
        batch = relevant[i : i + BATCH_SIZE]
        topics = sorted({t for ts in prescreen_hits.values() for t in ts})
        user_prompt = build_topic_confirm_prompt(topics, batch)
        try:
            data = await complete_json(
                llm,
                user_prompt,
                system=build_topic_confirm_system_prompt(output_language, style_prompt),
                usage=usage,
            )
        except Exception as exc:
            raise ValueError(f"topic confirm failed to parse JSON: {exc}") from exc

        id_to_cand: dict[int | str, StoredArticle] = {c.original_id: c for c in batch}
        for entry in data.get("matches", []):
            oid = entry.get("id")
            tname = entry.get("topic")
            note = (entry.get("note") or "").strip()
            cand = id_to_cand.get(oid)
            if cand and tname and tname in prescreen_hits.get(cand.url, []):
                all_matches.append(
                    TopicMatch(
                        url=cand.url,
                        title=cand.title,
                        source_name=cand.source_name,
                        topic_name=tname,
                        note=note,
                        score=cand.score,
                        ref_urls=cand.ref_urls,
                    )
                )
    return all_matches, usage


def assemble_tracked_section(
    matches: list[TopicMatch], event_refs: dict[str, str] | None = None
) -> DigestSection | None:
    """Group matches into one DigestSection for tracked_updates.

    Heading joins distinct topics in appearance order with '、'.
    Each item gets is_tracked_topic=True and optional event_ref.
    Empty matches -> None.
    """
    if not matches:
        return None
    event_refs = event_refs or {}
    topic_order: list[str] = []
    seen: set[str] = set()
    for m in matches:
        if m.topic_name not in seen:
            seen.add(m.topic_name)
            topic_order.append(m.topic_name)
    heading = "、".join(topic_order)
    items: list[DigestItem] = []
    for m in matches:
        ref = event_refs.get(m.topic_name)
        items.append(
            DigestItem(
                title=m.title,
                summary=m.note,
                sources=[m.source_name] if m.source_name else [],
                urls=[m.url],
                ref_urls=m.ref_urls,
                event_ref=ref,
                is_tracked_topic=True,
                score=m.score,
            )
        )
    return DigestSection(heading=heading, items=items)


def _upsert_one_event(
    event_store: EventStore,
    topic: str,
    joined_note: str,
    first_url: str,
    today: date,
) -> str:
    """Load or init EventFile for topic, append/join today's entry, save, return stem as ref."""
    events = event_store.load_events()
    existing = next((e for e in events if e.title == topic), None)
    if existing is None:
        ef = EventFile(
            title=topic,
            created=today,
            last_updated=today,
            status="active",
            timeline=[TimelineEntry(entry_date=today, text=joined_note)],
            source_references=[first_url] if first_url else [],
        )
    else:
        # preserve created, summary; append refs; one entry per day per topic (join with ;)
        if existing.timeline and existing.timeline[-1].entry_date == today:
            prev_text = existing.timeline[-1].text
            new_text = f"{prev_text}; {joined_note}" if prev_text else joined_note
            existing.timeline[-1] = TimelineEntry(entry_date=today, text=new_text)
        else:
            existing.timeline.append(TimelineEntry(entry_date=today, text=joined_note))
        existing.last_updated = today
        existing.status = "active"  # a fresh match revives a stale (inactive) event
        if first_url and first_url not in existing.source_references:
            existing.source_references.append(first_url)
        ef = existing
    path = event_store.save_event(ef)
    return path.stem


def record_tracked_events(
    event_store: EventStore | None,
    matches: list[TopicMatch],
    today: date,
) -> dict[str, str]:
    """Upsert one timeline entry per unique topic (notes joined '; '); return {topic: ref}.

    If no event_store or no matches: return topic->None .
    Bad existing files treated as absent (load skips) -> overwrite on save.
    """
    if not event_store or not matches:
        return {m.topic_name: None for m in matches}

    notes_by: dict[str, list[str]] = defaultdict(list)
    urls_by: dict[str, list[str]] = defaultdict(list)
    for m in matches:
        notes_by[m.topic_name].append(m.note)
        urls_by[m.topic_name].append(m.url)

    refs: dict[str, str] = {}
    for topic in notes_by:
        joined = "; ".join(notes_by[topic])
        first_url = urls_by[topic][0] if urls_by[topic] else ""
        ref = _upsert_one_event(event_store, topic, joined, first_url, today)
        refs[topic] = ref
    return refs
