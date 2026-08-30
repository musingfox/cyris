"""D1-specific behaviour. The shared contract lives in test_article_store.py."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fakes import CompoundSelectLimitedD1, SqliteD1

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
        triaged_at=now,
    )

    store.import_articles([stored])

    assert store.get_by_urls(["https://example.com/full"])[0] == stored


def test_human_stamps_survive_the_d1_backend(store: D1ArticleStore) -> None:
    """Vote similarity seeds from triaged_at; that stamp has to survive the backend."""
    now = datetime.now(UTC)

    def _stored(url: str, state: ArticleState, triaged: bool) -> StoredArticle:
        article = StoredArticle.from_article(_article(url, url), first_seen_at=now)
        article.state = state
        article.triaged_at = now - timedelta(days=1) if triaged else None
        return article

    store.import_articles(
        [
            *[_stored(f"https://example.com/a{i}", ArticleState.ACCEPTED, True) for i in range(5)],
            *[_stored(f"https://example.com/r{i}", ArticleState.REJECTED, True) for i in range(2)],
            # Rejected by the pipeline, not by a person: must not become training signal.
            _stored("https://example.com/pipeline", ArticleState.REJECTED, False),
        ]
    )

    stamped_up = [a for a in store.list_articles(state=ArticleState.ACCEPTED) if a.triaged_at]
    stamped_down = [a for a in store.list_articles(state=ArticleState.REJECTED) if a.triaged_at]

    assert len(stamped_up) == 5
    assert len(stamped_down) == 2


def test_architecture_no_longer_lists_cleaning_triaged_rows_as_outstanding() -> None:
    architecture = Path("docs/architecture.md").read_text()

    assert "| 15 | `cyris articles clean` deletes triaged rejected rows" not in architecture


def test_usage_is_logged_to_the_same_database(sample_digest_content) -> None:
    db = SqliteD1()
    sample_digest_content.usage.api_calls = 3

    append_usage_d1(sample_digest_content, client=db)

    rows = db.query("SELECT * FROM usage_log").rows
    assert len(rows) == 1
    assert rows[0]["digest_date"] == sample_digest_content.date
    assert rows[0]["api_calls"] == 3


def test_a_priced_model_writes_its_cost(sample_digest_content) -> None:
    db = SqliteD1()
    sample_digest_content.usage.api_calls = 3
    sample_digest_content.usage.model = "gemini-3.6-flash"
    sample_digest_content.usage.input_tokens = 1_000_000
    sample_digest_content.usage.output_tokens = 1_000_000

    append_usage_d1(sample_digest_content, client=db)

    assert db.query("SELECT * FROM usage_log").rows[0]["cost_usd"] == 4.5


def test_an_unpriced_model_omits_the_column_rather_than_breaking_the_insert(
    sample_digest_content,
) -> None:
    """`cost_usd` is NOT NULL DEFAULT 0, so a NULL here would fail the write."""
    db = SqliteD1()
    sample_digest_content.usage.api_calls = 3
    sample_digest_content.usage.model = "@cf/openai/gpt-oss-120b"

    append_usage_d1(sample_digest_content, client=db)

    row = db.query("SELECT * FROM usage_log").rows[0]
    assert row["cost_usd"] == 0
    assert row["model"] == "@cf/openai/gpt-oss-120b"  # what tells 0 from unknown


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
        status_code = 200

        def json(self) -> dict:
            return {"success": False, "errors": [{"message": "no such table"}]}

    monkeypatch.setattr(client._http, "post", lambda *a, **k: Response())

    with pytest.raises(D1Error, match="no such table"):
        client.query("SELECT 1")


def test_a_rejected_statement_reports_what_d1_said(monkeypatch) -> None:
    """D1 puts the reason in a 400's body; raise_for_status would discard it, and
    `no such table: sources` is the difference between a fix and a guess."""
    client = D1Client(account_id="a", database_id="b", api_token="c")

    class Response:
        status_code = 400

        def json(self) -> dict:
            return {"errors": [{"message": "no such table: sources: SQLITE_ERROR"}]}

    monkeypatch.setattr(client._http, "post", lambda *a, **k: Response())

    with pytest.raises(D1Error, match="no such table: sources"):
        client.query("SELECT 1 FROM sources")


def test_a_client_error_is_not_retried(monkeypatch) -> None:
    """Repeating a malformed request only delays the diagnosis."""
    client = D1Client(account_id="a", database_id="b", api_token="c")
    calls = []

    class Response:
        status_code = 400

        def json(self) -> dict:
            return {"errors": [{"message": "bad request"}]}

    def post(*_a, **_k):
        calls.append(1)
        return Response()

    monkeypatch.setattr(client._http, "post", post)

    with pytest.raises(D1Error):
        client.query("SELECT 1")
    assert len(calls) == 1


def test_scores_survive_a_batch_past_d1s_compound_select_ceiling() -> None:
    """A scoring run of more than five articles must still write every score.

    `update_scores` batched its rows as `SELECT ? UNION ALL SELECT ?`, and D1
    caps a compound SELECT at five terms. The D1Error escaped `score_in_batches`
    before the tag write, so every run lost both its scores and its tags to
    run_digest's "Scoring failed; continuing without scores".
    """
    store = D1ArticleStore(CompoundSelectLimitedD1())
    urls = [f"https://example.com/{n}" for n in range(12)]
    store.save([_article(url, n) for n, url in enumerate(urls)])

    # The scores themselves are the receipt, not the return count: stdlib
    # sqlite3 reports rowcount 0 for `UPDATE ... FROM`, where D1 counts the rows.
    store.update_scores({url: (float(n), "en") for n, url in enumerate(urls)})

    stored = {a.url: a.score for a in store.list_articles(state=None, limit=100)}
    assert [stored[url] for url in urls] == [float(n) for n in range(len(urls))]
