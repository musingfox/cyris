"""Tests for VoteOrder: the source that scores an issue's kept articles."""

import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from cyris.adapters.clef import ClefError
from cyris.domain.models import Article, Tier
from cyris.domain.similarity import SimilarityVerdict
from cyris.service_layer import prompts, vote_order
from cyris.service_layer.ports import NoulAnswer
from cyris.service_layer.vote_order import VoteOrder
from cyris.service_layer.vote_similarity import VoteSimilarityReport

pytestmark = pytest.mark.unit

MODEL = "gemini-embedding-001"
SNIPPET = 1000


def _article(url: str, title: str = "T", content: str = "body") -> Article:
    return Article(
        id=1,
        title=title,
        url=url,
        content=content,
        published_at=datetime(2026, 4, 10, tzinfo=UTC),
        source_name="Wire",
        source_tier=Tier.FILTER,
    )


def _report(nets: dict[str, float], *, vectors: bool = True, **fields) -> VoteSimilarityReport:
    verdicts = {
        url: SimilarityVerdict(url=url, down_similarity=0.0, up_similarity=net, suppressed=False)
        for url, net in nets.items()
    }
    return VoteSimilarityReport(
        verdicts=verdicts,
        candidate_vectors={url: [1.0, 0.0, 0.0] for url in nets} if vectors else {},
        up_seed_vectors={"u": [1.0, 0.0, 0.0]},
        down_seed_vectors={"d": [0.0, 1.0, 0.0]},
        seed_titles={"u": "Up", "d": "Down"},
        **fields,
    )


