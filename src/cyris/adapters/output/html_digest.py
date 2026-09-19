"""HTML digest output writer for newspaper-style rendering."""

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader, select_autoescape

from cyris.domain.models import DigestContent, DigestItem, StoredArticle
from cyris.service_layer.schedule import PERIOD_ORDER


def _hostname(url: str) -> str:
    """Return a URL's hostname for a compact source-link label."""
    return urlsplit(url).hostname or url


def _features(content: DigestContent) -> list[DigestItem]:
    """The issue's full-summary stories in reading order; the first is its lead.

    Every summarize-tier group with a full summary: the scored ones (already sorted
    by score in layer_by_score) followed by the unscored rest. Rendering them as
    one stream is what lets the digest drop the structurally near-empty "Thematic
    Summaries" section, and what the archive's headline card takes its title from.
    """
    return [
        item
        for section in [*content.featured_articles, *content.thematic_summaries]
        for item in section.items
    ]


class HtmlDigestWriter:
    """Renders DigestContent as a newspaper-style HTML page."""

    @staticmethod
    def digest_filename(date: str, period: str) -> str:
        """The digest page's file name; its extensionless form is the issue's URL."""
        return f"{date}-{period}.html"

    @staticmethod
    def raw_filename(date: str, period: str) -> str:
        """The raw page's file name; the archive links to it only under this name."""
        return f"{date}-{period}-raw.html"

    def __init__(self, output_dir: Path):
        """Initialize writer with output directory.

        The promote Worker's URL and bearer used to be constructor arguments,
        rendered into the page so a reader's browser could call the Worker
        directly. `private-votes-public-archive` moved the vote to the app
        Worker's `POST /api/vote`, which attaches the token server-side, and the
        buttons now gate on probing that route rather than on anything the
        renderer knows. Both arguments outlived their last read by four days.

        Args:
            output_dir: Directory to write HTML digests
        """
        self.output_dir = Path(output_dir)

        template_dir = Path(__file__).parent / "templates"
        self.env = Environment(
            loader=FileSystemLoader(template_dir),
            # default=True matters: select_autoescape matches on the template's own
            # extension, and these are named *.html.j2, so the .j2 suffix left every
            # template unescaped — feed-controlled titles and URLs went out raw.
            autoescape=select_autoescape(["html", "xml"], default=True),
        )
        self.env.filters["hostname"] = _hostname

    def render(self, content: DigestContent, raw_page: bool = False) -> str:
        """Transform DigestContent into complete HTML document.

        Args:
            content: Digest content to render
            raw_page: Whether this issue's raw page is emitted; only then does the
                issue bar link to it

        Returns:
            Complete HTML string with inline styles

        Raises:
            jinja2.TemplateNotFound: If template is missing
        """
        template = self.env.get_template("digest.html.j2")

        features = _features(content)
        lead_story = features[0] if features else None
        featured_articles = features[1:]

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
            attention_sections=content.attention_sections,
            filtered_headlines=content.filtered_headlines,
            raw_page=raw_page,
        )

    def write(self, content: DigestContent, dry_run: bool = False, raw_page: bool = False) -> Path:
        """Persist rendered HTML to disk and regenerate index.

        Args:
            content: Digest content to write
            dry_run: If True, print to stdout instead of writing
            raw_page: Whether this issue's raw page was written

        Returns:
            Path to written file (or would-be path in dry_run mode)

        Raises:
            OSError: If output directory cannot be created
        """
        html = self.render(content, raw_page=raw_page)
        file_path = self.output_dir / self.digest_filename(content.date, content.period)

        if dry_run:
            print(html)
            return file_path

        self.output_dir.mkdir(parents=True, exist_ok=True)

        file_path.write_text(html, encoding="utf-8")

        self.write_index(self.output_dir, content=content)

        return file_path

    def render_index(
        self,
        filenames: list[str],
        *,
        content: DigestContent | None = None,
        counts: Mapping[tuple[str, str], int] | None = None,
        period_order: Sequence[str] = PERIOD_ORDER,
    ) -> str:
        """The archive page, from the list of files the site is made of.

        Takes names rather than a directory because the archive no longer has to
        be a directory: with the site published from a manifest, the list comes
        from D1. `write_index` keeps the directory-scanning behaviour for the
        unconfigured local case.

        A day's issues follow `period_order`, the order the schedule fires them
        in, latest first; a label outside it sorts below the known ones of its day.

        The headline card is `content`'s issue when it is listed, so a run's own
        archive leads with it; otherwise the first issue. The rest go in month panels,
        each row showing its article count when `counts` has one for it.
        """
        template = self.env.get_template("index.html.j2")

        digest_pattern = re.compile(r"(\d{4}-\d{2}-\d{2})-(.+)\.html$")
        present = set(filenames)
        digests = []
        for name in filenames:
            if name.endswith("-raw.html"):
                continue
            match = digest_pattern.match(name)
            if match:
                date, period = match.groups()
                raw = self.raw_filename(date, period)
                digests.append(
                    {
                        "date": date,
                        "period": period,
                        "filename": name,
                        # Only an issue whose raw page exists links to it: no dead entries.
                        "raw_filename": raw if raw in present else None,
                    }
                )

        rank = {period: i for i, period in enumerate(period_order)}
        digests.sort(
            key=lambda d: (d["date"], rank.get(d["period"], -1), d["period"]), reverse=True
        )

        latest = digests[0] if digests else None
        card: dict = {}
        if content is not None:
            this_run = self.digest_filename(content.date, content.period)
            listed = next((d for d in digests if d["filename"] == this_run), None)
            if listed is not None:
                # What the card says beyond date and period is known only for the
                # issue this run is rendering, and only from memory.
                latest = listed
                features = _features(content)
                card["lead"] = features[0].title if features else None
                card["count"] = content.articles_included
                # A cluster's members are its URLs: one item can hold a whole story.
                largest = sorted(
                    content.news_clusters,
                    key=lambda section: -sum(len(item.urls) for item in section.items),
                )
                card["topics"] = " · ".join(section.heading for section in largest[:2])

        months: dict[str, list[dict]] = {}
        for digest in digests:
            if digest is not latest:
                rows = months.setdefault(digest["date"][:7], [])
                # Label-independent: whatever a day's periods are called, and however
                # many there are, its second row onward says it continues the day.
                digest["same_day"] = bool(rows) and rows[-1]["date"] == digest["date"]
                digest["count"] = (counts or {}).get((digest["date"], digest["period"]))
                rows.append(digest)

        return template.render(
            digests=digests,
            latest=latest,
            card=card,
            months=[{"month": month, "issues": issues} for month, issues in months.items()],
        )

    def write_index(self, digest_dir: Path, content: DigestContent | None = None) -> Path:
        """Persist the index page to disk.

        Args:
            digest_dir: Directory to write index.html
            content: This run's digest, so the headline card leads with it

        Returns:
            Path to written index.html
        """
        names = [p.name for p in digest_dir.iterdir() if p.is_file()] if digest_dir.exists() else []
        html = self.render_index(names, content=content)
        index_path = digest_dir / "index.html"

        digest_dir.mkdir(parents=True, exist_ok=True)

        index_path.write_text(html, encoding="utf-8")
        return index_path

    def write_raw(self, date: str, period: str, articles: list[StoredArticle]) -> Path:
        """Render this run's judged and still-pending articles, grouped by source.

        Args:
            date: Digest date (``YYYY-MM-DD``).
            period: Digest period (``morning``/``evening``).
            articles: What this run judged, plus whatever is still pending. Rows an
                earlier run in the overlapping window judged are the caller's to drop.

        Returns:
            Path to the written raw page.
        """
        html = self.render_raw(date, period, articles)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        file_path = self.output_dir / self.raw_filename(date, period)
        file_path.write_text(html, encoding="utf-8")
        return file_path

    def render_raw(self, date: str, period: str, articles: list[StoredArticle]) -> str:
        """The companion page's HTML: the given articles, grouped by source."""
        groups: dict[str, list[StoredArticle]] = {}
        for a in articles:
            groups.setdefault(a.source_name, []).append(a)

        rendered_groups = [
            {
                "name": name,
                # Highest score first; unscored articles trail the ranked ones.
                "articles": sorted(items, key=lambda a: (a.score is None, -(a.score or 0.0))),
            }
            for name, items in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        ]

        return self.env.get_template("raw.html.j2").render(
            date=date,
            period=period,
            total=len(articles),
            groups=rendered_groups,
        )
