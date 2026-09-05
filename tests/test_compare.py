"""`embed-compare` and `llm-compare` — the judgements, without the CLI around them.

These used to live inside two typer commands, which is why the rules they encode
had no test at all: `margin`, what counts as a disagreement, and the one that
matters most — an arm that never reached its model is not a result.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import FakeLLM
from test_vote_similarity import FakeEmbedder, FakeStore, article

from cyris.config import AppConfig, Config
from cyris.diagnostics.compare import (
    NothingToCompareError,
    compare_embedders,
    compare_llms,
    margin,
)
from cyris.domain.models import Article, ArticleState, SourceConfig, Tier


class _Usage:
    def as_dict(self) -> dict:
        return {"embedded": 2, "requests": 1, "api_seconds": 0.1, "neurons": 0, "input_tokens": 0}


class UsageTrackingEmbedder(FakeEmbedder):
    """`Embedder` carries no usage; `embed-compare` reads it anyway — see §7 #20."""

    usage = _Usage()


def _verdicts(pairs: list[tuple[float, bool]]):
    class V:
        def __init__(self, similarity: float, suppressed: bool) -> None:
            self.down_similarity = similarity
            self.suppressed = suppressed

    class R:
        verdicts = {str(i): V(s, sup) for i, (s, sup) in enumerate(pairs)}

    return R()


def test_margin_reports_both_sides_of_the_boundary():
    """Which is threshold staleness and which is a model difference: only these tell them apart."""
    m = margin(_verdicts([(0.61, True), (0.58, True), (0.49, False), (0.31, False)]))

    assert m == {"suppressed_min": 0.58, "kept_max": 0.49}


def test_margin_has_no_boundary_when_a_side_is_empty():
    assert margin(_verdicts([(0.61, True)])) == {"suppressed_min": 0.61, "kept_max": None}
    assert margin(_verdicts([(0.2, False)])) == {"suppressed_min": None, "kept_max": 0.2}


async def test_the_arms_agree_and_disagree_by_url():
    """A shared threshold makes both arms suppress the same row; a raised one does not."""
    store = FakeStore([article("d", "Lottery draw", ArticleState.REJECTED, triaged=True)])
    candidates = [article("c1", "Lottery again"), article("c2", "Tech thing")]
    now = datetime(2026, 9, 5, tzinfo=UTC)

    comparison = await compare_embedders(
        store,
        [
            ("gemini", UsageTrackingEmbedder(), 0.8),
            ("workers_ai", UsageTrackingEmbedder(), 1.5),  # above any cosine: suppresses nothing
        ],
        candidates,
        hours=24,
        max_seeds=200,
        checked_at=now,
    )

    assert comparison.agreed == set()
    assert comparison.only == {"gemini_only": ["c1"], "workers_ai_only": []}

    row = comparison.log_row()
    assert row["checked_at"] == now.isoformat()
    assert row["candidates"] == 2
    assert row["gemini"]["suppressed"] == 1
    assert row["workers_ai"]["suppressed"] == 0


async def test_what_both_arms_suppress_is_agreement_not_a_disagreement():
    """The broken version of this returns each arm's whole set as its own — every
    row would read as a disagreement, which is the one number the log is kept for."""
    store = FakeStore([article("d", "Lottery draw", ArticleState.REJECTED, triaged=True)])
    candidates = [article("c1", "Lottery again"), article("c2", "Tech thing")]

    comparison = await compare_embedders(
        store,
        [
            ("gemini", UsageTrackingEmbedder(), 0.8),
            ("workers_ai", UsageTrackingEmbedder(), 0.8),
        ],
        candidates,
        hours=24,
        max_seeds=200,
        checked_at=datetime(2026, 9, 5, tzinfo=UTC),
    )

    assert comparison.agreed == {"c1"}
    assert comparison.only == {"gemini_only": [], "workers_ai_only": []}


async def test_a_window_without_seeds_is_not_a_comparison():
    store = FakeStore([article("d", "Lottery draw", ArticleState.REJECTED, triaged=False)])

    with pytest.raises(NothingToCompareError):
        await compare_embedders(
            store,
            [("gemini", UsageTrackingEmbedder(), 0.8)],
            [article("c1", "Lottery again")],
            hours=24,
            max_seeds=200,
            checked_at=datetime(2026, 9, 5, tzinfo=UTC),
        )


def _article(url: str) -> Article:
    return Article(
        id=url,
        title="Something happened",
        url=url,
        content="Body text",
        published_at=datetime(2026, 9, 5, tzinfo=UTC),
        source_name="Src",
        source_tier=Tier.FILTER,
    )


def test_an_arm_that_never_reached_its_model_is_not_a_row():
    """The pipeline ships excerpts when the LLM fails — a plausible digest from no model
    at all. Printing it beside a real arm invites reading the fallback as that model's
    work, so zero API calls is dropped rather than reported."""
    cfg = Config(
        app=AppConfig.model_validate({}),
        sources={"Src": SourceConfig(name="Src", url="http://s", tier=Tier.FILTER)},
    )

    rows = compare_llms(
        [
            ("dead", FakeLLM(error=RuntimeError("401"))),
            ("alive", FakeLLM(responses="{}")),
        ],
        [_article("http://a.com/1")],
        {},
        cfg,
        period="morning",
        render=lambda content: "# digest",
    )

    assert [row.label for row in rows] == ["alive"]
    assert rows[0].markdown == "# digest"
    assert rows[0].content.usage.api_calls > 0


def test_the_cli_no_longer_holds_the_comparison_logic() -> None:
    """`cli.py` parses `--arm` and prints; the arms and the rules live in diagnostics."""
    source = Path("src/cyris/entrypoints/cli.py").read_text()

    assert "GeminiEmbedder(" not in source
    assert "WorkersAIEmbedder(" not in source
    assert "DigestPipeline(" not in source
    assert "api_calls == 0" not in source
