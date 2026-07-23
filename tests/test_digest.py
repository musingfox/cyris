"""Tests for digest writer."""

import pytest

from cyris.adapters.output.digest import DigestWriter
from cyris.domain.models import DigestContent


class TestDigestWriter:
    @pytest.fixture
    def writer(self, tmp_path):
        return DigestWriter(tmp_path, "Digests")

    def test_render_contains_frontmatter(self, writer, sample_digest_content):
        md = writer.render(sample_digest_content)

        assert md.startswith("---\n")
        assert "date: 2026-03-16" in md
        assert "period: morning" in md
        assert "sources_processed: 3" in md

    def test_render_contains_title(self, writer, sample_digest_content):
        md = writer.render(sample_digest_content)
        assert "# 早報 2026-03-16" in md

    def test_render_contains_thematic_summaries(self, writer, sample_digest_content):
        md = writer.render(sample_digest_content)
        assert "## 主題摘要" in md
        assert "### [AI 產業本週動態](https://stratechery.com/2026/03/16/weekly-trends)" in md
        assert "`Sources: Stratechery`" in md

    def test_render_contains_filtered_headlines_callout(self, writer, sample_digest_content):
        md = writer.render(sample_digest_content)
        assert "> [!info]- 其他標題（2篇）" in md
        # Two-line format: title + source on line 1, summary on line 2
        assert (
            "> - **[Apple Vision Pro 第二代發表]"
            "(https://techcrunch.com/2026/03/16/apple-vision-pro-2)** (TechCrunch)"
        ) in md
        assert ">   價格降至 $2499，新增企業功能" in md
        assert (
            "> - **[台積電亞利桑那廠延期六個月]"
            "(https://reuters.com/2026/03/16/tsmc-arizona)** (Reuters)"
        ) in md
        assert ">   Phase 2 因人力問題延後" in md

    def test_render_has_no_checkboxes(self, writer, sample_digest_content):
        """Promote loop replaced digest checkboxes: no deep-read/track markers."""
        md = writer.render(sample_digest_content)
        assert "deep-read" not in md
        assert "- [ ] track" not in md

    def test_render_contains_statistics(self, writer, sample_digest_content):
        md = writer.render(sample_digest_content)
        assert "## 統計" in md
        assert "處理來源：3 個" in md
        assert "收到文章：10 篇" in md

    def test_render_evening_label(self, writer, sample_digest_content):
        sample_digest_content.period = "evening"
        md = writer.render(sample_digest_content)
        assert "# 晚報 2026-03-16" in md

    def test_render_empty_digest(self, writer):
        content = DigestContent(
            date="2026-03-16",
            period="morning",
            sources_processed=0,
            articles_received=0,
            articles_included=0,
        )
        md = writer.render(content)
        assert "# 早報 2026-03-16" in md
        assert "## 統計" in md
        assert "## 主題摘要" not in md
        assert "[!info]- 其他標題" not in md

    def test_write_creates_file(self, writer, sample_digest_content, tmp_path):
        path = writer.write(sample_digest_content)

        assert path.exists()
        assert path == tmp_path / "Digests" / "2026-03-16-morning.md"
        assert "早報" in path.read_text(encoding="utf-8")

    def test_write_dry_run(self, writer, sample_digest_content, tmp_path):
        path = writer.write(sample_digest_content, dry_run=True)

        assert not path.exists()
        assert path == tmp_path / "Digests" / "2026-03-16-morning.md"

    def test_render_news_clusters(self, writer):
        from cyris.domain.models import DigestContent, DigestItem, DigestSection

        content = DigestContent(
            date="2026-03-31",
            period="morning",
            sources_processed=2,
            articles_received=5,
            articles_included=2,
            news_clusters=[
                DigestSection(
                    heading="科技裁員潮",
                    items=[
                        DigestItem(
                            title="科技裁員潮",
                            summary="多家科技公司宣布裁員計畫",
                            sources=["Reuters", "Bloomberg"],
                            urls=["https://reuters.com/1", "https://bloomberg.com/2"],
                        )
                    ],
                )
            ],
        )

        md = writer.render(content)

        assert "## 新聞聚合" in md
        assert "> 相關新聞整合摘要" in md
        assert "### [科技裁員潮](https://reuters.com/1)" in md
        assert "多家科技公司宣布裁員計畫" in md

    def test_render_empty_news_clusters(self, writer):
        from cyris.domain.models import DigestContent

        content = DigestContent(
            date="2026-03-31",
            period="morning",
            sources_processed=1,
            articles_received=3,
            articles_included=0,
            news_clusters=[],
        )

        md = writer.render(content)

        assert "## 新聞聚合" not in md

    def test_render_attention_sections(self, writer):
        from cyris.domain.models import DigestContent, DigestItem, DigestSection

        content = DigestContent(
            date="2026-04-10",
            period="evening",
            sources_processed=3,
            articles_received=10,
            articles_included=2,
            attention_sections=[
                DigestSection(
                    heading="tech",
                    items=[
                        DigestItem(
                            title="Tech Article",
                            summary="",
                            sources=["TechNews"],
                            urls=["https://example.com/tech"],
                        )
                    ],
                )
            ],
        )

        md = writer.render(content)

        assert "## 值得關注" in md
        assert "### [tech](https://example.com/tech)" in md
        assert "`Sources: TechNews`" in md

    def test_render_empty_attention_sections(self, writer):
        from cyris.domain.models import DigestContent

        content = DigestContent(
            date="2026-04-10",
            period="morning",
            sources_processed=2,
            articles_received=5,
            articles_included=0,
            attention_sections=[],
        )

        md = writer.render(content)

        assert "## 值得關注" not in md

    def test_section_order_with_attention(self, writer):
        from cyris.domain.models import DigestContent, DigestItem, DigestSection

        content = DigestContent(
            date="2026-04-10",
            period="morning",
            sources_processed=5,
            articles_received=20,
            articles_included=7,
            featured_articles=[
                DigestSection(
                    heading="Featured",
                    items=[
                        DigestItem(
                            title="F1",
                            summary="Featured summary",
                            sources=["S1"],
                            urls=["http://f1"],
                        )
                    ],
                )
            ],
            news_clusters=[
                DigestSection(
                    heading="News",
                    items=[
                        DigestItem(
                            title="N1", summary="News summary", sources=["S2"], urls=["http://n1"]
                        )
                    ],
                )
            ],
            thematic_summaries=[
                DigestSection(
                    heading="Theme",
                    items=[
                        DigestItem(
                            title="T1",
                            summary="Thematic summary",
                            sources=["S3"],
                            urls=["http://t1"],
                        )
                    ],
                )
            ],
            attention_sections=[
                DigestSection(
                    heading="Attention",
                    items=[DigestItem(title="A1", summary="", sources=["S4"], urls=["http://a1"])],
                )
            ],
            filtered_headlines=[
                DigestItem(
                    title="H1",
                    summary="Headline summary",
                    sources=["S5"],
                    urls=["http://h1"],
                )
            ],
        )

        md = writer.render(content)

        # Check order: 精選文章 → 新聞聚合 → 主題摘要 → 值得關注 → 其他標題
        featured_idx = md.find("## 精選文章")
        news_idx = md.find("## 新聞聚合")
        thematic_idx = md.find("## 主題摘要")
        attention_idx = md.find("## 值得關注")
        headlines_idx = md.find("> [!info]- 其他標題")

        assert featured_idx < news_idx
        assert news_idx < thematic_idx
        assert thematic_idx < attention_idx
        assert attention_idx < headlines_idx

        # Verify no --- separator after callout (Contract 2)
        stats_idx = md.find("## 統計")
        between = md[headlines_idx:stats_idx]
        assert "---" not in between

    def test_render_section_with_description(self, writer):
        """Contract 1: description field rendered when non-empty."""
        from cyris.domain.models import DigestItem, DigestSection

        section = DigestSection(
            heading="Test",
            description="This is a description",
            items=[DigestItem(title="T", summary="S", sources=["X"], urls=["http://x"])],
        )
        output = writer._render_section(section)
        assert "### [Test](http://x)" in output
        assert "This is a description" in output
        # description should appear before the item summary
        desc_idx = output.index("This is a description")
        summary_idx = output.index("S")
        assert desc_idx < summary_idx

    def test_render_section_without_description(self, writer):
        """Contract 1: no description paragraph when description is empty."""
        from cyris.domain.models import DigestItem, DigestSection

        section = DigestSection(
            heading="Test",
            items=[DigestItem(title="T", summary="S", sources=["X"], urls=["http://x"])],
        )
        output = writer._render_section(section)
        assert "### [Test](http://x)" in output
        # No extra blank lines between heading and summary
        lines = output.split("\n")
        heading_idx = next(i for i, line in enumerate(lines) if "### [Test]" in line)
        summary_idx = next(i for i, line in enumerate(lines) if line == "S")
        # Should be consecutive (no description line in between)
        assert summary_idx == heading_idx + 1

    def test_render_filtered_callout_two_line_format(self, writer):
        """Contract 3: callout uses two-line format with truncated summary."""
        from cyris.domain.models import DigestItem

        headlines = [
            DigestItem(
                title="Article One",
                summary="Short summary",
                sources=["TechCrunch"],
                urls=["http://a1"],
            ),
        ]
        output = writer._render_filtered_callout(headlines)
        assert "> [!info]- 其他標題（1篇）" in output
        assert "> - **[Article One](http://a1)** (TechCrunch)" in output
        assert ">   Short summary" in output
        assert "- [ ]" not in output  # no checkboxes

    def test_render_filtered_callout_truncates_long_summary(self, writer):
        """Contract 3: summary truncated to 80 chars."""
        from cyris.domain.models import DigestItem

        long = "A" * 100
        headlines = [DigestItem(title="T", summary=long, sources=["X"], urls=["http://x"])]
        output = writer._render_filtered_callout(headlines)
        lines = output.split("\n")
        summary_line = [line for line in lines if line.startswith(">   ")][0]
        # ">   " prefix + 80 chars + "…"
        content = summary_line[4:]  # strip ">   "
        assert len(content) <= 81  # 80 + ellipsis char
        assert content.endswith("…")

    def test_render_filtered_callout_no_summary(self, writer):
        """Contract 3: no second line when summary is empty."""
        from cyris.domain.models import DigestItem

        headlines = [DigestItem(title="T", summary="", sources=["X"], urls=["http://x"])]
        output = writer._render_filtered_callout(headlines)
        lines = output.strip().split("\n")
        assert len(lines) == 2  # header + one item line

    def test_no_separator_after_callout(self, writer, sample_digest_content):
        """Contract 2: no --- separator after callout block."""
        md = writer.render(sample_digest_content)
        callout_idx = md.find("> [!info]- 其他標題")
        stats_idx = md.find("## 統計")
        between = md[callout_idx:stats_idx]
        assert "---" not in between

    def test_no_usage_guide(self, writer, sample_digest_content):
        """Promote loop replaced the deep-read workflow: no usage guide callout."""
        md = writer.render(sample_digest_content)
        assert "> [!tip]- 使用說明" not in md

    def test_triage_section_after_statistics(self, writer):
        """Triage section appears after statistics."""
        from cyris.domain.models import DigestContent

        content = DigestContent(
            date="2026-04-12",
            period="morning",
            sources_processed=5,
            articles_received=15,
            articles_included=8,
            triage_pending_count=3,
        )

        md = writer.render(content)
        stats_idx = md.find("## 統計")
        triage_idx = md.find("## 待瀏覽")

        assert stats_idx != -1
        assert triage_idx != -1
        assert stats_idx < triage_idx

    def test_render_tracked_updates_multi_item(self, writer):
        """TrackedMarkdownRendering T1: multi-item per-item (title+link+summary+source+ref)."""
        from cyris.domain.models import DigestContent, DigestItem, DigestSection

        section = DigestSection(
            heading="台積電、AI 監管",
            items=[
                DigestItem(
                    title="t1",
                    summary="n1",
                    sources=["S1"],
                    urls=["u1"],
                    event_ref="台積電",
                ),
                DigestItem(
                    title="t2",
                    summary="n2",
                    sources=["S2"],
                    urls=["u2"],
                    event_ref="AI 監管",
                ),
            ],
        )
        content = DigestContent(
            date="2026-04-10",
            period="morning",
            sources_processed=1,
            articles_received=2,
            articles_included=2,
            tracked_updates=section,
        )

        md = writer.render(content)

        assert "## 追蹤主題更新" in md
        assert "- **[t1](u1)** — n1 (S1) · [[台積電]]" in md
        assert "- **[t2](u2)** — n2 (S2) · [[AI 監管]]" in md

    def test_render_tracked_updates_single_item(self, writer):
        """TrackedMarkdownRendering T2: single item still uses per-item form starting '- **['."""
        from cyris.domain.models import DigestContent, DigestItem, DigestSection

        section = DigestSection(
            heading="single",
            items=[
                DigestItem(
                    title="t1",
                    summary="n1",
                    sources=["S1"],
                    urls=["u1"],
                    event_ref="ref1",
                ),
            ],
        )
        content = DigestContent(
            date="2026-04-10",
            period="morning",
            sources_processed=1,
            articles_received=1,
            articles_included=1,
            tracked_updates=section,
        )

        md = writer.render(content)

        assert "- **[t1](u1)**" in md

    def test_render_tracked_updates_item_without_event_ref(self, writer):
        """TrackedMarkdownRendering T3: item with event_ref=None omits ' · [[' part."""
        from cyris.domain.models import DigestContent, DigestItem, DigestSection

        section = DigestSection(
            heading="no-ref",
            items=[
                DigestItem(
                    title="t",
                    summary="n",
                    sources=["S"],
                    urls=["u"],
                    event_ref=None,
                ),
            ],
        )
        content = DigestContent(
            date="2026-04-10",
            period="morning",
            sources_processed=1,
            articles_received=1,
            articles_included=1,
            tracked_updates=section,
        )

        md = writer.render(content)

        assert " · [[" not in md
        assert "- **[t](u)** — n (S)" in md

    def test_render_no_tracked_updates_header(self, writer):
        """TrackedMarkdownRendering T4: when no tracked_updates, output omits '追蹤主題更新'."""
        from cyris.domain.models import DigestContent, DigestSection

        content = DigestContent(
            date="2026-04-10",
            period="morning",
            sources_processed=1,
            articles_received=1,
            articles_included=1,
            # tracked_updates defaults to None
        )
        md = writer.render(content)
        assert "追蹤主題更新" not in md

        # also test explicit empty items
        empty_section = DigestSection(heading="empty", items=[])
        content2 = DigestContent(
            date="2026-04-10",
            period="morning",
            sources_processed=1,
            articles_received=0,
            articles_included=0,
            tracked_updates=empty_section,
        )
        md2 = writer.render(content2)
        assert "追蹤主題更新" not in md2
