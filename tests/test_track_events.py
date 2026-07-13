"""Tests for event markdown parse/render per PRD schema."""

from datetime import date

import pytest
from pydantic import ValidationError

from cyris.adapters.store.events import EventFile, TimelineEntry, parse_event, render_event

PRD_EXAMPLE = """---
title: TSMC Arizona Fab Phase 2
created: 2026-01-15
last_updated: 2026-03-16
tags: [semiconductor, geopolitics, tsmc, us-china]
status: active
---

## Summary
One-paragraph current state of this event.

## Timeline
- **2026-03-16**: Production timeline delayed by 6 months due to...
- **2026-02-20**: Workforce training partnership announced with...
- **2026-01-15**: TSMC confirms Phase 2 expansion at Arizona site...

## Key Entities
- TSMC, Arizona, US CHIPS Act, Intel

## Source References
- 2026-03-16-am: TechCrunch, Nikkei Asia
- 2026-01-15-pm: Reuters, Stratechery
"""


class TestEventMarkdownParse:
    def test_t1_full_prd_example(self):
        ef = parse_event(PRD_EXAMPLE)
        assert ef.title == "TSMC Arizona Fab Phase 2"
        assert ef.created == date(2026, 1, 15)
        assert ef.last_updated == date(2026, 3, 16)
        assert ef.tags == ["semiconductor", "geopolitics", "tsmc", "us-china"]
        assert ef.status == "active"
        assert len(ef.timeline) == 3
        assert ef.timeline[0].entry_date == date(2026, 3, 16)
        assert ef.key_entities == ["TSMC", "Arizona", "US CHIPS Act", "Intel"]
        assert ef.source_references == [
            "2026-03-16-am: TechCrunch, Nikkei Asia",
            "2026-01-15-pm: Reuters, Stratechery",
        ]

    def test_t2_chinese_title(self):
        md = """---
title: 台積電亞利桑那廠
created: 2026-01-15
last_updated: 2026-03-16
tags: []
status: active
---

## Summary
中文摘要。

## Timeline
- **2026-03-16**: 中文時間線

## Key Entities
- 台積電

## Source References
- none
"""
        ef = parse_event(md)
        assert ef.title == "台積電亞利桑那廠"
        assert "中文時間線" in ef.timeline[0].text

    def test_t3_no_frontmatter_raises(self):
        with pytest.raises(ValueError):
            parse_event("# Just markdown\nno frontmatter")

    def test_t4_extra_notes_section_raises(self):
        md = PRD_EXAMPLE.replace(
            "## Source References",
            "## Notes\nExtra\n\n## Source References",
        )
        with pytest.raises(ValueError) as exc:
            parse_event(md)
        assert "Notes" in str(exc.value)

    def test_t5_unknown_frontmatter_key_raises(self):
        md = """---
title: Foo
created: 2026-01-15
last_updated: 2026-03-16
tags: []
status: active
owner: nick
---

## Summary
x

## Timeline
- **2026-01-15**: y

## Key Entities
- a

## Source References
- b
"""
        with pytest.raises(ValueError) as exc:
            parse_event(md)
        assert "forbid" in str(exc.value).lower() or "extra" in str(exc.value).lower()

    def test_t6_missing_key_entities_section_defaults_empty(self):
        md = """---
title: Foo
created: 2026-01-15
last_updated: 2026-03-16
tags: []
status: active
---

## Summary
s

## Timeline
- **2026-01-15**: t

## Source References
- r
"""
        ef = parse_event(md)
        assert ef.key_entities == []

    def test_t7_timeline_no_bold_raises(self):
        md = """---
title: Foo
created: 2026-01-15
last_updated: 2026-03-16
tags: []
status: active
---

## Summary
s

## Timeline
- 2026-03-16: no bold

## Key Entities
- a

## Source References
- b
"""
        with pytest.raises(ValueError):
            parse_event(md)

    def test_status_illegal_raises_validationerror(self):
        with pytest.raises(ValidationError):
            EventFile(
                title="x",
                created=date(2026, 1, 1),
                last_updated=date(2026, 1, 1),
                tags=[],
                status="foo",
                summary="",
                timeline=[],
                key_entities=[],
                source_references=[],
            )


class TestEventMarkdownRender:
    def test_t1_render_format(self):
        ef = parse_event(PRD_EXAMPLE)
        out = render_event(ef)
        assert out.startswith("---\n")
        assert "created: 2026-01-15" in out
        assert "'2026" not in out and '"2026' not in out  # bare date
        assert "- **2026-03-16**:" in out
        assert out.count("## ") == 4  # Summary, Timeline, Key Entities, Source References

    def test_t2_roundtrip_any(self):
        ef = EventFile(
            title="台積電亞利桑那廠",
            created=date(2026, 1, 15),
            last_updated=date(2026, 3, 16),
            tags=["半導體"],
            status="active",
            summary="中文。",
            timeline=[TimelineEntry(entry_date=date(2026, 3, 16), text="更新")],
            key_entities=["TSMC"],
            source_references=[],
        )
        rendered = render_event(ef)
        parsed = parse_event(rendered)
        assert parsed == ef
        assert "半導體" in rendered  # CJK tags stay human-readable, not \u-escaped
        assert "\\u" not in rendered

    def test_t3_roundtrip_example(self):
        original = parse_event(PRD_EXAMPLE)
        rendered = render_event(original)
        reparsed = parse_event(rendered)
        assert reparsed == original

    def test_t4_chinese_no_escape(self):
        ef = EventFile(
            title="台積電亞利桑那廠",
            created=date(2026, 1, 15),
            last_updated=date(2026, 3, 16),
            tags=[],
            status="active",
            summary="",
            timeline=[],
            key_entities=[],
            source_references=[],
        )
        out = render_event(ef)
        assert "台積電亞利桑那廠" in out
        assert "\\u" not in out

    def test_t5_colon_title_roundtrip(self):
        """Supplement for render contract: colon title must use yaml safe quote and roundtrip."""
        ef = EventFile(
            title="Foo: Bar",
            created=date(2026, 1, 15),
            last_updated=date(2026, 3, 16),
            tags=[],
            status="active",
            summary="has colon in title",
            timeline=[],
            key_entities=[],
            source_references=[],
        )
        rendered = render_event(ef)
        parsed = parse_event(rendered)
        assert parsed == ef
        # ensure safe quoting happened, no raw unquoted colon break
        assert "Foo: Bar" in rendered or "'Foo: Bar'" in rendered or '"Foo: Bar"' in rendered
