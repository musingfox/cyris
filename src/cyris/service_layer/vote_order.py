"""Use case: score the articles an issue kept by how likely this reader is to upvote them.

Two sources can score them. `cosine` reads the vote-similarity pass's verdicts. `clef`
puts one question per kept article to Clef, beside the titles of its nearest votes,
and falls back to the cosine scores when it cannot answer every question: a digest
must still go out, and a half-Clef order would mix two scales.
"""

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from cyris.domain.models import Article
from cyris.service_layer import prompts
from cyris.service_layer.ports import AskClef, NoulAnswer
from cyris.service_layer.vote_similarity import VoteSimilarityReport

logger = logging.getLogger(__name__)

# Eight in flight: an issue keeps up to a few dozen items, and one request per item at
# once would hit Workers AI's rate limit, which answers 429; a 429 that persists through
# two retries fails the whole pass.
CLEF_CONCURRENCY = 8
# One minute, so the pass plus the publish budget and the run reserve end well before
# the container's sleep-after (tests/test_publish.py pins that total).
CLEF_PASS_SECONDS = 60
# The nearest votes shown per article: enough to show the reader's taste there,
# few enough to keep the question short.
CLEF_NEAREST_VOTES = 5


@dataclass
class OrderReceipt:
    """Which source ordered the issue, and why Clef did not when it was asked to."""

    source: str | None = None
    model: str | None = None
    skipped: str = ""
    clef_skipped: str | None = None
    clef_spend: dict | None = None

    def as_summary(self) -> dict:
        summary: dict = {}
        if self.source is not None:
            summary["preference_source"] = self.source
            summary["preference_model"] = self.model
        if self.clef_skipped is not None:
            summary["preference_clef_skipped"] = self.clef_skipped
        if self.clef_spend is not None:
            summary["clef"] = self.clef_spend
        return summary


class VoteOrder:
    def __init__(
        self,
        *,
        rank: bool,
        source: Literal["cosine", "clef"],
        similarity: VoteSimilarityReport | None,
        ask_clef: AskClef | None,
        embedding_model: str,
        snippet_length: int,
    ) -> None:
        self._rank = rank
        self._source = source
        self._similarity = similarity
        self._ask_clef = ask_clef
        self._embedding_model = embedding_model
        self._snippet_length = snippet_length
        self.receipt = OrderReceipt()

    async def __call__(self, kept: list[Article]) -> Mapping[str, float] | None:
        """Each kept article's score, or None when this issue keeps the model's order."""
        similarity = self._similarity
        reason = self._unranked_reason()
        if reason:
            self.receipt = OrderReceipt(
                skipped=reason, clef_skipped=reason if self._wants_clef() else None
            )
            return None
        assert similarity is not None
        kept_urls = {a.url for a in kept}
        cosine = {
            url: verdict.net for url, verdict in similarity.verdicts.items() if url in kept_urls
        }
        if self._source != "clef":
            self.receipt = OrderReceipt(source="cosine", model=self._embedding_model)
            return cosine

        scores, receipt = await self._by_clef(kept, similarity)
        if scores is not None:
            self.receipt = receipt
            return scores
        receipt.source, receipt.model = "cosine", self._embedding_model
        self.receipt = receipt
        return cosine

    def _wants_clef(self) -> bool:
        return self._rank and self._source == "clef"

    def _unranked_reason(self) -> str:
        if not self._rank:
            return "switched off"
        if self._similarity is None:
            return "vote similarity off"
        if not self._similarity.ran:
            return f"vote similarity skipped: {self._similarity.skipped_reason}"
        return ""

    async def _by_clef(
        self, kept: list[Article], similarity: VoteSimilarityReport
    ) -> tuple[dict[str, float] | None, OrderReceipt]:
        """Clef's score per asked article, or None with the reason it could not give them."""
        ask = self._ask_clef
        if ask is None:
            return None, OrderReceipt(clef_skipped="no Workers AI token or account id")
        asked = [a for a in kept if a.url in similarity.candidate_vectors]
        if not asked:
            return None, OrderReceipt(clef_skipped="no kept item has a nearest vote")

        gate = asyncio.Semaphore(CLEF_CONCURRENCY)
        answers: dict[str, NoulAnswer] = {}
        calls = 0

        async def put(article: Article) -> None:
            nonlocal calls
            async with gate:
                calls += 1
                answers[article.url] = await ask(
                    self._question(article, similarity), prompts.CLEF_PREFERENCE
                )

        def spend() -> dict | None:
            if not calls:
                return None
            tokens = [a.input_tokens for a in answers.values()]
            known = bool(tokens) and all(isinstance(t, int) for t in tokens)
            return {
                "calls": calls,
                "answered": len(answers),
                "input_tokens": sum(tokens) if known else None,
                "model": _models(answers.values()),
            }

        pass_seconds = CLEF_PASS_SECONDS
        try:
            async with asyncio.timeout(pass_seconds):
                async with asyncio.TaskGroup() as group:
                    for article in asked:
                        group.create_task(put(article))
        except TimeoutError:
            reason = f"clef timed out after {pass_seconds:g}s"
        except ExceptionGroup as group:
            error = group
            while isinstance(error, BaseExceptionGroup):
                error = error.exceptions[0]
            reason = f"clef failed: {type(error).__name__}: {error}"
        else:
            missing = {a.url for a in asked} - answers.keys()
            if not missing:
                model = _models(answers.values())
                return {url: a.noul for url, a in answers.items()}, OrderReceipt(
                    source="clef", model=model, clef_spend=spend()
                )
            reason = f"clef answered {len(asked) - len(missing)} of {len(asked)} questions"
        logger.warning("Clef did not order this issue, the cosine order stands: %s", reason)
        return None, OrderReceipt(clef_skipped=reason, clef_spend=spend())

    def _question(self, article: Article, similarity: VoteSimilarityReport) -> dict:
        up, down = similarity.nearest_titles(article.url, CLEF_NEAREST_VOTES)
        return {
            "article": {
                "title": article.title,
                "source": article.source_name,
                "content": article.content[: self._snippet_length],
            },
            "reader_upvoted": up,
            "reader_downvoted": down,
        }


def _models(answers) -> str | None:
    return (
        ", ".join(sorted({a.model for a in answers if isinstance(a.model, str) and a.model}))
        or None
    )
