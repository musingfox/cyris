"""Mail vocabulary loaded from keywords.json.

The tokens are data; the structure around them is not. A subject line's forward
and reply prefixes repeat, nest, and are followed by either ASCII or fullwidth
colons — that shape is the same in every language, so it stays in the regexes
below while the words themselves live in the JSON.
"""

from __future__ import annotations

import json
import re
from functools import cache
from importlib.resources import files

_SEPARATOR = "[:：]"


@cache
def _vocabulary() -> dict[str, list[str]]:
    raw = (files(__package__) / "keywords.json").read_text(encoding="utf-8")
    return {k: v for k, v in json.loads(raw).items() if not k.startswith("_")}


def _alternation(key: str) -> str:
    return "|".join(re.escape(token) for token in _vocabulary()[key])


@cache
def subject_prefix_re() -> re.Pattern[str]:
    """Any run of forward/reply prefixes at the head of a subject."""
    both = f"{_alternation('forward_prefixes')}|{_alternation('reply_prefixes')}"
    return re.compile(rf"^\s*(?:(?:{both}){_SEPARATOR}\s*)+", re.IGNORECASE)


@cache
def private_reply_re() -> re.Pattern[str]:
    """A reply, or a forward of one — forwards may stack, the reply is the last prefix."""
    forwards = _alternation("forward_prefixes")
    replies = _alternation("reply_prefixes")
    return re.compile(
        rf"^(?:(?:{forwards}){_SEPARATOR}\s*)*(?:{replies}){_SEPARATOR}\s*",
        re.IGNORECASE,
    )


@cache
def view_in_browser_re() -> re.Pattern[str]:
    """The "read this on the web" wording newsletters put above their content."""
    return re.compile(_alternation("view_in_browser_markers"), re.IGNORECASE)
