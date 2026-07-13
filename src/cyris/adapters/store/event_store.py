"""EventStore for scanning/writing event markdown files under agent-vault/events."""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from pathlib import Path

from .events import EventFile, parse_event, render_event

logger = logging.getLogger(__name__)


def _sanitize_filename(title: str) -> str:
    """Sanitize title to a safe .md filename.

    Chinese preserved; ":" becomes space; other fs-illegal chars removed; whitespace collapsed.
    """
    s = title.replace(":", " ")
    s = re.sub(r'[/\\*?"<>|]', "", s)
    s = " ".join(s.split()).strip()
    if not s:
        s = "untitled"
    return f"{s}.md"


class EventStore:
    """Filesystem store for EventFile markdowns.

    load skips bad files with a warning; save overwrites by sanitized title.
    """

    def __init__(self, events_dir: Path) -> None:
        self.events_dir = events_dir

    def load_events(self) -> list[EventFile]:
        """Load all *.md sorted by filename. Bad files -> warning + skip. Missing dir -> []."""
        if not self.events_dir.exists():
            return []
        events: list[EventFile] = []
        for p in sorted(self.events_dir.glob("*.md")):
            if p.name == ".gitkeep":
                continue
            try:
                text = p.read_text(encoding="utf-8")
                ef = parse_event(text)
                events.append(ef)
            except Exception as exc:  # malformed frontmatter, sections, timeline etc.
                logger.warning("skipping bad event file %s: %s", p.name, exc)
        return events

    def save_event(self, event: EventFile) -> Path:
        """Write event as <sanitized-title>.md under events_dir. Overwrites. Returns path."""
        fname = _sanitize_filename(event.title)
        self.events_dir.mkdir(parents=True, exist_ok=True)
        path = self.events_dir / fname
        path.write_text(render_event(event), encoding="utf-8")  # save render
        return path

    def mark_stale_inactive(self, today: date, stale_days: int = 30) -> list[str]:
        """Set status=inactive on active events with no update for stale_days+. Returns titles."""
        cutoff = today - timedelta(days=stale_days)
        changed: list[str] = []
        for ef in self.load_events():
            if ef.status == "active" and ef.last_updated <= cutoff:
                ef.status = "inactive"
                self.save_event(ef)
                changed.append(ef.title)
        return changed
