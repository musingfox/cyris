"""Tests for EventStore load/save per contracts."""

from datetime import date
from pathlib import Path

import pytest

from cyris.adapters.store.event_store import EventStore
from cyris.adapters.store.events import EventFile, TimelineEntry, parse_event, render_event


def _make_sample(title: str = "TSMC Arizona Fab Phase 2") -> EventFile:
    return EventFile(
        title=title,
        created=date(2026, 1, 15),
        last_updated=date(2026, 3, 16),
        tags=["semiconductor", "tsmc"],
        status="active",
        summary="Current state.",
        timeline=[
            TimelineEntry(entry_date=date(2026, 3, 16), text="Update"),
            TimelineEntry(entry_date=date(2026, 1, 15), text="Initial"),
        ],
        key_entities=["TSMC", "Arizona"],
        source_references=["ref1"],
    )


class TestEventStoreLoad:
    def test_t1_load_two_legal_skip_gitkeep_sorted(self, tmp_path: Path):
        d = tmp_path / "events"
        d.mkdir()
        (d / ".gitkeep").write_text("")
        e1 = _make_sample("Alpha Event")
        e2 = _make_sample("Zeta Event")
        (d / "Alpha Event.md").write_text(render_event(e1))
        (d / "Zeta Event.md").write_text(render_event(e2))
        store = EventStore(d)
        events = store.load_events()
        assert len(events) == 2
        assert events[0].title == "Alpha Event"
        assert events[1].title == "Zeta Event"

    def test_t2_missing_dir_returns_empty(self, tmp_path: Path):
        store = EventStore(tmp_path / "no-such-dir")
        assert store.load_events() == []

    def test_t3_one_good_one_bad_warns_skips_bad(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        d = tmp_path / "events"
        d.mkdir()
        good = _make_sample("Good One")
        (d / "Good One.md").write_text(render_event(good))
        bad = d / "Bad.md"
        bad.write_text("# no frontmatter at all\njust body")
        store = EventStore(d)
        with caplog.at_level("WARNING"):
            events = store.load_events()
        assert len(events) == 1
        assert events[0].title == "Good One"
        assert "Bad.md" in caplog.text
        assert "WARNING" in caplog.text or any(
            "warn" in r.levelname.lower() for r in caplog.records
        )


class TestEventStoreSave:
    def test_t1_save_chinese_title_roundtrips(self, tmp_path: Path):
        d = tmp_path / "events"
        store = EventStore(d)
        ef = _make_sample("台積電亞利桑那廠")
        path = store.save_event(ef)
        assert path.exists()
        assert path.name == "台積電亞利桑那廠.md"
        loaded = parse_event(path.read_text(encoding="utf-8"))
        assert loaded == ef

    def test_t2_sanitize_illegal_chars(self, tmp_path: Path):
        d = tmp_path / "events"
        store = EventStore(d)
        ef = _make_sample("Foo: Bar/Baz?")
        path = store.save_event(ef)
        assert path.name == "Foo BarBaz.md"

    def test_t3_overwrite_updates(self, tmp_path: Path):
        d = tmp_path / "events"
        store = EventStore(d)
        ef = _make_sample("Same Title")
        store.save_event(ef)
        ef2 = ef.model_copy(update={"summary": "updated summary"})
        store.save_event(ef2)
        files = list(d.glob("*.md"))
        assert len(files) == 1
        loaded = parse_event(files[0].read_text(encoding="utf-8"))
        assert loaded.summary == "updated summary"

    def test_t4_creates_nested_parents(self, tmp_path: Path):
        d = tmp_path / "a" / "b" / "events"
        store = EventStore(d)
        ef = _make_sample("Nested")
        p = store.save_event(ef)
        assert p.exists()
        assert d.exists()


class TestEventStoreLifecycle:
    def _store_with(self, tmp_path: Path, last_updated: date, status: str = "active") -> EventStore:
        d = tmp_path / "events"
        store = EventStore(d)
        ef = _make_sample("E").model_copy(update={"last_updated": last_updated, "status": status})
        store.save_event(ef)
        return store

    def test_stale_31_days_marked_inactive(self, tmp_path: Path):
        store = self._store_with(tmp_path, date(2026, 1, 1))
        assert store.mark_stale_inactive(date(2026, 2, 1)) == ["E"]  # 31 days
        assert store.load_events()[0].status == "inactive"

    def test_boundary_exactly_30_days_marked_inactive(self, tmp_path: Path):
        store = self._store_with(tmp_path, date(2026, 1, 1))
        assert store.mark_stale_inactive(date(2026, 1, 31)) == ["E"]  # 30 days = 30+

    def test_fresh_29_days_untouched(self, tmp_path: Path):
        store = self._store_with(tmp_path, date(2026, 1, 1))
        assert store.mark_stale_inactive(date(2026, 1, 30)) == []  # 29 days
        assert store.load_events()[0].status == "active"

    def test_already_inactive_not_re_saved(self, tmp_path: Path):
        store = self._store_with(tmp_path, date(2026, 1, 1), status="inactive")
        assert store.mark_stale_inactive(date(2026, 3, 1)) == []
