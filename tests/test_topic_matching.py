"""Tests for topic matching: keyword prescreen, LLM confirm, assembly, event timeline."""

import json
from datetime import UTC, date, datetime, timedelta

import pytest
from fakes import FakeLLM

from cyris.adapters.store.article_store import StoredArticle
from cyris.adapters.store.event_store import EventStore
from cyris.adapters.store.events import EventFile, TimelineEntry, parse_event
from cyris.domain.models import Article, Tier
from cyris.domain.tracking import TrackedTopic, keyword_prescreen
from cyris.service_layer.topic_matching import (
    TopicMatch,
    assemble_tracked_section,
    confirm_topic_matches,
    record_tracked_events,
)


def _mk_article(url: str, title: str, tags: list[str] | None = None) -> Article:
    return Article(
        id=1,
        title=title,
        url=url,
        content="content",
        published_at=datetime.now(UTC) - timedelta(hours=1),
        source_name="src",
        source_tier=Tier.SUMMARIZE,
        source_tags=tags or [],
    )


def _mk_topic(name: str, keywords: list[str], status: str = "active") -> TrackedTopic:
    return TrackedTopic(
        name=name,
        keywords=keywords,
        created=date(2026, 1, 1),
        status=status,  # type: ignore[arg-type]
    )


def _mk_match(
    url: str,
    topic_name: str,
    note: str,
    title: str = "some article",
    source_name: str = "SomeSource",
    score: float | None = None,
) -> TopicMatch:
    return TopicMatch(
        url=url,
        title=title,
        source_name=source_name,
        topic_name=topic_name,
        note=note,
        score=score,
    )


def _mk_stored(url: str, oid: int, title: str) -> StoredArticle:
    return StoredArticle(
        url=url,
        original_id=oid,
        title=title,
        content="c",
        published_at=datetime.now(UTC) - timedelta(hours=1),
        source_name="s",
        source_tier=Tier.SUMMARIZE,
        first_seen_at=datetime.now(UTC),
    )


def test_keyword_prescreen_hits_title_and_tags():
    """T1: title or tag substring match on active topic keywords -> url maps to topic name."""
    art = _mk_article("u1", "TSMC Arizona fab update", ["tech"])
    topic = _mk_topic("台積電", ["TSMC", "Arizona"])
    res = keyword_prescreen([art], [topic])
    assert res == {"u1": ["台積電"]}


def test_keyword_prescreen_ignores_inactive():
    """T2: inactive topic produces no hits even if text matches."""
    art = _mk_article("u1", "TSMC Arizona fab update", ["tech"])
    topic = _mk_topic("台積電", ["TSMC", "Arizona"], status="inactive")
    res = keyword_prescreen([art], [topic])
    assert res == {}


def test_keyword_prescreen_casefold_substring():
    """T3: keyword match is casefold substring on title."""
    art = _mk_article("u1", "OpenAI DevDay recap")
    topic = _mk_topic("openai", ["openai"])
    res = keyword_prescreen([art], [topic])
    assert "u1" in res


def test_keyword_prescreen_tag_substring():
    """T4: source_tags substring match on keyword."""
    art = _mk_article("u1", "unrelated", ["ai-regulation"])
    topic = _mk_topic("reg", ["regulation"])
    res = keyword_prescreen([art], [topic])
    assert res == {"u1": ["reg"]}


def test_keyword_prescreen_name_as_term_when_no_keywords():
    """T5: if keywords empty, topic name itself is used as term (substring)."""
    art = _mk_article("u1", "Anthropic news")
    topic = _mk_topic("Anthropic", [])
    res = keyword_prescreen([art], [topic])
    assert res == {"u1": ["Anthropic"]}


def test_keyword_prescreen_no_term_no_hit():
    """T6: title and tags have no term -> article absent from result."""
    art = _mk_article("u1", "unrelated title", ["other"])
    topic = _mk_topic("foo", ["bar"])
    res = keyword_prescreen([art], [topic])
    assert res == {}


