"""The sand line: one or two sentences of what happened, written for each topic of two or more.

The filter assigns the topics and no later call writes for one, so this is one more call per
issue, made only when the issue holds sand. Its line is the cluster's `DigestSection.summary`;
a topic the call leaves without one keeps the stand-in, its first member's excerpt.
"""

import logging
from collections.abc import Sequence

from cyris.domain.models import DigestSection, UsageStats
from cyris.service_layer.parse import echoed_position
from cyris.service_layer.ports import LLMClient, complete_json
from cyris.service_layer.prompts import build_sand_line_prompt, build_sand_line_system_prompt

logger = logging.getLogger(__name__)


def _is_sand(section: DigestSection) -> bool:
    return len(section.members) >= 2


async def write_sand_lines(
    clusters: Sequence[DigestSection],
    llm: LLMClient | None,
    usage: UsageStats,
    *,
    output_language: str,
    style_prompt: str,
) -> dict | None:
    """Write each sand topic's line onto its section's `summary`, in one call.

    The call's spend is added to `usage`. A topic the reply leaves out, a malformed reply or a
    failed call keeps the stand-in, and the run goes on: the stand-in is what the page showed
    before this step, so falling back to it never marks the run degraded.

    Returns:
        The step's receipt for the run summary, or None when it made no call.
    """
    sand = [section for section in clusters if _is_sand(section)]
    if not sand or llm is None:
        return None

    spend = UsageStats(model=usage.model)
    receipt: dict = {"topics": len(sand), "written": 0}
    prompt = build_sand_line_prompt(
        [(s.heading, [(m.title, m.summary) for m in s.members]) for s in sand]
    )
    try:
        data = await complete_json(
            llm,
            prompt,
            system=build_sand_line_system_prompt(output_language, style_prompt=style_prompt),
            temperature=1.0,
            usage=spend,
        )
        lines = _lines_in(data, len(sand))
    except Exception as e:
        logger.warning("Sand line call failed; every topic keeps its stand-in", exc_info=True)
        receipt["error"] = f"{type(e).__name__}: {e}"[:300]
        lines = {}
    for position, line in lines.items():
        sand[position].summary = line
    receipt["written"] = len(lines)
    if len(lines) < len(sand):
        logger.warning(
            "Sand line wrote %d of %d topics; the rest keep their stand-in", len(lines), len(sand)
        )
    usage.merge(spend)
    cost = spend.estimated_cost
    return receipt | {
        "api_calls": spend.api_calls,
        "input_tokens": spend.input_tokens,
        "output_tokens": spend.output_tokens,
        "estimated_cost_usd": round(cost, 6) if cost is not None else None,
    }


def _lines_in(data: object, count: int) -> dict[int, str]:
    """The reply's lines by topic position; an entry that names no topic or holds no text is
    skipped, and a reply with no `lines` object raises, as a failed call."""
    lines = data.get("lines") if isinstance(data, dict) else None
    if not isinstance(lines, dict):
        raise ValueError(f"sand line reply has no lines object: {data!r:.200}")
    found: dict[int, str] = {}
    for raw_id, text in lines.items():
        position = echoed_position(raw_id, count)
        if position is not None and isinstance(text, str) and text.strip():
            found.setdefault(position, text.strip())
    return found
