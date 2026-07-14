"""Event timeline domain models (pure; markdown parse/render lives in adapters/store)."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TimelineEntry(BaseModel):
    """Single timeline entry with date and text."""

    entry_date: date
    text: str


class EventFile(BaseModel):
    """Structured event. Strict: unknown keys/sections error."""

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
