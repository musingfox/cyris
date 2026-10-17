"""A run honours the holds set by earlier runs, on their own seven-day clock."""

import json
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fakes import FakeLLM, SqliteD1
from test_run_digest import FakeSource, make_deps

import cyris.adapters.store.article_store as article_store_module
import cyris.service_layer.run_digest as run_digest_module
from cyris.adapters.store.d1 import D1Error
from cyris.adapters.store.d1_store import D1ArticleStore
from cyris.adapters.store.suppressions import D1Suppressions
from cyris.domain.models import Article, ArticleState, Tier
from cyris.domain.similarity import normalize
from cyris.service_layer.run_digest import RunOptions, run_digest

pytestmark = pytest.mark.integration

MODEL = "gemini-embedding-001"
T0 = datetime(2026, 10, 17, 6, 0, tzinfo=UTC)
A = "https://example.com/a"
D = "https://example.com/disliked"


def _fixed_clock(monkeypatch, when: datetime, *also) -> None:
    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return when if tz is None else when.astimezone(tz)

    for module in (run_digest_module, *also):
        monkeypatch.setattr(module, "datetime", Fixed)


def _article(article_id: int, title: str, url: str, when: datetime, tier=Tier.SUMMARIZE) -> Article:
    return Article(
        id=article_id,
        title=title,
        url=url,
        content="Some text.",
        published_at=when - timedelta(hours=1),
        source_name="NotifySource",
        source_tier=tier,
        source_tags=["tech"],
    )


class _Embedder:
    """Titles to vectors, normalized as a real embedder returns them."""

    def __init__(self, vectors: dict[str, list[float]], *, fail: bool = False) -> None:
        self._vectors = vectors
        self._fail = fail
        self.usage = SimpleNamespace(as_dict=lambda: {"embedded": 0})

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if self._fail:
            raise RuntimeError("embedding down")
        return [normalize(self._vectors[t]) for t in texts]


def _down(cosine: float) -> list[float]:
    """A vector whose cosine to the downvoted seed [0, 1, 0] is `cosine`."""
    return [0.0, cosine, math.sqrt(1 - cosine * cosine)]


def _llm() -> FakeLLM:
    section = {"heading": "AI", "summary": "S", "articles": [{"id": 0}]}
    return FakeLLM([json.dumps({"scores": []}), json.dumps({"sections": [section]})])


def _world(
    tmp_path: Path,
    when: datetime,
    vectors: dict[str, list[float]],
    *,
    db=None,
    enabled: bool = True,
    fail: bool = False,
    suppressions=None,
    store=None,
    tier=Tier.SUMMARIZE,
    extra=(),
):
    """Two votes, and candidates A (titled 'Outlier') plus nine others, judged at `when`."""
    candidates = (
        [_article(1, "Outlier", A, when, tier)]
        + [
            _article(10 + i, f"Tech {i}", f"https://example.com/t{i}", when, tier)
            for i in range(len(SPREAD))
        ]
        + list(extra)
    )
    deps, _ = make_deps(tmp_path, _llm(), FakeSource(candidates))
    deps.cfg.app.vote_similarity.enabled = enabled
    db = db or SqliteD1()
    if store is not None:
        deps = replace(deps, store=store)
    votes = [
        _article(20, "Liked before", "https://example.com/liked", when),
        _article(21, "Disliked before", "https://example.com/disliked", when),
    ]
    deps.store.save(votes + candidates, now=when - timedelta(minutes=5))
    deps.store.accept(["https://example.com/liked"])
    deps.store.reject(["https://example.com/disliked"], "not_interested")
    deps.store.update_triage_timestamp(["https://example.com/liked"], when)
    deps.store.update_triage_timestamp(["https://example.com/disliked"], when)
    recorded: list[dict] = []
    deps = replace(
        deps,
        embedder=_Embedder(vectors, fail=fail),
        suppressions=suppressions or D1Suppressions(db, MODEL),
        record_similarity=None,
        record_run=recorded.append,
    )
    return deps, recorded, db


def _vectors(a: float, others: list[float]) -> dict[str, list[float]]:
    vectors = {"Liked before": [1.0, 0.0, 0.0], "Disliked before": [0.0, 1.0, 0.0]}
    vectors["Outlier"] = _down(a)
    for i, cosine in enumerate(others):
        vectors[f"Tech {i}"] = _down(cosine)
    return vectors


