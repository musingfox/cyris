"""`embed-compare` and `llm-compare` — the judgements, without the CLI around them.

These used to live inside two typer commands, which is why the rules they encode
had no test at all: `margin`, what counts as a disagreement, and the one that
matters most — an arm that never reached its model is not a result.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import FakeLLM, make_config
from test_vote_similarity import FakeEmbedder, FakeStore, article
from typer.testing import CliRunner

from cyris.diagnostics.compare import (
    NothingToCompareError,
    compare_embedders,
    compare_llms,
    margin,
)
from cyris.domain.models import Article, ArticleState, SourceConfig, Tier
from cyris.entrypoints.cli import app

pytestmark = pytest.mark.integration


class _Usage:
    def as_dict(self) -> dict:
        return {"embedded": 2, "requests": 1, "api_seconds": 0.1, "neurons": 0, "input_tokens": 0}


class UsageTrackingEmbedder(FakeEmbedder):
    """`Embedder` carries no usage; `embed-compare` reads it anyway — see `ports.Embedder.usage`."""

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


def test_margin_names_the_lowest_cut_and_highest_kept_cosine():
    assert margin(_verdicts([(0.82, True), (0.61, False)])) == {
        "suppressed_min": 0.82,
        "kept_max": 0.61,
    }


def test_margin_has_no_boundary_when_a_side_is_empty():
    assert margin(_verdicts([(0.61, True)])) == {"suppressed_min": 0.61, "kept_max": None}
    assert margin(_verdicts([(0.2, False)])) == {"suppressed_min": None, "kept_max": 0.2}


class _ScaledEmbedder(UsageTrackingEmbedder):
    """Seeds on [1, 0]; each candidate's cosine is its table value plus `shift`."""

    COSINES = {f"C{i}": 0.40 + 0.01 * i for i in range(11)} | {"Out": 0.70}

    def __init__(self, shift: float = 0.0) -> None:
        self._shift = shift

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            if text.startswith("S"):
                out.append([1.0, 0.0])
            else:
                c = self.COSINES[text] + self._shift
                out.append([c, (1 - c * c) ** 0.5])
        return out


_WINDOW_SEEDS = FakeStore(
    [article(f"s{i}", f"S{i}", ArticleState.REJECTED, triaged=True) for i in range(3)]
)
_WINDOW = [article(title, title) for title in [*_ScaledEmbedder.COSINES]]


async def test_an_outlier_is_the_same_under_a_shifted_cosine_scale():
    """One window, two arms on different cosine scales: both cut the same row, so it agrees."""
    comparison = await compare_embedders(
        _WINDOW_SEEDS,
        [("gemini", _ScaledEmbedder()), ("workers_ai", _ScaledEmbedder(shift=0.2))],
        _WINDOW,
        hours=24,
        max_seeds=200,
        checked_at=datetime(2026, 9, 5, tzinfo=UTC),
    )

    assert comparison.agreed == {"Out"}
    assert comparison.only == {"gemini_only": [], "workers_ai_only": []}


async def test_the_log_row_names_each_arms_cut_and_carries_no_threshold():
    comparison = await compare_embedders(
        _WINDOW_SEEDS,
        [("gemini", _ScaledEmbedder()), ("workers_ai", _ScaledEmbedder(shift=0.2))],
        _WINDOW,
        hours=24,
        max_seeds=200,
        checked_at=datetime(2026, 9, 5, tzinfo=UTC),
    )

    arm = comparison.log_row()["gemini"]
    assert {"suppressed", "suppressed_min", "kept_max"} <= arm.keys()
    assert "threshold" not in arm
    assert arm["suppressed_min"] == 0.7


async def test_a_window_without_seeds_is_not_a_comparison():
    store = FakeStore([article("d", "Lottery draw", ArticleState.REJECTED, triaged=False)])

    with pytest.raises(NothingToCompareError):
        await compare_embedders(
            store,
            [("gemini", UsageTrackingEmbedder())],
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
    cfg = make_config(
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
    assert rows[0].rendered == "# digest"
    assert rows[0].content.usage.api_calls > 0


def test_the_cli_no_longer_holds_the_comparison_logic() -> None:
    """`cli.py` parses `--arm` and prints; the arms and the rules live in diagnostics."""
    source = Path("src/cyris/entrypoints/cli.py").read_text()

    assert "GeminiEmbedder(" not in source
    assert "WorkersAIEmbedder(" not in source
    assert "DigestPipeline(" not in source
    assert "api_calls == 0" not in source


def test_each_arm_is_named_with_its_embedder() -> None:
    from cyris.diagnostics.compare import build_embedding_arms

    arms = build_embedding_arms("acct", "gemini-key", "workers-token")

    assert [name for name, _ in arms] == ["gemini", "workers_ai"]


@pytest.mark.parametrize("flag", ["--threshold", "--workers-threshold"])
def test_embed_compare_refuses_a_cutoff_option(flag: str) -> None:
    result = CliRunner().invoke(app, ["embed-compare", flag, "0.5"])

    assert result.exit_code == 2
