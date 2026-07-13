"""Tracking config source: load and upsert for topics in agent-vault/tracking.yaml.

Zero AI calls, zero new dependencies. Mirrors FetchSource Protocol style.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Protocol, runtime_checkable

import yaml

from cyris.domain.tracking import TrackedTopic


def _represent_date(dumper, data):
    """Custom representer so date serializes as bare YYYY-MM-DD (no quotes, matches contract)."""
    return dumper.represent_scalar("tag:yaml.org,2002:timestamp", data.isoformat())


yaml.SafeDumper.add_representer(date, _represent_date)


@runtime_checkable
class TrackingConfigSource(Protocol):
    """Async Protocol for tracking config sources (load + upsert).

    Mirrors the style of FetchSource in src/cyris/fetch/source_interface.py.
    """

    async def load_topics(self) -> list[TrackedTopic]:
        """Load list of tracked topics. Missing file returns []. Bad file raises ValueError."""
        ...

    async def upsert_topic(self, topic: TrackedTopic) -> None:
        """Insert or replace by name. Creates dirs. Bad file -> ValueError (no modify)."""
        ...


DEFAULT_TRACKING_PATH: Path = Path("agent-vault/tracking.yaml")


def _read_tracking_file(path: Path) -> list[TrackedTopic]:
    """Internal: read/validate; missing->[]; bad yaml or schema -> ValueError w/ path."""
    if not path.exists():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise ValueError(f"Invalid tracking YAML at {path}: {exc}") from exc

    topics_raw = raw.get("topics") or []
    try:
        return [TrackedTopic.model_validate(item) for item in topics_raw]
    except Exception as exc:
        raise ValueError(f"Invalid tracking config at {path}: {exc}") from exc


def _write_tracking_file(path: Path, topics: list[TrackedTopic]) -> None:
    """Internal: write human-readable YAML (bare dates, unicode, no escapes)."""
    data = {
        "topics": [
            {
                "name": t.name,
                "keywords": t.keywords,
                "created": t.created,
                "status": t.status,
            }
            for t in topics
        ]
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            data,
            f,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )


async def load_topics_from_file(path: Path) -> list[TrackedTopic]:
    """Load topics from explicit tracking file path."""
    return _read_tracking_file(path)


async def upsert_topic_to_file(path: Path, topic: TrackedTopic) -> None:
    """Upsert to explicit path: replace by name or append; raises on bad (no modify)."""
    try:
        existing = _read_tracking_file(path)
    except ValueError:
        raise
    new_list: list[TrackedTopic] = []
    replaced = False
    for t in existing:
        if t.name == topic.name:
            new_list.append(topic)
            replaced = True
        else:
            new_list.append(t)
    if not replaced:
        new_list.append(topic)
    _write_tracking_file(path, new_list)


async def load_topics(tracking_path: Path | None = None) -> list[TrackedTopic]:
    """Load topics using default or provided path. Nonexistent file -> empty list."""
    path = tracking_path or DEFAULT_TRACKING_PATH
    return await load_topics_from_file(path)


async def upsert_topic(topic: TrackedTopic, tracking_path: Path | None = None) -> None:
    """Upsert topic using default or provided path."""
    path = tracking_path or DEFAULT_TRACKING_PATH
    await upsert_topic_to_file(path, topic)