SPREAD = [0.10, 0.12, 0.14, 0.16, 0.18, 0.20, 0.22, 0.24, 0.26]
ALL_HIGH = [0.990, 0.991, 0.992, 0.993, 0.994, 0.996, 0.997, 0.998, 0.999]


async def test_a_hold_outlasts_the_run_that_judged_it_not_its_judging(
    tmp_path: Path, monkeypatch
) -> None:
    _fixed_clock(monkeypatch, T0)
    deps, _, db = _world(tmp_path, T0, _vectors(0.995, SPREAD))
    await run_digest(deps, RunOptions(period="morning"))

    later = T0 + timedelta(days=1)
    _fixed_clock(monkeypatch, later)
    deps, recorded, _ = _world(tmp_path / "2", later, _vectors(0.995, ALL_HIGH), db=db)
    await run_digest(deps, RunOptions(period="morning"))

    [summary] = recorded[-1:]
    assert summary["suppression_honoured"] == 1
    assert summary["suppressed"] == 0
    assert "vote_similarity_skipped" not in summary
    assert A in {a.url for a in deps.store.list_articles(state=ArticleState.PENDING, limit=100)}


async def test_a_hold_expires_seven_days_after_its_run(tmp_path: Path, monkeypatch) -> None:
    _fixed_clock(monkeypatch, T0)
    deps, _, db = _world(tmp_path, T0, _vectors(0.995, SPREAD))
    await run_digest(deps, RunOptions(period="morning"))

    expired = T0 + timedelta(days=8)
    _fixed_clock(monkeypatch, expired)
    deps, recorded, _ = _world(tmp_path / "3", expired, _vectors(0.995, ALL_HIGH), db=db)
    await run_digest(deps, RunOptions(period="morning"))

    assert recorded[-1]["suppressed"] == 0
    assert recorded[-1]["suppression_honoured"] == 0
    assert A in {a.url for a in deps.store.list_articles(state=ArticleState.PENDING, limit=100)}


async def test_a_hold_still_applies_when_judging_cannot_embed(tmp_path: Path, monkeypatch) -> None:
    _fixed_clock(monkeypatch, T0)
    deps, _, db = _world(tmp_path, T0, _vectors(0.995, SPREAD))
    await run_digest(deps, RunOptions(period="morning"))

    later = T0 + timedelta(days=1)
    _fixed_clock(monkeypatch, later)
    deps, recorded, _ = _world(tmp_path / "2", later, {}, db=db, fail=True)
    await run_digest(deps, RunOptions(period="morning"))

    [summary] = recorded[-1:]
    assert summary["suppression_honoured"] == 1
    assert summary["vote_similarity_skipped"].startswith("embedding failed")


async def test_a_disabled_vote_pass_reads_no_hold(tmp_path: Path, monkeypatch) -> None:
    _fixed_clock(monkeypatch, T0)
    deps, _, db = _world(tmp_path, T0, _vectors(0.995, SPREAD))
    await run_digest(deps, RunOptions(period="morning"))

    later = T0 + timedelta(days=1)
    _fixed_clock(monkeypatch, later)
    deps, recorded, _ = _world(
        tmp_path / "2", later, _vectors(0.995, ALL_HIGH), db=db, enabled=False
    )
    await run_digest(deps, RunOptions(period="morning"))

    assert "suppression_honoured" not in recorded[-1]
    assert A in {a.url for a in deps.store.list_articles(state=ArticleState.PENDING, limit=100)}


class _BrokenReads:
    """A hold store whose read fails, and whose writes are the real ones."""

    def __init__(self, real: D1Suppressions) -> None:
        self._real = real

    def active(self, now: str):
        raise D1Error("boom")

    def record(self, run_at, period, rows):
        self._real.record(run_at, period, rows)


async def test_a_failed_hold_read_leaves_this_runs_own_judging(tmp_path: Path, monkeypatch) -> None:
    _fixed_clock(monkeypatch, T0)
    db = SqliteD1()
    deps, recorded, _ = _world(
        tmp_path,
        T0,
        _vectors(0.995, SPREAD),
        db=db,
        suppressions=_BrokenReads(D1Suppressions(db, MODEL)),
    )

    await run_digest(deps, RunOptions(period="morning"))

    [summary] = recorded
    assert summary["suppression_read_error"] == "boom"
    assert summary["suppressed"] == 1
    assert summary["received"] == 9


