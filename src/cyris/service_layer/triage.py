"""Triage digest feedback to accept/reject articles."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from cyris.domain.models import FeedbackData, TriageResult
from cyris.learn.feedback import parse_digest_feedback
from cyris.service_layer.ports import ArticleRepository

if TYPE_CHECKING:
    # Single-impl output sink injected at runtime (no Protocol, per ports.py doctrine);
    # imported only for type hints so the service layer keeps no runtime adapters/ import.
    from cyris.adapters.output.article_export import ArticleExporter

logger = logging.getLogger(__name__)


def scan_digests(
    vault_path: Path, digest_folder: str = "Digests", limit: int = 7
) -> list[FeedbackData]:
    """Scan recent digest markdown files in vault for feedback.

    Args:
        vault_path: Path to user vault.
        digest_folder: Subfolder containing digests.
        limit: Maximum number of digests to scan (newest first).

    Returns:
        List of FeedbackData (one per digest), sorted by recency (newest first).

    Raises:
        ValueError: If vault_path/digest_folder doesn't exist.
        ValueError: If no digest files found.
    """
    digest_path = vault_path / digest_folder

    if not digest_path.exists():
        raise ValueError(f"Digest folder does not exist: {digest_path}")

    # Find all .md files
    digest_files = sorted(digest_path.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)

    if not digest_files:
        raise ValueError(f"No digest files found in {digest_path}")

    # Limit to newest N files
    digest_files = digest_files[:limit]

    feedback_list = []
    for digest_file in digest_files:
        try:
            feedback = parse_digest_feedback(digest_file)
            feedback_list.append(feedback)
            logger.info(
                "Parsed feedback from %s: %d articles", digest_file.name, len(feedback.articles)
            )
        except Exception as e:
            logger.warning("Failed to parse %s: %s", digest_file.name, e)

    return feedback_list


def process_feedback(feedback_list: list[FeedbackData], store: ArticleRepository) -> TriageResult:
    """Process feedback to accept articles marked for deep read.

    Args:
        feedback_list: List of FeedbackData from digests.
        store: ArticleRepository to update.

    Returns:
        TriageResult with accepted_count, skipped_count, accepted_urls.
    """
    accepted_count = 0
    skipped_count = 0
    accepted_urls = []

    for feedback in feedback_list:
        for article_feedback in feedback.articles:
            # Only process articles marked with deep_read
            if not article_feedback.deep_read:
                continue

            # Must have URL
            if article_feedback.url is None:
                logger.warning("Skipping article without URL: %s", article_feedback.title)
                skipped_count += 1
                continue

            # Update article state
            success = store.accept([article_feedback.url])

            if success:
                accepted_count += 1
                accepted_urls.append(article_feedback.url)
                logger.info("Accepted: %s", article_feedback.title)
            else:
                skipped_count += 1
                logger.warning("Article not found in store: %s", article_feedback.url)

    return TriageResult(
        accepted_count=accepted_count,
        skipped_count=skipped_count,
        accepted_urls=accepted_urls,
    )


def export_accepted(
    store: ArticleRepository,
    exporter: ArticleExporter,
    user_vault_path: Path,
    accepted_urls: list[str],
    folder: str = "Reading",
) -> list[Path]:
    """Export newly accepted articles to user vault.

    Args:
        store: ArticleRepository to query.
        exporter: ArticleExporter for writing markdown.
        user_vault_path: Destination vault path.
        accepted_urls: URLs of articles to export (from this triage run).
        folder: Subfolder for export.

    Returns:
        List of written file paths.
    """
    if not accepted_urls:
        logger.info("No accepted articles to export")
        return []

    articles = store.get_by_urls(accepted_urls)

    if not articles:
        logger.info("No matching articles found in store")
        return []

    paths = exporter.export_to_vault(articles, user_vault_path, folder=folder)
    logger.info("Exported %d accepted articles to %s/%s", len(paths), user_vault_path, folder)

    return paths
