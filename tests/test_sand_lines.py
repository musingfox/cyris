"""The sand line: one call per issue writes what happened for every topic of two or more."""

import hashlib
import json

import pytest
from fakes import FakeLLM

from cyris.domain.models import DigestItem, DigestSection, UsageStats
from cyris.service_layer.prompts import (
    OUTPUT_LANGUAGE_RULES,
    SUMMARY_OPENING_RULES,
    build_sand_line_system_prompt,
    language_wording,
)
from cyris.service_layer.sand_lines import write_sand_lines

pytestmark = pytest.mark.unit

CANOPY = "Novi Sad station canopy collapse"
CANOPY_REPORTS = [
    (
        "Canopy at Novi Sad railway station collapses",
        "A concrete canopy at the railway station in Novi Sad collapsed on Friday.",
    ),
    (
        "Serbia mourns victims of station roof collapse",
        "Serbia declared a day of mourning after the station's canopy fell.",
    ),
]
STRIKE = "Port workers' strike"
STRIKE_REPORTS = [
    ("Dockers walk out at three ports", "Workers at three ports stopped work at dawn."),
    ("Port strike enters second day", "Ships queued outside the harbour on Tuesday."),
]
CANOPY_LINE = (
    "A concrete canopy at Novi Sad's railway station collapsed on Friday, "
    "and Serbia declared a day of mourning."
)


def _member(title: str, excerpt: str) -> DigestItem:
    return DigestItem(
        title=title,
        summary=excerpt,
        sources=["Wire"],
        urls=[f"https://wire.example.test/{hashlib.sha1(title.encode()).hexdigest()[:6]}"],
        passed_through=True,
    )


def _sand(heading: str, reports: list[tuple[str, str]]) -> DigestSection:
    members = [_member(title, excerpt) for title, excerpt in reports]
    merged = DigestItem(
        title=heading,
        summary="",
        sources=[s for m in members for s in m.sources],
        urls=[u for m in members for u in m.urls],
    )
    return DigestSection(heading=heading, items=[merged], members=members)


async def _write(clusters, llm, usage=None):
    return await write_sand_lines(
        clusters,
        llm,
        usage if usage is not None else UsageStats(model=llm.model if llm else "none"),
        output_language="en",
        style_prompt="",
    )


# ---- the prompt ------------------------------------------------------------------


def test_the_system_prompt_forbids_the_causal_inference_and_carries_both_rule_blocks() -> None:
    system = build_sand_line_system_prompt("zh-Hant", style_prompt="")
    wording = language_wording("zh-Hant")

    assert "「導火線是一帶一路事故」" in system
    assert "causal inference" in system
    assert SUMMARY_OPENING_RULES.replace("<output_language>", wording) in system
    assert OUTPUT_LANGUAGE_RULES.replace("<output_language>", wording) in system
    assert "<output_language>" not in system


# ---- one call for every topic ----------------------------------------------------


async def test_one_call_gives_every_topic_its_heading_and_each_reports_title_and_excerpt() -> None:
    reply = {"lines": {"0": CANOPY_LINE, "1": "Workers at three ports went on strike."}}
    llm = FakeLLM(json.dumps(reply))
    canopy, strike = _sand(CANOPY, CANOPY_REPORTS), _sand(STRIKE, STRIKE_REPORTS)

    await _write([canopy, strike], llm)

    (call,) = llm.calls
    assert call["prompt"] == "\n".join(
        [
            "Topics:",
            "",
            f"[0] {CANOPY}",
            *(line for t, e in CANOPY_REPORTS for line in (f"    - {t}", f"      {e}")),
            "",
            f"[1] {STRIKE}",
            *(line for t, e in STRIKE_REPORTS for line in (f"    - {t}", f"      {e}")),
            "",
        ]
    )
    assert call["system"] == build_sand_line_system_prompt("en", style_prompt="")
    assert call["temperature"] == 1.0
    assert (canopy.summary, strike.summary) == (
        CANOPY_LINE,
        "Workers at three ports went on strike.",
    )