async def test_a_hold_still_applies_when_the_run_has_no_embedder(
    tmp_path: Path, monkeypatch
) -> None:
    processed: list[set[str]] = []
    process = run_digest_module.DigestPipeline.process

    async def spy(self, articles, *args, **kwargs):
        processed.append({a.url for a in articles})
        return await process(self, articles, *args, **kwargs)

    monkeypatch.setattr(run_digest_module.DigestPipeline, "process", spy)
    _fixed_clock(monkeypatch, T0)
    deps, _, db = _world(tmp_path, T0, _vectors(0.995, SPREAD))
    await run_digest(deps, RunOptions(period="morning"))

    later = T0 + timedelta(days=1)
    _fixed_clock(monkeypatch, later)
    deps, recorded, _ = _world(tmp_path / "2", later, _vectors(0.995, ALL_HIGH), db=db)
    await run_digest(replace(deps, embedder=None), RunOptions(period="morning"))

    [summary] = recorded[-1:]
    assert summary["suppression_honoured"] == 1
    assert A not in processed[-1]
    assert A in {a.url for a in deps.store.list_articles(state=ArticleState.PENDING, limit=100)}


async def test_a_held_candidate_judged_again_counts_as_judged_not_as_honoured(
    tmp_path: Path, monkeypatch
) -> None:
    _fixed_clock(monkeypatch, T0)
    deps, _, db = _world(tmp_path, T0, _vectors(0.995, SPREAD))
    await run_digest(deps, RunOptions(period="morning"))

    later = T0 + timedelta(days=1)
    _fixed_clock(monkeypatch, later)
    deps, recorded, _ = _world(tmp_path / "2", later, _vectors(0.995, SPREAD), db=db)
    await run_digest(deps, RunOptions(period="morning"))

    [summary] = recorded[-1:]
    assert summary["suppressed"] == 1
    assert summary["suppression_honoured"] == 0


async def test_a_hold_ends_once_a_human_has_acted_on_the_article(
    tmp_path: Path, monkeypatch
) -> None:
    processed: list[set[str]] = []
    process = run_digest_module.DigestPipeline.process

    async def spy(self, articles, *args, **kwargs):
        processed.append({a.url for a in articles})
        return await process(self, articles, *args, **kwargs)

    monkeypatch.setattr(run_digest_module.DigestPipeline, "process", spy)
    _fixed_clock(monkeypatch, T0)
    # Articles and holds in one D1, as in production: the live-hold query reads the
    # article's triaged_at.
    db = SqliteD1()
    store = D1ArticleStore(db)
    deps, _, _ = _world(tmp_path, T0, _vectors(0.995, SPREAD), db=db, store=store)
    await run_digest(deps, RunOptions(period="morning"))

    later = T0 + timedelta(days=1)
    _fixed_clock(monkeypatch, later)
    deps, recorded, _ = _world(tmp_path / "2", later, _vectors(0.995, ALL_HIGH), db=db, store=store)
    deps.store.accept([A])
    deps.store.update_triage_timestamp([A], later - timedelta(minutes=1))
    await run_digest(deps, RunOptions(period="morning", force=True))

    [summary] = recorded[-1:]
    assert summary["suppression_honoured"] == 0
    assert A in processed[-1]


async def test_a_capped_run_accounts_for_every_row_the_cap_kept(
    tmp_path: Path, monkeypatch
) -> None:
    _fixed_clock(monkeypatch, T0)
    deps, _, db = _world(tmp_path, T0, _vectors(0.995, SPREAD))
    await run_digest(deps, RunOptions(period="morning"))

    later = T0 + timedelta(days=1)
    # The store's clock too, or the run's re-save adds a second copy of each row.
    _fixed_clock(monkeypatch, later, article_store_module)
    oldest = _article(30, "Cut by the cap", "https://example.com/old", later - timedelta(hours=2))
    deps, recorded, _ = _world(
        tmp_path / "2", later, _vectors(0.18, [0.995, *SPREAD[1:]]), db=db, extra=[oldest]
    )
    deps.cfg.app.digest.max_articles_per_digest = 1 + len(SPREAD)
    await run_digest(deps, RunOptions(period="morning"))

    [summary] = recorded[-1:]
    assert (summary["suppressed"], summary["suppression_honoured"]) == (1, 1)
    assert (
        summary["received"] + summary.get("suppressed", 0) + summary.get("suppression_honoured", 0)
        == deps.cfg.app.digest.max_articles_per_digest
    )
