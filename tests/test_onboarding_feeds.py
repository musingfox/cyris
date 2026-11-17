"""The onboarding feed catalog: every entry reads as a `SourceConfig`.

docs/milestones/cold-start-onboarding.md sets the shape this file holds the catalog to:
a `SourceConfig` per entry, `lane: skim`, at least one tag each, and both a 繁中 and an
English entry so onboarding has something to offer either reader. The list's own `topics`
table groups the entries: each entry names one or two of its ids, each topic holds at least
one entry, and `topics` is the list's field, never a `SourceConfig`'s. Whether a URL still
answers is `scripts/validate_feed_catalog.py`'s question, not this file's — a reviewer-run
gate, not a test, because it asks about the network rather than the code
(`tests/CLAUDE.md` R3).
"""

import json
import re
from pathlib import Path

import pytest

from cyris.domain.models import Lane, SourceConfig

pytestmark = [pytest.mark.unit, pytest.mark.guard]

CATALOG_PATH = Path(__file__).resolve().parents[1] / "src" / "cyris" / "onboarding_feeds.json"
ENGLISH_PATH = CATALOG_PATH.parent / "locales" / "en.json"


def _raw_feeds() -> list[dict]:
    return json.loads(CATALOG_PATH.read_text())["feeds"]


def _topics() -> list[dict]:
    return json.loads(CATALOG_PATH.read_text())["topics"]


def test_every_entry_reads_as_a_source_config() -> None:
    feeds = _raw_feeds()
    assert feeds, "catalog is empty"
    for entry in feeds:
        source = SourceConfig.model_validate(entry)
        assert source.fetchable, f"{source.name}: not fetchable (needs a url)"


def test_every_entry_is_in_the_skim_lane() -> None:
    for entry in _raw_feeds():
        source = SourceConfig.model_validate(entry)
        assert source.lane == Lane.SKIM, f"{source.name}: lane {source.lane}, want skim"


def test_every_entry_carries_at_least_one_tag() -> None:
    for entry in _raw_feeds():
        source = SourceConfig.model_validate(entry)
        assert source.tags, f"{source.name}: no tags"


def test_every_entry_names_a_language() -> None:
    for entry in _raw_feeds():
        source = SourceConfig.model_validate(entry)
        assert source.language in ("zh", "en"), f"{source.name}: language {source.language!r}"


def test_catalog_has_both_zh_and_en_entries() -> None:
    sources = [SourceConfig.model_validate(entry) for entry in _raw_feeds()]
    languages = {s.language for s in sources}
    assert "zh" in languages
    assert "en" in languages


def test_no_area_is_pre_assigned() -> None:
    """area_id points at a reader's own `areas` row; the catalog cannot know it in advance."""
    for entry in _raw_feeds():
        source = SourceConfig.model_validate(entry)
        assert source.area_id is None, f"{source.name}: area_id set"


def test_names_are_unique() -> None:
    names = [SourceConfig.model_validate(entry).name for entry in _raw_feeds()]
    assert len(names) == len(set(names)), "duplicate source name"


def test_every_topic_has_a_lowercase_ascii_id_and_an_english_label() -> None:
    """The file holds the id alone: the label is the UI catalogs' (ADR-0027)."""
    english = json.loads(ENGLISH_PATH.read_text(encoding="utf-8"))
    topics = _topics()
    assert topics, "no topic table"
    for topic in topics:
        assert set(topic) == {"id"}, f"{topic}: fields"
        assert re.fullmatch(r"[a-z]+", topic["id"]), f"{topic['id']!r}: not lowercase ascii"
        label = english.get(f"onboarding_topic.{topic['id']}", "")
        assert label.strip(), f"{topic['id']}: no label"
    ids = [topic["id"] for topic in topics]
    assert len(ids) == len(set(ids)), "duplicate topic id"


def test_every_entry_names_one_or_two_topics_from_the_table() -> None:
    known = {topic["id"] for topic in _topics()}
    for entry in _raw_feeds():
        topics = entry.get("topics", [])
        assert 1 <= len(topics) <= 2, f"{entry['name']}: {len(topics)} topics"
        assert len(topics) == len(set(topics)), f"{entry['name']}: a topic named twice"
        assert set(topics) <= known, f"{entry['name']}: unknown topic {set(topics) - known}"


def test_every_topic_holds_at_least_one_entry() -> None:
    held = {topic for entry in _raw_feeds() for topic in entry.get("topics", [])}
    empty = [topic["id"] for topic in _topics() if topic["id"] not in held]
    assert empty == [], f"topics with no feed: {empty}"


def test_topics_are_the_lists_own_field_not_a_source_configs() -> None:
    """A stored source must not carry the list's grouping: the page strips it before posting."""
    assert "topics" not in SourceConfig.model_fields
    for entry in _raw_feeds():
        stored = SourceConfig.model_validate(entry).model_dump(mode="json")
        assert "topics" not in stored, f"{entry['name']}: topics reached the source"
