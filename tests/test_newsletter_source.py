"""Tests for NewsletterArchiveSource adapter."""

import json
from datetime import datetime

import pytest

from cyris.adapters.fetch.newsletter_source import NewsletterArchiveSource


@pytest.mark.asyncio
async def test_fetch_articles_loads_archive(tmp_path):
    """Test that fetch_articles loads articles from archive within time window."""
    archive_dir = tmp_path / "newsletters"
    archive_dir.mkdir()

    # Create test archive files
    articles_day1 = [
        {
            "id": "nl-1",
            "title": "Article 1",
            "url": "https://example.com/1",
            "content": "Content 1",
            "published_at": "2026-03-18T08:00:00",
            "source_name": "Newsletter",
            "source_tier": "summarize",
        }
    ]
    articles_day2 = [
        {
            "id": "nl-2",
            "title": "Article 2",
            "url": "https://example.com/2",
            "content": "Content 2",
            "published_at": "2026-03-19T10:00:00",
            "source_name": "Newsletter",
            "source_tier": "summarize",
        }
    ]
    (archive_dir / "2026-03-18.json").write_text(json.dumps(articles_day1))
    (archive_dir / "2026-03-19.json").write_text(json.dumps(articles_day2))

    source = NewsletterArchiveSource(archive_dir)
    result = await source.fetch_articles(
        after=datetime(2026, 3, 18),
        before=datetime(2026, 3, 20),
        sources={},
    )

    assert len(result) == 2
    assert result[0].title == "Article 1"
    assert result[1].title == "Article 2"


@pytest.mark.asyncio
async def test_fetch_articles_filters_by_time(tmp_path):
    """Test that fetch_articles filters articles by time window."""
    archive_dir = tmp_path / "newsletters"
    archive_dir.mkdir()

    articles = [
        {
            "id": "nl-1",
            "title": "Too Early",
            "url": "https://example.com/1",
            "content": "Content 1",
            "published_at": "2026-03-17T08:00:00",
            "source_name": "Newsletter",
            "source_tier": "summarize",
        },
        {
            "id": "nl-2",
            "title": "Within Window",
            "url": "https://example.com/2",
            "content": "Content 2",
            "published_at": "2026-03-18T10:00:00",
            "source_name": "Newsletter",
            "source_tier": "summarize",
        },
        {
            "id": "nl-3",
            "title": "Too Late",
            "url": "https://example.com/3",
            "content": "Content 3",
            "published_at": "2026-03-20T10:00:00",
            "source_name": "Newsletter",
            "source_tier": "summarize",
        },
    ]
    (archive_dir / "2026-03-18.json").write_text(json.dumps(articles))

    source = NewsletterArchiveSource(archive_dir)
    result = await source.fetch_articles(
        after=datetime(2026, 3, 18),
        before=datetime(2026, 3, 20),
        sources={},
    )

    # Only the article within window should be included
    assert len(result) == 1
    assert result[0].title == "Within Window"


@pytest.mark.asyncio
async def test_fetch_articles_missing_path(tmp_path):
    """Test that fetch_articles returns empty list when path doesn't exist."""
    nonexistent = tmp_path / "nonexistent"

    source = NewsletterArchiveSource(nonexistent)
    result = await source.fetch_articles(
        after=datetime(2026, 3, 18),
        before=datetime(2026, 3, 20),
        sources={},
    )

    assert result == []


@pytest.mark.asyncio
async def test_fetch_articles_handles_malformed_file(tmp_path):
    """Test that fetch_articles skips malformed JSON files."""
    archive_dir = tmp_path / "newsletters"
    archive_dir.mkdir()

    # Create valid and invalid files
    valid_articles = [
        {
            "id": "nl-1",
            "title": "Valid",
            "url": "https://example.com/1",
            "content": "Content",
            "published_at": "2026-03-18T08:00:00",
            "source_name": "Newsletter",
            "source_tier": "summarize",
        }
    ]
    (archive_dir / "valid.json").write_text(json.dumps(valid_articles))
    (archive_dir / "invalid.json").write_text("not valid json {")

    source = NewsletterArchiveSource(archive_dir)
    result = await source.fetch_articles(
        after=datetime(2026, 3, 18),
        before=datetime(2026, 3, 20),
        sources={},
    )

    # Should only return valid articles
    assert len(result) == 1
    assert result[0].title == "Valid"


@pytest.mark.asyncio
async def test_health_check_path_exists(tmp_path):
    """Test that health_check returns True when path exists."""
    archive_dir = tmp_path / "newsletters"
    archive_dir.mkdir()

    source = NewsletterArchiveSource(archive_dir)
    result = await source.health_check()

    assert result is True


@pytest.mark.asyncio
async def test_health_check_path_missing(tmp_path):
    """Test that health_check returns False when path doesn't exist."""
    nonexistent = tmp_path / "nonexistent"

    source = NewsletterArchiveSource(nonexistent)
    result = await source.health_check()

    assert result is False
