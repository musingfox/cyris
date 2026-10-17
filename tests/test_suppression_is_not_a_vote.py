"""A suppressed article stays an ordinary pending article: no later pass reads it as a vote."""

from datetime import timedelta
from pathlib import Path

import pytest
from fakes import SqliteD1
from test_suppression_clock_run import SPREAD, T0, A, _fixed_clock, _vectors, _world

from cyris.adapters.store.blind_labels import D1BlindLabels
from cyris.adapters.store.d1_store import D1ArticleStore
from cyris.domain.models import ArticleState, Tier
from cyris.service_layer.run_digest import RunOptions, run_digest
from cyris.service_layer.vote_similarity import _voted

pytestmark = pytest.mark.integration


async def _suppressing_run(tmp_path: Path, monkeypatch) -> SqliteD1:
    _fixed_clock(monkeypatch, T0)
    db = SqliteD1()
    deps, recorded, _ = _world(
        tmp_path,
        T0,
        _vectors(0.995, SPREAD),
        db=db,
        store=D1ArticleStore(db),
        tier=Tier.FILTER,
    )
    await run_digest(deps, RunOptions(period="morning"))
    assert recorded[-1]["suppressed"] == 1
    return db


def _row(db) -> list[dict]:
    return db.query(
        "SELECT state, triaged_at, rejection_reason FROM stored_articles WHERE url = ?", [A]
    ).rows


async def test_the_suppressed_article_stays_pending_and_unstamped(tmp_path, monkeypatch) -> None:
    db = await _suppressing_run(tmp_path, monkeypatch)

    assert _row(db) == [{"state": "pending", "triaged_at": None, "rejection_reason": None}]


async def test_no_later_vote_pass_seeds_from_the_suppressed_article(tmp_path, monkeypatch) -> None:
    db = await _suppressing_run(tmp_path, monkeypatch)

    rejected = _voted(D1ArticleStore(db), ArticleState.REJECTED, 100)

    assert A not in {a.url for a in rejected}


async def test_the_pipeline_reject_stratum_of_the_blind_pool_is_unchanged(
    tmp_path, monkeypatch
) -> None:
    _fixed_clock(monkeypatch, T0)
    db = SqliteD1()
    deps, recorded, _ = _world(
        tmp_path, T0, _vectors(0.995, SPREAD), db=db, store=D1ArticleStore(db), tier=Tier.FILTER
    )
    rejects = lambda pool: [r.url for r in pool if r.state is ArticleState.REJECTED]  # noqa: E731
    before = rejects(D1BlindLabels(db).pool(T0 - timedelta(days=1)))

    await run_digest(deps, RunOptions(period="morning"))

    after = rejects(D1BlindLabels(db).pool(T0 - timedelta(days=1)))
    assert recorded[-1]["suppressed"] == 1
    assert A not in after
    assert A not in before
