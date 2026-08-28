from pathlib import Path

from fakes import FakeLLM, SqliteD1

from cyris.adapters.store.tags import D1TagStore
from cyris.domain.models import Article, Tier
from cyris.service_layer.digest_pipeline import DigestPipeline


def _news(article_id: int, url: str) -> Article:
    from datetime import UTC, datetime

    return Article(
        id=article_id,
        title=f"News {article_id}",
        url=url,
        content="News content",
        published_at=datetime.now(UTC),
        source_name="Source",
        source_tier=Tier.FILTER,
        source_tags=["news"],
    )


async def test_cluster_tags_persist_for_every_member_normalized() -> None:
    db = SqliteD1()
    pipeline = DigestPipeline(
        FakeLLM(
            '{"clusters": [{"heading": "H", "summary": "S", '
            '"article_ids": [1, 2], "tags": ["AI Policy", "ai policy"]}]}'
        )
    )

    result = await pipeline.process([_news(1, "u1"), _news(2, "u2")], {})
    D1TagStore(db).save(result.url_to_tags)

    assert db.query("SELECT name FROM tags").rows == [{"name": "ai policy"}]
    assert db.query("SELECT article_url, tag FROM article_tags ORDER BY article_url").rows == [
        {"article_url": "u1", "tag": "ai policy"},
        {"article_url": "u2", "tag": "ai policy"},
    ]


def test_save_is_idempotent_normalizes_tags_and_reports_rows_written() -> None:
    db = SqliteD1()
    store = D1TagStore(db)

    first = store.save({"u1": ["  Rust "]})
    second = store.save({"u1": ["  Rust "]})

    assert first == 2  # 1 tags row + 1 article_tags row
    assert second == 1  # vocabulary ignored; membership replaced to refresh tagged_at
    assert db.query("SELECT name FROM tags").rows == [{"name": "rust"}]
    assert db.query("SELECT article_url, tag FROM article_tags").rows == [
        {"article_url": "u1", "tag": "rust"}
    ]


def test_resave_refreshes_tagged_at_but_keeps_created_at() -> None:
    db = SqliteD1()
    store = D1TagStore(db)
    store.save({"u1": ["rust"]})
    db.query("UPDATE article_tags SET tagged_at = 'stale'")
    db.query("UPDATE tags SET created_at = 'first-seen'")

    store.save({"u1": ["rust"]})

    assert db.query("SELECT tagged_at FROM article_tags").rows[0]["tagged_at"] != "stale"
    assert db.query("SELECT created_at FROM tags").rows[0]["created_at"] == "first-seen"


class CountingD1:
    """SqliteD1 that counts queries, so a per-row-write regression fails a test."""

    def __init__(self) -> None:
        self._inner = SqliteD1()
        self.query_count = 0

    def query(self, sql, params=None):
        self.query_count += 1
        return self._inner.query(sql, params)


def test_save_batches_writes_within_the_bound_param_budget() -> None:
    db = CountingD1()
    url_to_tags = {f"u{i}": [f"tag{i}a", f"tag{i}b"] for i in range(60)}

    written = D1TagStore(db).save(url_to_tags)
    queries = db.query_count

    assert written == 240  # 120 vocabulary rows + 120 membership rows
    # 3 vocabulary statements (120 rows, 50 per) + 4 membership statements (120 rows, 33 per)
    assert queries == 7


def test_architecture_lists_tag_residency() -> None:
    architecture = Path("docs/architecture.md").read_text()

    assert "D1 `tags`" in architecture
    assert "D1 `article_tags`" in architecture
