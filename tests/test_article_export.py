"""Tests for article export functionality."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cyris.adapters.output.article_export import ArticleExporter
from cyris.domain.models import ArticleState, StoredArticle, Tier


@pytest.fixture
def exporter() -> ArticleExporter:
    """Create exporter instance."""
    return ArticleExporter()


@pytest.fixture
def sample_stored_articles() -> list[StoredArticle]:
    """Generate sample stored articles for testing."""
    now = datetime.now(UTC)
    return [
        StoredArticle(
            url="https://example.com/1",
            original_id=1,
            title="First Article",
            content="Content 1",
            published_at=now,
            source_name="Source A",
            source_tier=Tier.FILTER,
            first_seen_at=now,
            state=ArticleState.ACCEPTED,
            author="Author 1",
        ),
        StoredArticle(
            url="https://example.com/2",
            original_id=2,
            title="Second Article",
            content="Content 2",
            published_at=now,
            source_name="Source B",
            source_tier=Tier.SUMMARIZE,
            first_seen_at=now,
            state=ArticleState.ACCEPTED,
        ),
        StoredArticle(
            url="https://example.com/3",
            original_id=3,
            title="Third Article",
            content="Content 3",
            published_at=now,
            source_name="Source C",
            source_tier=Tier.FILTER,
            first_seen_at=now,
            state=ArticleState.ACCEPTED,
        ),
    ]


def test_export_creates_files(
    exporter: ArticleExporter, sample_stored_articles: list[StoredArticle], tmp_path: Path
) -> None:
    """Export creates markdown files for all articles."""
    paths = exporter.export_to_vault(sample_stored_articles, tmp_path, folder="Reading")
    assert len(paths) == 3
    assert all(p.exists() for p in paths)
    assert all(p.suffix == ".md" for p in paths)


def test_export_sanitized_filename(exporter: ArticleExporter, tmp_path: Path) -> None:
    """Export sanitizes special characters in filename."""
    now = datetime.now(UTC)
    article = StoredArticle(
        url="https://example.com/x",
        original_id=1,
        title="Test: Article / Name?",
        content="C",
        published_at=now,
        source_name="S",
        source_tier=Tier.FILTER,
        first_seen_at=now,
        state=ArticleState.ACCEPTED,
    )
    paths = exporter.export_to_vault([article], tmp_path)
    assert ":" not in paths[0].name
    assert "/" not in paths[0].name
    assert "?" not in paths[0].name


def test_export_markdown_frontmatter(exporter: ArticleExporter, tmp_path: Path) -> None:
    """Export generates correct markdown with frontmatter."""
    now = datetime.now(UTC)
    article = StoredArticle(
        url="https://example.com/t",
        original_id=1,
        title="T",
        content="Body text here",
        author="A",
        published_at=now,
        source_name="S",
        source_tier=Tier.FILTER,
        source_tags=["tech"],
        first_seen_at=now,
        state=ArticleState.ACCEPTED,
    )
    paths = exporter.export_to_vault([article], tmp_path)
    content = paths[0].read_text()
    assert "---" in content
    assert "url: https://example.com/t" in content
    assert "author: A" in content
    assert "Body text here" in content


def test_export_invalid_vault_path(
    exporter: ArticleExporter, sample_stored_articles: list[StoredArticle]
) -> None:
    """Export raises ValueError for non-existent vault path."""
    with pytest.raises(ValueError, match="vault_path"):
        exporter.export_to_vault(sample_stored_articles, Path("/nonexistent/path"))


def test_export_idempotent(
    exporter: ArticleExporter, sample_stored_articles: list[StoredArticle], tmp_path: Path
) -> None:
    """Exporting same articles twice should not create duplicate files."""
    paths1 = exporter.export_to_vault(sample_stored_articles, tmp_path)
    paths2 = exporter.export_to_vault(sample_stored_articles, tmp_path)
    assert paths1 == paths2
