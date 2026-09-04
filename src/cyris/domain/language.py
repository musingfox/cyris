"""Language detection via CJK character heuristic."""

import re

_CJK_PATTERN = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")


def detect_language(title: str, content: str = "") -> str:
    """Detect language from title and content snippet.

    Returns "zh" if 5+ CJK characters found in title + first 200 chars of content,
    otherwise "en".
    """
    text = title + content[:200]
    matches = _CJK_PATTERN.findall(text)
    return "zh" if len(matches) >= 5 else "en"


# Reader-facing order for the language column: Chinese first. `detect_language`
# only ever produces these, so anything else — a legacy row, an unset value —
# sorts after them. Both stores order by this, and the D1 backend builds its SQL
# CASE from the same tuple so the two cannot drift.
LANGUAGE_SORT_ORDER = ("zh", "en")


def language_sort_key(language: str | None) -> int:
    """Position of `language` in the reader-facing order; unknown values sort last."""
    try:
        return LANGUAGE_SORT_ORDER.index(language)
    except ValueError:
        return len(LANGUAGE_SORT_ORDER)
