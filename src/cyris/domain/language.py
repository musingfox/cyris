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
