"""D1-specific behaviour. The shared contract lives in test_article_store.py."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fakes import SqliteD1

from cyris.adapters.output.usage_log import append_usage_d1
from cyris.adapters.store import ArticleStore
from cyris.adapters.store.d1 import D1Client, D1Error, chunk_rows
from cyris.adapters.store.d1_store import D1ArticleStore
from cyris.bootstrap import build_store
from cyris.config import AppConfig, Config, StoreConfig
from cyris.domain.models import Article, ArticleState, StoredArticle, Tier


@pytest.fixture
def store() -> D1ArticleStore:
    return D1ArticleStore(SqliteD1())


def _article(url: str, article_id: int | str = 1) -> Article:
    return Article(
        id=article_id,
        title="Title",
        url=url,
        content="Content",
        published_at=datetime.now(UTC),
        source_name="Source",
        source_tier=Tier.FILTER,
    )


def test_save_batches_past_the_bound_parameter_limit(store: D1ArticleStore) -> None:
    """D1 binds at most 100 parameters, and one article already needs 19."""
    articles = [_article(f"https://example.com/{i}", i) for i in range(50)]

    result = store.save(articles)

    assert result.saved_count == 50
    assert len(store.list_articles(state=None, limit=100)) == 50


def test_chunk_rows_respects_the_parameter_budget() -> None:
    assert all(len(c) * 19 <= 100 for c in chunk_rows(list(range(50)), 19))


def test_dedup_is_not_limited_to_a_window(store: D1ArticleStore) -> None:
    """The JSON store only scans 8 days back; a URL primary key has no horizon."""
    old = datetime.now(UTC) - timedelta(days=90)
    store.save([_article("https://example.com/a")], now=old)

    result = store.save([_article("https://example.com/a")])

    assert result.saved_count == 0
    assert result.skipped_count == 1


def test_import_preserves_state_and_never_overwrites(store: D1ArticleStore) -> None:
    """Re-running a migration must not undo a decision already made in D1."""
    stored = StoredArticle.from_article(
        _article("https://example.com/kept"), first_seen_at=datetime.now(UTC)
    )
    stored.state = ArticleState.ACCEPTED
    stored.score = 88.0
    stored.triaged_at = datetime.now(UTC)

    assert store.import_articles([stored]) == 1
    store.reject(["https://example.com/kept"], reason="changed my mind")

    assert store.import_articles([stored]) == 0
    assert store.get_by_urls(["https://example.com/kept"])[0].state == ArticleState.REJECTED


def test_import_round_trips_every_field(store: D1ArticleStore) -> None:
    now = datetime.now(UTC)
    stored = StoredArticle(
        url="https://example.com/full",
        original_id="newsletter-abc",
        title="標題",
        content="內容",
        author="Author",
        published_at=now,
        source_name="Source",
        source_tier=Tier.SUMMARIZE,
        source_tags=["tech", "ai"],
        ref_urls=["https://example.com/ref"],
        state=ArticleState.AWAITING_TRIAGE,
        first_seen_at=now,
        digest_date="2026-08-25",
        rejection_reason=None,
        score=71.5,
        language="zh",
        scored_at=now,
        triaged_at=now,
        exported_at=now,
    )

    store.import_articles([stored])

    assert store.get_by_urls(["https://example.com/full"])[0] == stored


def test_usage_is_logged_to_the_same_database(sample_digest_content) -> None:
    db = SqliteD1()
    sample_digest_content.usage.api_calls = 3

    append_usage_d1(sample_digest_content, client=db)

    rows = db.query("SELECT * FROM usage_log").rows
    assert len(rows) == 1
    assert rows[0]["digest_date"] == sample_digest_content.date
    assert rows[0]["api_calls"] == 3


def test_usage_with_no_api_calls_is_not_logged(sample_digest_content) -> None:
    db = SqliteD1()
    sample_digest_content.usage.api_calls = 0

    append_usage_d1(sample_digest_content, client=db)

    assert db.query("SELECT * FROM usage_log").rows == []


def _config(tmp_path: Path, **store_kwargs) -> Config:
    app = AppConfig(store=StoreConfig(**store_kwargs))
    app.agent_vault.path = tmp_path
    return Config(app=app, sources={})


def test_build_store_defaults_to_the_local_files(tmp_path: Path) -> None:
    assert isinstance(build_store(_config(tmp_path)), ArticleStore)


def test_build_store_returns_d1_when_selected(tmp_path: Path) -> None:
    cfg = _config(tmp_path, backend="d1", database_id="db", account_id="acct", api_token="tok")

    assert isinstance(build_store(cfg), D1ArticleStore)


def test_d1_credentials_are_required_before_a_run(tmp_path: Path) -> None:
    cfg = _config(tmp_path, backend="d1", account_id="acct", api_token="tok")

    with pytest.raises(ValueError, match="database_id"):
        cfg.validate_required_keys()


def test_api_errors_surface_instead_of_returning_empty(monkeypatch) -> None:
    """A failed query must not look like an empty table — that would delete data."""
    client = D1Client(account_id="a", database_id="b", api_token="c")

    class Response:
        def raise_for_status(self) -> None: ...

        def json(self) -> dict:
            return {"success": False, "errors": [{"message": "no such table"}]}

    monkeypatch.setattr(client._http, "post", lambda *a, **k: Response())

    with pytest.raises(D1Error, match="no such table"):
        client.query("SELECT 1")
