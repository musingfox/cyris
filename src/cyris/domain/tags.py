"""Canonical tag normalization."""

import unicodedata
from collections.abc import Iterable


def normalize_tag(tag: str) -> str | None:
    """Normalize a tag to its canonical form, dropping empty values."""
    normalized = " ".join(unicodedata.normalize("NFKC", tag).casefold().split())
    return normalized or None


def normalize_tags(tags: Iterable[object]) -> list[str]:
    """Normalize string tags and deduplicate them in input order."""
    normalized_tags: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        if not isinstance(tag, str):
            continue
        normalized = normalize_tag(tag)
        if normalized is not None and normalized not in seen:
            seen.add(normalized)
            normalized_tags.append(normalized)
    return normalized_tags
