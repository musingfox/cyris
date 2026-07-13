"""Export articles from store to Obsidian vault."""

import logging
import re
from pathlib import Path

from cyris.domain.models import StoredArticle

logger = logging.getLogger(__name__)


class ArticleExporter:
    """Export articles to Obsidian vault as markdown files."""

    def export_to_vault(
        self,
        articles: list[StoredArticle],
        vault_path: Path,
        folder: str = "Reading",
    ) -> list[Path]:
        """Export articles to vault as markdown files.

        Args:
            articles: Articles to export.
            vault_path: Destination vault directory.
            folder: Subfolder in vault.

        Returns:
            List of paths to written markdown files.

        Raises:
            ValueError: If vault_path doesn't exist.
        """
        if not vault_path.exists():
            raise ValueError(f"vault_path does not exist: {vault_path}")

        # Create folder if needed
        export_folder = vault_path / folder
        export_folder.mkdir(parents=True, exist_ok=True)

        written_paths = []

        for article in articles:
            # Sanitize filename
            filename = self._sanitize_filename(article.title)
            file_path = export_folder / f"{filename}.md"

            # Skip if already exists (idempotent)
            if file_path.exists():
                written_paths.append(file_path)
                continue

            # Generate markdown content
            content = self._generate_markdown(article)

            # Write file
            file_path.write_text(content, encoding="utf-8")
            logger.info("Exported article to %s", file_path)
            written_paths.append(file_path)

        return written_paths

    def _sanitize_filename(self, title: str) -> str:
        """Sanitize title for filename.

        Removes special chars like :/?*"<>|, max 100 chars, strips whitespace.
        """
        # Remove invalid filename characters
        sanitized = re.sub(r'[:/\\?*"<>|]', "", title)
        # Collapse multiple spaces
        sanitized = re.sub(r"\s+", " ", sanitized)
        # Strip whitespace
        sanitized = sanitized.strip()
        # Limit length
        if len(sanitized) > 100:
            sanitized = sanitized[:100].strip()
        return sanitized

    def _generate_markdown(self, article: StoredArticle) -> str:
        """Generate markdown content with YAML frontmatter."""
        # Build frontmatter
        frontmatter_lines = ["---"]
        frontmatter_lines.append(f"url: {article.url}")

        if article.author:
            frontmatter_lines.append(f"author: {article.author}")

        frontmatter_lines.append(f"published_at: {article.published_at.isoformat()}")
        frontmatter_lines.append(f"source_name: {article.source_name}")

        if article.source_tags:
            tags_str = ", ".join(article.source_tags)
            frontmatter_lines.append(f"source_tags: [{tags_str}]")

        frontmatter_lines.append("---")

        # Combine frontmatter and content
        content_parts = [
            "\n".join(frontmatter_lines),
            "",
            f"# {article.title}",
            "",
            article.content,
        ]

        return "\n".join(content_parts)