async def test_no_topic_of_two_means_no_call() -> None:
    llm = FakeLLM('{"lines": {"0": "Never asked."}}')
    lone = _sand("Alone", CANOPY_REPORTS[:1])

    receipt = await _write([lone], llm)

    assert receipt is None
    assert llm.calls == []
    assert lone.summary is None


async def test_with_no_model_nothing_is_written_and_nothing_is_asked() -> None:
    canopy = _sand(CANOPY, CANOPY_REPORTS)

    assert await _write([canopy], None) is None
    assert canopy.summary is None


async def test_only_topics_of_two_are_asked_and_the_ids_number_them_alone() -> None:
    llm = FakeLLM(json.dumps({"lines": {"0": CANOPY_LINE}}))
    lone, canopy = _sand("Alone", STRIKE_REPORTS[:1]), _sand(CANOPY, CANOPY_REPORTS)

    await _write([lone, canopy], llm)

    assert "Alone" not in llm.calls[0]["prompt"]
    assert (lone.summary, canopy.summary) == (None, CANOPY_LINE)


# ---- what falls back --------------------------------------------------------------


async def test_a_topic_the_reply_leaves_out_keeps_its_stand_in() -> None:
    llm = FakeLLM(json.dumps({"lines": {"1": "Workers at three ports went on strike."}}))
    canopy, strike = _sand(CANOPY, CANOPY_REPORTS), _sand(STRIKE, STRIKE_REPORTS)
    usage = UsageStats(model=llm.model)

    receipt = await _write([canopy, strike], llm, usage)

    assert canopy.summary is None
    assert strike.summary == "Workers at three ports went on strike."
    assert receipt is not None and (receipt["topics"], receipt["written"]) == (2, 1)
    # The stand-in is what the page showed before this step: no degraded notice for it.
    assert not usage.fell_back_to_excerpts


@pytest.mark.parametrize(
    "reply",
    [
        "[]",
        "not json at all",
        '{"lines": null}',
        '{"lines": ["A list, not an object."]}',
        '{"lines": {"0": "", "1": null}}',
        '{"lines": {"7": "Names no topic.", "x": "Nor this."}}',
    ],
)
async def test_a_malformed_reply_keeps_every_stand_in(reply: str) -> None:
    canopy, strike = _sand(CANOPY, CANOPY_REPORTS), _sand(STRIKE, STRIKE_REPORTS)
    usage = UsageStats(model="test-model")

    receipt = await _write([canopy, strike], FakeLLM(reply), usage)

    assert (canopy.summary, strike.summary) == (None, None)
    assert receipt is not None and receipt["written"] == 0
    assert usage.api_calls == 1 and not usage.fell_back_to_excerpts


async def test_a_failed_call_keeps_every_stand_in_and_says_why() -> None:
    canopy = _sand(CANOPY, CANOPY_REPORTS)
    usage = UsageStats(model="test-model")

    receipt = await _write([canopy], FakeLLM(error=RuntimeError("quota")), usage)

    assert canopy.summary is None
    assert receipt == {
        "topics": 1,
        "written": 0,
        "error": "RuntimeError: quota",
        "api_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "estimated_cost_usd": None,
    }
    assert usage.api_calls == 0 and not usage.fell_back_to_excerpts


# ---- the receipt ------------------------------------------------------------------


async def test_the_call_is_added_to_the_runs_usage_and_its_receipt_names_its_spend() -> None:
    llm = FakeLLM(
        json.dumps({"lines": {"0": CANOPY_LINE}}),
        model="claude-haiku-5-5",
        input_tokens=1200,
        output_tokens=80,
    )
    usage = UsageStats(model=llm.model, input_tokens=5000, output_tokens=900, api_calls=3)
    spend = UsageStats(model=llm.model, input_tokens=1200, output_tokens=80)

    receipt = await _write([_sand(CANOPY, CANOPY_REPORTS)], llm, usage)

    assert receipt == {
        "topics": 1,
        "written": 1,
        "api_calls": 1,
        "input_tokens": 1200,
        "output_tokens": 80,
        "estimated_cost_usd": round(spend.estimated_cost, 6),
    }
    assert (usage.api_calls, usage.input_tokens, usage.output_tokens) == (4, 6200, 980)
