"""Parse Claude API responses, extracting JSON from possible markdown wrappers."""

import json
import re


def extract_json(text: str) -> dict:
    """Extract and parse JSON from Claude's response text.

    Handles responses that may be wrapped in markdown code blocks
    (```json ... ```) or contain leading/trailing text.
    """
    # Try direct parse first
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strip markdown code block
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Find first { ... } block
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))

    raise ValueError(f"No valid JSON found in response: {text[:200]}")


def echoed_position(raw_id: object, count: int) -> int | None:
    """The index a model echoed for a positional prompt id, or None if it names none.

    Models echo the bare index as an int or as a digit string; anything else,
    or an index outside the prompt's ``count`` articles, names no article.
    """
    if isinstance(raw_id, bool):
        return None
    if isinstance(raw_id, str):
        digits = raw_id.strip()
        # Length first: int() refuses a digit string past Python's conversion limit,
        # leading zeros included, and no index of ``count`` articles is longer than
        # ``count`` written out — so only the significant digits are converted.
        significant = digits.lstrip("0") or "0"
        fits = len(significant) <= len(str(count))
        raw_id = int(significant) if digits.isascii() and digits.isdigit() and fits else None
    if isinstance(raw_id, int) and 0 <= raw_id < count:
        return raw_id
    return None
