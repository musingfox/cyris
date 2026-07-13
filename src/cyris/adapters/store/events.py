"""Event file Pydantic schema and markdown parse/render (PRD schema, pyyaml frontmatter)."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class TimelineEntry(BaseModel):
    """Single timeline entry with date and text."""

    entry_date: date
    text: str


class EventFile(BaseModel):
    """Structured event from/to PRD markdown format. Strict: unknown keys/sections error."""

    model_config = ConfigDict(extra="forbid")

    title: str
    created: date
    last_updated: date
    tags: list[str] = Field(default_factory=list)
    status: Literal["active", "inactive"]
    summary: str = ""
    timeline: list[TimelineEntry] = Field(default_factory=list)
    key_entities: list[str] = Field(default_factory=list)
    source_references: list[str] = Field(default_factory=list)


KNOWN_SECTIONS = {"## Summary", "## Timeline", "## Key Entities", "## Source References"}


class _FrontMatter(BaseModel):
    """Internal strict frontmatter only (forbid extra keys like 'owner')."""

    model_config = ConfigDict(extra="forbid")

    title: str
    created: date
    last_updated: date
    tags: list[str] = Field(default_factory=list)
    status: Literal["active", "inactive"]


def parse_event(markdown: str) -> EventFile:
    """Parse PRD-format event markdown into EventFile.

    Raises ValueError on malformed or extra content.
    """
    if not markdown.strip().startswith("---"):
        raise ValueError("missing frontmatter")

    # Split frontmatter
    parts = markdown.split("---", 2)
    if len(parts) < 3:
        raise ValueError("invalid frontmatter delimiters")
    fm_raw = parts[1].strip()
    body = parts[2].strip()

    if not fm_raw:
        raise ValueError("empty frontmatter")

    try:
        frontmatter: dict[str, Any] = yaml.safe_load(fm_raw) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"frontmatter yaml error: {e}") from e

    if not isinstance(frontmatter, dict):
        raise ValueError("frontmatter must be mapping")

    # Pydantic will enforce extra forbid + required fields
    # Now parse body sections strictly
    # Validate frontmatter strictly first (unknown keys raise with 'forbid' per contract T5)
    try:
        _FrontMatter.model_validate(frontmatter)
    except Exception as e:
        raise ValueError(f"validation error (forbid extra?): {e}") from e

    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = stripped
            buf = []
            if current not in KNOWN_SECTIONS:
                raise ValueError(f"unknown section: {current}")
        else:
            if current is None:
                # leading text before first section is ignored (sections default to empty)
                continue
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()

    # Build fields with defaults for missing
    summary = sections.get("## Summary", "")
    timeline_raw = sections.get("## Timeline", "")
    key_raw = sections.get("## Key Entities", "")
    refs_raw = sections.get("## Source References", "")

    timeline: list[TimelineEntry] = []
    for line in timeline_raw.splitlines():
        line = line.strip()
        if not line or not line.startswith("- "):
            continue
        # require bold date: - **YYYY-MM-DD**: text
        rest = line[2:].strip()
        if not rest.startswith("**") or "**:" not in rest:
            raise ValueError(f"timeline entry must be bold date: {line}")
        try:
            date_part, text_part = rest.split("**:", 1)
            dstr = date_part.strip("* ")
            entry_date = date.fromisoformat(dstr)
            text = text_part.strip()
            timeline.append(TimelineEntry(entry_date=entry_date, text=text))
        except Exception as e:
            raise ValueError(f"bad timeline entry: {line}") from e

    key_entities: list[str] = []
    if key_raw:
        # handle "- TSMC, Arizona, ..." or multiple - lines
        for line in key_raw.splitlines():
            line = line.strip()
            if line.startswith("- "):
                content = line[2:].strip()
                # split on top-level commas
                for ent in [e.strip() for e in content.split(",") if e.strip()]:
                    key_entities.append(ent)

    source_references: list[str] = []
    for line in refs_raw.splitlines():
        line = line.strip()
        if line.startswith("- "):
            source_references.append(line[2:].strip())

    data = {
        "title": frontmatter["title"],
        "created": frontmatter["created"],
        "last_updated": frontmatter["last_updated"],
        "tags": frontmatter.get("tags", []) or [],
        "status": frontmatter["status"],
        "summary": summary,
        "timeline": timeline,
        "key_entities": key_entities,
        "source_references": source_references,
    }

    # Let pydantic validate dates etc and forbid extras
    try:
        return EventFile.model_validate(data)
    except Exception as e:
        # wrap to match contract T5 mentioning 'forbid'
        raise ValueError(f"validation error (forbid extra?): {e}") from e


def render_event(event: EventFile) -> str:
    """Render EventFile back to exact PRD markdown. Roundtrips cleanly."""
    # Manual frontmatter to guarantee bare dates, no extra quotes
    fm_lines = [
        "---",
        "title: "
        + yaml.safe_dump(event.title, default_flow_style=True, allow_unicode=True)
        .strip()
        .splitlines()[0],
        f"created: {event.created.isoformat()}",
        f"last_updated: {event.last_updated.isoformat()}",
        f"tags: {yaml.safe_dump(event.tags, default_flow_style=True, allow_unicode=True).strip()}",
        f"status: {event.status}",
        "---",
        "",
    ]
    lines: list[str] = fm_lines[:]

    lines.append("## Summary")
    if event.summary:
        lines.append(event.summary)
    lines.append("")

    lines.append("## Timeline")
    for te in event.timeline:
        lines.append(f"- **{te.entry_date.isoformat()}**: {te.text}")
    lines.append("")

    lines.append("## Key Entities")
    if event.key_entities:
        # one line comma list to match example
        line = "- " + ", ".join(event.key_entities)
        lines.append(line)
    lines.append("")

    lines.append("## Source References")
    for ref in event.source_references:
        lines.append(f"- {ref}")
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"