async def test_topic_match_confirmation_success():
    """T1: 1 candidate + matches json -> TopicMatch list; api_calls==1."""
    cand = _mk_stored("u1", 1, "foo")
    resp = {"matches": [{"id": 1, "topic": "台積電", "note": "亞利桑那廠進入量產"}]}
    llm = FakeLLM([json.dumps(resp)])
    matches, usage = await confirm_topic_matches([cand], {"u1": ["台積電"]}, llm)
    assert len(matches) == 1
    assert matches[0].url == "u1"
    assert matches[0].title == "foo"
    assert matches[0].source_name == "s"
    assert matches[0].topic_name == "台積電"
    assert matches[0].note == "亞利桑那廠進入量產"
    assert matches[0].score is None
    assert usage.api_calls == 1


async def test_topic_match_confirmation_ignores_unknown():
    """T2: unknown id/topic in response -> silently [] ."""
    cand = _mk_stored("u1", 1, "foo")
    resp = {
        "matches": [
            {"id": 99, "topic": "未知", "note": "x"},
            {"id": 1, "topic": "不存在主題", "note": "y"},
        ]
    }
    llm = FakeLLM([json.dumps(resp)])
    matches, _ = await confirm_topic_matches([cand], {"u1": ["台積電"]}, llm)
    assert matches == []


async def test_topic_match_confirmation_batches():
    """T3: 21 candidates -> 2 LLM calls (BATCH_SIZE=20)."""
    cands = [_mk_stored(f"u{i}", i, f"t{i}") for i in range(21)]
    pres = {f"u{i}": ["t"] for i in range(21)}
    llm = FakeLLM([json.dumps({"matches": []}), json.dumps({"matches": []})])
    _, usage = await confirm_topic_matches(cands, pres, llm)
    assert len(llm.calls) == 2


async def test_topic_match_confirmation_bad_json_raises():
    """T4: non-json response -> ValueError."""
    cand = _mk_stored("u1", 1, "foo")
    llm = FakeLLM(["not json"])
    with pytest.raises(ValueError):
        await confirm_topic_matches([cand], {"u1": ["台積電"]}, llm)


def test_tracked_section_assembly_same_topic():
    """T1: 2 matches same topic + refs -> heading single, 2 items, is_tracked, event_ref set."""
    m1 = _mk_match("u1", "台積電", "note1", title="A1", source_name="S1", score=77.0)
    m2 = _mk_match("u2", "台積電", "note2", title="A2", source_name="S2")
    refs = {"台積電": "台積電"}
    sec = assemble_tracked_section([m1, m2], refs)
    assert sec is not None
    assert sec.heading == "台積電"
    assert len(sec.items) == 2
    assert sec.items[0].is_tracked_topic
    assert sec.items[0].event_ref == "台積電"
    # Contract mapping: title=article title, summary=note, sources=[source_name], score=score
    assert sec.items[0].title == "A1"
    assert sec.items[0].summary == "note1"
    assert sec.items[0].sources == ["S1"]
    assert sec.items[0].score == 77.0
    assert sec.items[1].title == "A2"
    assert sec.items[1].summary == "note2"


def test_tracked_section_assembly_multi_topic():
    """T2: matches across topics (first-seen order) -> heading joined by 、 ."""
    m1 = _mk_match("u1", "台積電", "n1")
    m2 = _mk_match("u2", "AI 監管", "n2")
    sec = assemble_tracked_section([m1, m2], {})
    assert sec is not None
    assert sec.heading == "台積電、AI 監管"


def test_tracked_section_assembly_empty():
    """T3: matches=[] -> None ."""
    assert assemble_tracked_section([]) is None


def test_tracked_section_assembly_renders_title_and_note(tmp_path):
    """Integration: TopicMatch -> assemble -> render keeps article title AND note visible."""
    from cyris.adapters.output.digest import DigestWriter
    from cyris.domain.models import DigestContent

    m = _mk_match(
        "https://ex/reg",
        "AI 監管",
        "歐盟法案通過",
        title="EU AI regulation passes",
        source_name="Reuters",
    )
    sec = assemble_tracked_section([m], {"AI 監管": "AI 監管"})
    content = DigestContent(
        date="2026-07-11",
        period="morning",
        sources_processed=1,
        articles_received=1,
        articles_included=1,
        tracked_updates=sec,
    )
    text = DigestWriter(tmp_path).render(content)
    assert (
        "- **[EU AI regulation passes](https://ex/reg)** — 歐盟法案通過 (Reuters) · [[AI 監管]]"
        in text
    )