class _Clef:
    def __init__(self, answers: dict[str, NoulAnswer | Exception] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[tuple[dict, str]] = []

    async def __call__(self, state: dict, instructions: str) -> NoulAnswer:
        self.calls.append((state, instructions))
        answer = self.answers[state["article"]["title"]]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _order(report, ask=None, *, rank=True, source="clef") -> VoteOrder:
    return VoteOrder(
        rank=rank,
        source=source,
        similarity=report,
        ask_clef=ask,
        embedding_model=MODEL,
        snippet_length=SNIPPET,
    )


def _answer(noul: float, tokens: int | None = 842) -> NoulAnswer:
    return NoulAnswer(noul=noul, model="clef-flash", input_tokens=tokens)


# ---- CosineVoteOrderUnchanged ----------------------------------------------------------


async def test_cosine_source_returns_each_verdicts_net_and_asks_no_clef() -> None:
    clef = _Clef()
    order = _order(_report({"a": 0.3, "b": -0.1}), clef, source="cosine")

    scores = await order([_article("a"), _article("b")])

    assert scores == {"a": 0.3, "b": -0.1}
    assert order.receipt.source == "cosine"
    assert order.receipt.model == MODEL
    assert order.receipt.clef_skipped is None
    assert order.receipt.clef_spend is None
    assert clef.calls == []


async def test_ranking_off_keeps_the_model_order() -> None:
    order = _order(_report({"a": 0.3}), rank=False, source="cosine")

    assert await order([_article("a")]) is None
    assert order.receipt.source is None
    assert order.receipt.skipped == "switched off"


async def test_without_a_similarity_report_nothing_is_ordered() -> None:
    order = _order(None, source="cosine")

    assert await order([_article("a")]) is None
    assert order.receipt.skipped == "vote similarity off"


async def test_a_skipped_similarity_pass_names_its_reason() -> None:
    report = _report({}, skipped_reason="no human-voted articles yet")
    order = _order(report, source="cosine")

    assert await order([_article("a")]) is None
    assert order.receipt.skipped == "vote similarity skipped: no human-voted articles yet"


async def test_ranking_off_with_clef_asks_nothing_and_reports_no_clef_reason() -> None:
    clef = _Clef()
    order = _order(_report({"a": 0.3}), clef, rank=False)

    assert await order([_article("a")]) is None
    assert order.receipt.skipped == "switched off"
    assert order.receipt.clef_skipped is None
    assert clef.calls == []


async def test_clef_without_a_similarity_report_reports_the_same_reason_twice() -> None:
    order = _order(None, _Clef())

    assert await order([_article("a")]) is None
    assert order.receipt.skipped == order.receipt.clef_skipped == "vote similarity off"


# ---- ClefVoteOrder ---------------------------------------------------------------------


async def test_clef_scores_each_kept_article_by_its_probability() -> None:
    clef = _Clef({"A": _answer(0.2), "B": _answer(0.7)})
    order = _order(_report({"a": 0.0, "b": 0.0}), clef)

    scores = await order([_article("a", "A"), _article("b", "B")])

    assert scores == {"a": 0.2, "b": 0.7}
    assert order.receipt.source == "clef"
    assert order.receipt.model == "clef-flash"
    assert order.receipt.clef_skipped is None
    assert order.receipt.clef_spend == {
        "calls": 2,
        "answered": 2,
        "input_tokens": 1684,
        "model": "clef-flash",
    }


async def test_an_article_without_a_candidate_vector_is_not_asked() -> None:
    clef = _Clef({"A": _answer(0.2), "X": _answer(0.5)})
    order = _order(_report({"a": 0.0}), clef)

    scores = await order([_article("a", "A"), _article("x", "X")])

    assert scores == {"a": 0.2}
    assert len(clef.calls) == 1


async def test_an_answer_without_a_token_count_leaves_the_total_unknown() -> None:
    clef = _Clef({"A": _answer(0.2, None), "B": _answer(0.7)})
    order = _order(_report({"a": 0.0, "b": 0.0}), clef)

    await order([_article("a", "A"), _article("b", "B")])

    assert order.receipt.clef_spend["input_tokens"] is None


# ---- ClefFallsBackToCosine -------------------------------------------------------------


async def test_one_failed_question_sends_the_whole_issue_to_the_cosine_order() -> None:
    clef = _Clef(
        {
            "A": _answer(0.9),
            "B": ClefError("Clef refused the request (HTTP 400): bad"),
            "C": _answer(0.1),
        }
    )
    order = _order(_report({"A": 0.1, "B": 0.3, "C": 0.2}), clef)

    scores = await order([_article("A", "A"), _article("B", "B"), _article("C", "C")])

    assert scores == {"A": 0.1, "B": 0.3, "C": 0.2}
    assert order.receipt.source == "cosine"
    assert order.receipt.model == MODEL
    assert (
        order.receipt.clef_skipped
        == "clef failed: ClefError: Clef refused the request (HTTP 400): bad"
    )


async def test_no_clef_client_takes_the_cosine_order_without_a_call() -> None:
    order = _order(_report({"a": 0.4}), None)

    assert await order([_article("a")]) == {"a": 0.4}
    assert order.receipt.clef_skipped == "no Workers AI token or account id"
    assert order.receipt.clef_spend is None


async def test_no_kept_vector_takes_the_cosine_order_without_a_call() -> None:
    clef = _Clef()
    order = _order(_report({}, vectors=False), clef)

    assert await order([_article("a")]) == {}
    assert order.receipt.clef_skipped == "no kept item has a nearest vote"
    assert clef.calls == []
    assert order.receipt.clef_spend is None


async def test_a_transport_error_is_named_by_its_type_and_message() -> None:
    clef = _Clef({"A": httpx.ConnectError("boom")})
    order = _order(_report({"a": 0.4}), clef)

    await order([_article("a", "A")])

    assert order.receipt.clef_skipped == "clef failed: ConnectError: boom"


async def test_cancelling_the_ordering_task_propagates_the_cancellation() -> None:
    never = asyncio.Event()

    async def hang(state: dict, instructions: str) -> NoulAnswer:
        await never.wait()
        raise AssertionError

    order = _order(_report({"a": 0.4}), hang)
    task = asyncio.create_task(order([_article("a", "A")]))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


# ---- ClefPassIsBounded -----------------------------------------------------------------


async def test_at_most_eight_questions_are_in_flight() -> None:
    pending = peak = 0

    async def slow(state: dict, instructions: str) -> NoulAnswer:
        nonlocal pending, peak
        pending += 1
        peak = max(peak, pending)
        await asyncio.sleep(0.01)
        pending -= 1
        return _answer(0.5)

    urls = [f"u{i}" for i in range(20)]
    order = _order(_report(dict.fromkeys(urls, 0.0)), slow)

    scores = await order([_article(u) for u in urls])

    assert len(scores) == 20
    assert peak == vote_order.CLEF_CONCURRENCY == 8


async def test_a_pass_past_its_deadline_takes_the_cosine_order(monkeypatch) -> None:
    monkeypatch.setattr(vote_order, "CLEF_PASS_SECONDS", 0.05)
    never = asyncio.Event()

    async def hang(state: dict, instructions: str) -> NoulAnswer:
        await never.wait()
        raise AssertionError

    order = _order(_report({"a": 0.4}), hang)

    scores = await asyncio.wait_for(order([_article("a")]), timeout=1)

    assert scores == {"a": 0.4}
    assert order.receipt.clef_skipped == "clef timed out after 0.05s"


# ---- ClefRequestShape ------------------------------------------------------------------


async def test_a_question_carries_the_article_and_its_five_nearest_votes_each_way() -> None:
    def vec(cosine: float) -> list[float]:
        return [cosine, (1 - cosine**2) ** 0.5, 0.0]

    report = VoteSimilarityReport(
        candidate_vectors={"a": [1.0, 0.0, 0.0]},
        up_seed_vectors={f"u{i}": vec(c) for i, c in enumerate((0.9, 0.8, 0.7, 0.6, 0.5, 0.4), 1)},
        down_seed_vectors={"d1": vec(0.8), "d2": vec(0.1)},
        seed_titles={
            **{f"u{i}": f"U{i}" for i in range(1, 7)},
            "d1": "D1",
            "d2": "D2",
        },
    )
    clef = _Clef({"Original A": _answer(0.5)})
    article = _article("a", "Original A", "x" * 1500)

    await _order(report, clef)([article])

    [(state, instructions)] = clef.calls
    assert state == {
        "article": {"title": "Original A", "source": "Wire", "content": "x" * 1000},
        "reader_upvoted": ["U1", "U2", "U3", "U4", "U5"],
        "reader_downvoted": ["D1", "D2"],
    }
    assert instructions is prompts.CLEF_PREFERENCE


async def test_without_upvotes_the_question_lists_none() -> None:
    report = VoteSimilarityReport(
        candidate_vectors={"a": [1.0, 0.0, 0.0]},
        down_seed_vectors={"d": [0.0, 1.0, 0.0]},
        seed_titles={"d": "Down"},
    )
    clef = _Clef({"A": _answer(0.5)})

    await _order(report, clef)([_article("a", "A")])

    assert clef.calls[0][0]["reader_upvoted"] == []


async def test_the_snippet_length_is_the_one_the_order_was_given() -> None:
    clef = _Clef({"A": _answer(0.5)})
    order = VoteOrder(
        rank=True,
        source="clef",
        similarity=_report({"a": 0.0}),
        ask_clef=clef,
        embedding_model=MODEL,
        snippet_length=7,
    )

    await order([_article("a", "A", "x" * 20)])

    assert clef.calls[0][0]["article"]["content"] == "x" * 7


async def test_the_cosine_scores_cover_only_the_kept_articles() -> None:
    order = _order(_report({"a": 0.3, "z": 0.9}), source="cosine")

    assert await order([_article("a")]) == {"a": 0.3}


async def test_clef_with_no_kept_vector_returns_no_other_candidates_scores() -> None:
    report = _report({"z": 0.9}, vectors=False)

    assert await _order(report, _Clef())([_article("a")]) == {}


async def test_a_question_that_cancels_itself_sends_the_issue_to_cosine() -> None:
    async def cancels(state: dict, instructions: str) -> NoulAnswer:
        if state["article"]["title"] == "B":
            raise asyncio.CancelledError
        return _answer(0.9)

    order = _order(_report({"a": 0.1, "b": 0.2}), cancels)

    scores = await order([_article("a", "A"), _article("b", "B")])

    assert scores == {"a": 0.1, "b": 0.2}
    assert order.receipt.source == "cosine"
    assert order.receipt.clef_skipped == "clef answered 1 of 2 questions"
