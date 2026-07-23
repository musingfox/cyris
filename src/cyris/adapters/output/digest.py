"""Digest note generator for Obsidian markdown output."""

import logging
from pathlib import Path

from cyris.domain.models import DigestContent, DigestItem, DigestSection

logger = logging.getLogger(__name__)

PERIOD_LABELS = {
    "morning": "早報",
    "evening": "晚報",
}


class DigestWriter:
    """Renders DigestContent to Obsidian markdown and writes to the user vault."""

    def __init__(self, user_vault_path: Path, digest_folder: str = "Digests") -> None:
        self.vault_path = user_vault_path
        self.digest_folder = digest_folder

    def render(self, content: DigestContent) -> str:
        """Render DigestContent to Obsidian markdown string."""
        parts: list[str] = []

        # YAML frontmatter
        parts.append("---")
        parts.append(f"date: {content.date}")
        parts.append(f"period: {content.period}")
        parts.append(f"sources_processed: {content.sources_processed}")
        parts.append(f"articles_received: {content.articles_received}")
        parts.append(f"articles_included: {content.articles_included}")
        parts.append("---")
        parts.append("")

        # Title
        label = PERIOD_LABELS.get(content.period, content.period)
        parts.append(f"# {label} {content.date}")
        parts.append("")

        # Tracked topic updates
        if content.tracked_updates and content.tracked_updates.items:
            parts.append("## 追蹤主題更新")
            parts.append("")
            parts.append("> 你正在追蹤的主題有新動態")
            parts.append("")
            parts.append(self._render_tracked_updates(content.tracked_updates))
            parts.append("---")
            parts.append("")

        # Featured articles
        if content.featured_articles:
            parts.append("## 精選文章")
            parts.append("")
            for section in content.featured_articles:
                parts.append(self._render_section(section))
            parts.append("---")
            parts.append("")

        # News clusters
        if content.news_clusters:
            parts.append("## 新聞聚合")
            parts.append("")
            parts.append("> 相關新聞整合摘要")
            parts.append("")
            for section in content.news_clusters:
                parts.append(self._render_section(section))
            parts.append("---")
            parts.append("")

        # Fan sections (followed groups/newsletters — own passthrough channel)
        if content.fan_sections:
            parts.append("## 追蹤動態")
            parts.append("")
            parts.append("> 你追蹤的團體／電子報更新")
            parts.append("")
            for section in content.fan_sections:
                parts.append(self._render_fan_section(section))
            parts.append("---")
            parts.append("")

        # Thematic summaries
        if content.thematic_summaries:
            parts.append("## 主題摘要")
            parts.append("")
            for section in content.thematic_summaries:
                parts.append(self._render_section(section))
            parts.append("---")
            parts.append("")

        # Attention sections
        if content.attention_sections:
            parts.append("## 值得關注")
            parts.append("")
            for section in content.attention_sections:
                parts.append(self._render_attention_section(section))
            parts.append("---")
            parts.append("")

        # Filtered headlines (collapsible)
        if content.filtered_headlines:
            parts.append(self._render_filtered_callout(content.filtered_headlines))
            parts.append("")

        # Statistics
        parts.append("## 統計")
        parts.append(f"- 處理來源：{content.sources_processed} 個")
        parts.append(f"- 收到文章：{content.articles_received} 篇")
        parts.append(f"- 過濾後保留：{content.articles_included} 篇")
        if content.articles_received > 0:
            pct = content.articles_included / content.articles_received * 100
            parts[-1] += f"（{pct:.1f}%）"
        if content.usage.api_calls > 0:
            u = content.usage
            parts.append(
                f"- API 用量：{u.api_calls} 次呼叫，"
                f"{u.input_tokens:,} 輸入 + {u.output_tokens:,} 輸出 tokens"
            )
            parts.append(f"- 預估費用：${u.estimated_cost:.4f}")
        parts.append("")

        # Triage section
        if content.triage_pending_count is not None and content.triage_pending_count > 0:
            parts.append("---")
            parts.append("")
            parts.append("## 待瀏覽")
            parts.append(
                f"還有 {content.triage_pending_count} 篇待瀏覽，執行 `cyris triage-ui` 啟動分類介面"
            )
            parts.append("")

        return "\n".join(parts)

    def _render_section(self, section: DigestSection) -> str:
        """Render a single thematic section."""
        # Render heading with URL if first item has urls
        if section.items and section.items[0].urls:
            heading = f"### [{section.heading}]({section.items[0].urls[0]})"
        else:
            heading = f"### {section.heading}"

        lines = [heading]

        # Render description if non-empty (Contract 1)
        if section.description:
            lines.append(section.description)

        if section.items:
            # Use the summary from the first item as the section summary
            first = section.items[0]
            if first.summary:
                lines.append(first.summary)
            # Collect all sources
            all_sources = []
            for item in section.items:
                all_sources.extend(item.sources)
            unique_sources = list(dict.fromkeys(all_sources))
            if unique_sources:
                lines.append(f"`Sources: {', '.join(unique_sources)}`")

            # Event reference
            event_refs = [i.event_ref for i in section.items if i.event_ref]
            if event_refs:
                lines.append(f" · [[{event_refs[0]}]]")
        lines.append("")
        return "\n".join(lines)

    def _render_attention_section(self, section: DigestSection) -> str:
        """Render attention section with per-article titles and snippets."""
        if len(section.items) <= 1:
            return self._render_section(section)

        lines = [f"### {section.heading}"]
        if section.description:
            lines.append(section.description)

        for item in section.items:
            title_part = f"**[{item.title}]({item.urls[0]})**" if item.urls else f"**{item.title}**"
            source_str = f" ({', '.join(item.sources)})" if item.sources else ""
            summary_str = f" — {item.summary}" if item.summary else ""
            lines.append(f"- {title_part}{summary_str}{source_str}")

        lines.append("")
        return "\n".join(lines)

    def _render_fan_section(self, section: DigestSection) -> str:
        """Render a fan section: source heading + title/link/excerpt per item (no AI)."""
        lines = [f"### {section.heading}"]
        for item in section.items:
            title_part = f"**[{item.title}]({item.urls[0]})**" if item.urls else f"**{item.title}**"
            summary_str = f" — {item.summary}" if item.summary else ""
            lines.append(f"- {title_part}{summary_str}")
        lines.append("")
        return "\n".join(lines)

    def _render_tracked_updates(self, section: DigestSection) -> str:
        """Render tracked updates per-item (title+link, summary, source, event wiki ref)."""
        lines = []
        for item in section.items:
            title_part = f"**[{item.title}]({item.urls[0]})**" if item.urls else f"**{item.title}**"
            source_str = f" ({', '.join(item.sources)})" if item.sources else ""
            summary_str = f" — {item.summary}" if item.summary else ""
            ref_str = f" · [[{item.event_ref}]]" if item.event_ref else ""
            lines.append(f"- {title_part}{summary_str}{source_str}{ref_str}")
        lines.append("")
        return "\n".join(lines)

    def _render_filtered_callout(self, headlines: list[DigestItem]) -> str:
        """Render filtered headlines in a concise two-line format inside a callout (Contract 3)."""
        count = len(headlines)
        lines = [f"> [!info]- 其他標題（{count}篇）"]

        for item in headlines:
            # Line 1: title with link and sources
            source_str = f" ({', '.join(item.sources)})" if item.sources else ""
            title_part = f"**[{item.title}]({item.urls[0]})**" if item.urls else f"**{item.title}**"
            lines.append(f"> - {title_part}{source_str}")

            # Line 2: summary if present, truncated to 80 chars
            if item.summary:
                summary = item.summary
                if len(summary) > 80:
                    summary = summary[:80] + "…"
                lines.append(f">   {summary}")

        return "\n".join(lines)

    def _digest_path(self, content: DigestContent) -> Path:
        """Compute the output file path for a digest."""
        folder = self.vault_path / self.digest_folder
        filename = f"{content.date}-{content.period}.md"
        return folder / filename

    def write(self, content: DigestContent, dry_run: bool = False) -> Path:
        """Render and write digest note to user vault.

        Args:
            content: Digest content to render.
            dry_run: If True, return path without writing.

        Returns:
            Path where the digest was (or would be) written.
        """
        path = self._digest_path(content)

        if dry_run:
            logger.info("Dry run: would write to %s", path)
            return path

        path.parent.mkdir(parents=True, exist_ok=True)
        markdown = self.render(content)
        path.write_text(markdown, encoding="utf-8")
        logger.info("Digest written to %s", path)
        return path