def test_tracked_section_assembly_no_refs():
    """T4: event_refs={} -> items have event_ref=None ."""
    m = _mk_match("u1", "t", "n")
    sec = assemble_tracked_section([m], {})
    assert sec is not None
    assert sec.items[0].event_ref is None


def test_event_timeline_upsert_new(tmp_path):
    """T1: new topic note url today empty dir -> correct ref, parsed file, timeline/refs."""
    ev_dir = tmp_path / "events"
    store = EventStore(ev_dir)
    today = date(2026, 7, 11)
    m = _mk_match("u1", "AI: Agents", "a")
    refs = record_tracked_events(store, [m], today)
    assert refs == {"AI: Agents": "AI Agents"}
    p = ev_dir / "AI Agents.md"
    assert p.exists()
    ef = parse_event(p.read_text())
    assert ef.status == "active"
    assert ef.created == ef.last_updated == today
    assert len(ef.timeline) == 1 and ef.timeline[0].text == "a"
    assert ef.source_references == ["u1"]


def test_event_timeline_upsert_append(tmp_path):
    """T2: append to existing; preserve created/summary, update last+refs+timeline."""
    ev_dir = tmp_path / "events"
    store = EventStore(ev_dir)
    today = date(2026, 7, 11)
    old = EventFile(
        title="台積電",
        created=date(2026, 7, 1),
        last_updated=date(2026, 7, 1),
        status="active",
        summary="S",
        timeline=[TimelineEntry(entry_date=date(2026, 7, 1), text="old")],
        source_references=["u0"],
    )
    store.save_event(old)
    m = _mk_match("u1", "台積電", "n")
    refs = record_tracked_events(store, [m], today)
    assert refs["台積電"] == "台積電"
    ef = parse_event((ev_dir / "台積電.md").read_text())
    assert len(ef.timeline) == 2
    assert ef.last_updated == today
    assert ef.summary == "S"
    assert ef.created == date(2026, 7, 1)
    assert set(ef.source_references) == {"u0", "u1"}


def test_event_timeline_upsert_revives_inactive(tmp_path):
    """A fresh match on a stale (inactive) event flips status back to active."""
    ev_dir = tmp_path / "events"
    store = EventStore(ev_dir)
    old = EventFile(
        title="台積電",
        created=date(2026, 5, 1),
        last_updated=date(2026, 5, 1),
        status="inactive",
        summary="S",
        timeline=[TimelineEntry(entry_date=date(2026, 5, 1), text="old")],
    )
    store.save_event(old)
    record_tracked_events(store, [_mk_match("u1", "台積電", "n")], date(2026, 7, 11))
    ef = parse_event((ev_dir / "台積電.md").read_text())
    assert ef.status == "active"


def test_event_timeline_upsert_join_same_day(tmp_path):
    """T3: 2 notes same topic one round -> joined entry text 'a; b'."""
    ev_dir = tmp_path / "events"
    store = EventStore(ev_dir)
    today = date(2026, 7, 11)
    ms = [_mk_match("u1", "t", "a"), _mk_match("u2", "t", "b")]
    record_tracked_events(store, ms, today)
    ef = parse_event((ev_dir / "t.md").read_text())
    assert ef.timeline[0].text == "a; b"


def test_event_timeline_upsert_bad_format_overwrite(tmp_path):
    """T4: bad .md for name -> treat absent, overwrite with valid parseable."""
    ev_dir = tmp_path / "events"
    ev_dir.mkdir()
    bad_path = ev_dir / "台積電.md"
    bad_path.write_text("garbage not frontmatter\n")
    store = EventStore(ev_dir)
    today = date(2026, 7, 11)
    m = _mk_match("u1", "台積電", "new")
    record_tracked_events(store, [m], today)
    ef = parse_event(bad_path.read_text())
    assert ef.timeline[0].text == "new"
