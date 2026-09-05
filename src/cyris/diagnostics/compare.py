"""Run one window through two wirings and report where they differ.

Both comparisons have the same shape: build every arm before spending anything,
run the same input through each, and hand back rows. What a row means is decided
here; where it is printed and whether it is written down is the entrypoint's
business, which is what keeps these commands read-only by construction.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from cyris.config import Config
from cyris.domain.models import Article, DigestContent, StoredArticle
from cyris.service_layer.digest_pipeline import DigestPipeline
from cyris.service_layer.ports import EmbeddingUsage, LLMClient
from cyris.service_layer.vote_similarity import judge_by_votes

logger = logging.getLogger(__name__)


class NothingToCompareError(Exception):
    """The window, the credentials or the seeds cannot support a comparison."""


# --- Embedding providers ---


def margin(report) -> dict[str, float | None]:
    """Where this window's boundary actually fell, per arm.

    The thresholds are pinned constants calibrated against two downvote seeds. As
    the seed set grows they drift, and by different amounts because the two cosine
    scales differ — so the first disagreement this log records could just as easily
    be threshold staleness as a model difference. Recording each side of the
    boundary is what lets the two be told apart later.
    """
    cut_side = [v.down_similarity for v in report.verdicts.values() if v.suppressed]
    keep_side = [v.down_similarity for v in report.verdicts.values() if not v.suppressed]
    return {
        "suppressed_min": round(min(cut_side), 4) if cut_side else None,
        "kept_max": round(max(keep_side), 4) if keep_side else None,
    }


@dataclass(frozen=True)
class EmbedArm:
    name: str
    threshold: float
    report: Any
    usage: EmbeddingUsage
    wall_seconds: float


@dataclass(frozen=True)
class EmbedComparison:
    checked_at: datetime
    hours: int
    candidates: list[StoredArticle]
    arms: list[EmbedArm]

    @property
    def _suppressed(self) -> dict[str, set[str]]:
        return {arm.name: set(arm.report.suppressed_urls) for arm in self.arms}

    @property
    def agreed(self) -> set[str]:
        return set.intersection(*self._suppressed.values())

    @property
    def only(self) -> dict[str, list[str]]:
        """Per arm, what it alone suppressed — the whole point of running both."""
        sets = self._suppressed
        return {
            f"{name}_only": sorted(urls.difference(*(s for n, s in sets.items() if n != name)))
            for name, urls in sets.items()
        }

    def log_row(self) -> dict:
        first = self.arms[0].report
        return {
            "checked_at": self.checked_at.isoformat(),
            "hours": self.hours,
            "candidates": len(self.candidates),
            "seeds": {"up": first.upvote_seeds, "down": first.downvote_seeds},
            "agree": len(self.agreed),
            **self.only,
            **{
                arm.name: {
                    "threshold": arm.threshold,
                    "suppressed": len(arm.report.suppressed_urls),
                    "wall_seconds": round(arm.wall_seconds, 2),
                    **margin(arm.report),
                    **arm.usage.as_dict(),
                }
                for arm in self.arms
            },
        }


def build_embedding_arms(
    account_id: str,
    gemini_api_key: str,
    workers_api_token: str,
    *,
    gemini_threshold: float | None = None,
    workers_threshold: float | None = None,
) -> list[tuple[str, Any, float]]:
    """Every arm, built before a single embedding is paid for.

    Both cutoffs default to `provider_defaults.json` rather than to a number
    written here: each is a measured property of its model, and a comparison
    running on a stale one measures threshold drift instead of the difference
    between two models — the confusion `margin()` exists to prevent.
    """
    from cyris.adapters.embedding import GeminiEmbedder, WorkersAIEmbedder
    from cyris.bootstrap import embedding_defaults

    if not (account_id and workers_api_token):
        raise NothingToCompareError(
            "Needs CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_EMBEDDING_API_TOKEN "
            "(the token must carry Workers AI -> Read; the wrangler one does not)."
        )
    gemini = embedding_defaults("gemini")
    workers = embedding_defaults("workers_ai")
    return [
        (
            "gemini",
            GeminiEmbedder(api_key=gemini_api_key, model=gemini["model"]),
            gemini_threshold if gemini_threshold is not None else gemini["threshold"],
        ),
        (
            "workers_ai",
            WorkersAIEmbedder(
                api_token=workers_api_token, account_id=account_id, model=workers["model"]
            ),
            workers_threshold if workers_threshold is not None else workers["threshold"],
        ),
    ]


async def compare_embedders(
    store,
    arms: list[tuple[str, Any, float]],
    candidates: list[StoredArticle],
    *,
    hours: int,
    max_seeds: int,
    checked_at: datetime,
) -> EmbedComparison:
    results = []
    for name, embedder, threshold in arms:
        # Wall-clock, not just api_seconds: Gemini sleeps 1.5s between batches of 50
        # where bge-m3 batches 100 with no pause, and that gap is the throughput
        # difference the per-request timer cannot see.
        started = time.monotonic()
        report = await judge_by_votes(
            store, embedder, candidates, threshold=threshold, max_seeds=max_seeds
        )
        elapsed = time.monotonic() - started
        if not report.ran:
            raise NothingToCompareError(report.skipped_reason)
        results.append(EmbedArm(name, threshold, report, embedder.usage, elapsed))
    return EmbedComparison(checked_at, hours, candidates, results)


# --- LLM providers ---


@dataclass(frozen=True)
class ArmDigest:
    label: str
    content: DigestContent
    wall_seconds: float
    neurons: float | None
    rendered: str  # the arm's digest as HTML; the markdown writer went with the vault


def compare_llms(
    arms: list[tuple[str, LLMClient]],
    articles: Iterable[Article],
    scores: dict[str, float],
    cfg: Config,
    *,
    period: str,
    render: Callable[[DigestContent], str],
) -> list[ArmDigest]:
    """One digest per arm over the same articles and the same stored scores."""
    articles = list(articles)
    rows = []
    for label, llm in arms:
        pipeline = DigestPipeline(
            llm,
            max_digest_output=cfg.app.digest.max_articles_per_digest_output,
            summarize_snippet_length=cfg.app.digest.summarize_snippet_length,
            filter_snippet_length=cfg.app.digest.filter_snippet_length,
            score_threshold=cfg.app.routing.summarize_score_threshold,
            output_language=cfg.app.digest.output_language,
            style_prompt=cfg.app.digest.style_prompt,
        )
        started = time.monotonic()
        try:
            result = asyncio.run(
                pipeline.process(
                    articles,
                    cfg.sources,
                    period=period,
                    timezone=cfg.app.general.timezone,
                    article_scores=scores,
                )
            )
        except Exception as e:  # noqa: BLE001 - one broken arm must not end the comparison
            logger.error("%s failed: %s", label, e)
            continue
        elapsed = time.monotonic() - started

        content = result.content
        # The pipeline swallows LLM failures on purpose — `cyris run` would rather
        # ship excerpts than nothing. For a comparison that is the wrong trade: an
        # arm that never reached its model still renders a plausible digest, and
        # printing it as a row invites reading excerpt fallback as this model's
        # work. Zero calls is the tell, and it is not a result.
        if content.usage.api_calls == 0:
            logger.error(
                "%s made no API calls — every request failed and the pipeline fell back to "
                "excerpts. Re-run with --verbose for the provider's reason. Not comparable.",
                label,
            )
            continue

        rows.append(
            ArmDigest(
                label=label,
                content=content,
                wall_seconds=elapsed,
                neurons=content.usage.neurons,
                rendered=render(content),
            )
        )
    return rows
