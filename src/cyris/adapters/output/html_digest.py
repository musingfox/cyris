"""HTML digest output writer for newspaper-style rendering."""

import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from cyris.domain.models import DigestContent, DigestItem


class HtmlDigestWriter:
    """Renders DigestContent as a newspaper-style HTML page."""

    def __init__(
        self,
        output_dir: Path,
        promote_worker_url: str = "",
        promote_token: str = "",
    ):
        """Initialize writer with output directory.

        Args:
            output_dir: Directory to write HTML digests
            promote_worker_url: Promote Worker base URL; empty disables promote buttons
            promote_token: Bearer token for the promote Worker
        """
        self.output_dir = Path(output_dir)
        self.promote_worker_url = promote_worker_url.rstrip("/")
        self.promote_token = promote_token

        # Load templates from package-relative templates/ directory
        template_dir = Path(__file__).parent / "templates"
        self.env = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=select_autoescape(["html", "xml"]),
        )

    def render(self, content: DigestContent) -> str:
        """Transform DigestContent into complete HTML document.

        Args:
            content: Digest content to render

        Returns:
            Complete HTML string with inline styles

        Raises:
            jinja2.TemplateNotFound: If template is missing
        """
        template = self.env.get_template("digest.html.j2")

        # Extract lead story with fallback chain
        lead_story: DigestItem | None = None
        featured_articles: list[DigestItem] = []

        # Try to get lead from featured_articles[0].items[0]
        if content.featured_articles and content.featured_articles[0].items:
            lead_story = content.featured_articles[0].items[0]
            # Remaining featured items after lead
            featured_articles = content.featured_articles[0].items[1:]
            # Add items from other featured sections
            for section in content.featured_articles[1:]:
                featured_articles.extend(section.items)
        # Fallback to thematic_summaries if no featured
        elif content.thematic_summaries:
            for section in content.thematic_summaries:
                if section.items:
                    lead_story = section.items[0]
                    break

        return template.render(
            date=content.date,
            period=content.period,
            sources_processed=content.sources_processed,
            articles_received=content.articles_received,
            articles_included=content.articles_included,
            triage_pending_count=content.triage_pending_count,
            usage=content.usage,
            lead_story=lead_story,
            featured_articles=featured_articles,
            news_clusters=content.news_clusters,
            fan_sections=content.fan_sections,
            thematic_summaries=content.thematic_summaries,
            attention_sections=content.attention_sections,
            filtered_headlines=content.filtered_headlines,
            promote_enabled=bool(self.promote_worker_url and self.promote_token),
            promote_worker_url=self.promote_worker_url,
            promote_token=self.promote_token,
        )

    def write(self, content: DigestContent, dry_run: bool = False) -> Path:
        """Persist rendered HTML to disk and regenerate index.

        Args:
            content: Digest content to write
            dry_run: If True, print to stdout instead of writing

        Returns:
            Path to written file (or would-be path in dry_run mode)

        Raises:
            OSError: If output directory cannot be created
        """
        html = self.render(content)
        filename = f"{content.date}-{content.period}.html"
        file_path = self.output_dir / filename

        if dry_run:
            print(html)
            return file_path

        # Create parent directories if needed
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Write digest file
        file_path.write_text(html, encoding="utf-8")

        # Regenerate index
        self.write_index(self.output_dir)

        return file_path

    def render_index(self, digest_dir: Path) -> str:
        """Generate history index page listing all digest files.

        Args:
            digest_dir: Directory containing digest HTML files

        Returns:
            Complete HTML index page

        Raises:
            jinja2.TemplateNotFound: If template is missing
        """
        template = self.env.get_template("index.html.j2")

        # Scan for digest files (pattern: YYYY-MM-DD-*.html)
        digest_pattern = re.compile(r"(\d{4}-\d{2}-\d{2})-(.+)\.html$")
        digests = []

        if digest_dir.exists():
            for file_path in digest_dir.iterdir():
                if not file_path.is_file():
                    continue
                match = digest_pattern.match(file_path.name)
                if match:
                    date, period = match.groups()
                    digests.append(
                        {
                            "date": date,
                            "period": period,
                            "filename": file_path.name,
                        }
                    )

        # Sort by date descending (most recent first)
        digests.sort(key=lambda d: (d["date"], d["period"]), reverse=True)

        return template.render(digests=digests)

    def write_index(self, digest_dir: Path) -> Path:
        """Persist the index page to disk.

        Args:
            digest_dir: Directory to write index.html

        Returns:
            Path to written index.html
        """
        html = self.render_index(digest_dir)
        index_path = digest_dir / "index.html"

        # Ensure directory exists
        digest_dir.mkdir(parents=True, exist_ok=True)

        index_path.write_text(html, encoding="utf-8")
        return index_path
