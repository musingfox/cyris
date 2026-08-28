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


def test_save_is_idempotent_and_normalizes_tags() -> None:
    db = SqliteD1()
    store = D1TagStore(db)

    store.save({"u1": ["  Rust "]})
    store.save({"u1": ["  Rust "]})

    assert db.query("SELECT name FROM tags").rows == [{"name": "rust"}]
    assert db.query("SELECT article_url, tag FROM article_tags").rows == [
        {"article_url": "u1", "tag": "rust"}
    ]


def test_architecture_lists_tag_residency() -> None:
    architecture = Path("docs/architecture.md").read_text()

    assert "D1 `tags`" in architecture
    assert "D1 `article_tags`" in architecture
