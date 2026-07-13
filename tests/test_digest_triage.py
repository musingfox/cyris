"""Tests for digest triage section rendering."""

import pytest

from cyris.adapters.output.digest import DigestWriter
from cyris.domain.models import DigestContent, UsageStats


class TestDigestTriageSection:
    """Tests for Contract 1 (DigestContent.triage_pending_count) and Contract 2 (rendering)."""

    @pytest.fixture
    def writer(self, tmp_path):
        return DigestWriter(tmp_path, "Digests")

    def test_triage_pending_count_none(self):
        """Contract 1: DigestContent can accept triage_pending_count=None."""
        content = DigestContent(
            date="2026-04-09",
            period="morning",
            sources_processed=5,
            articles_received=20,
            articles_included=10,
            triage_pending_count=None,
        )
        assert content.triage_pending_count is None

    def test_triage_pending_count_zero(self):
        """Contract 1: DigestContent can accept triage_pending_count=0."""
        content = DigestContent(
            date="2026-04-09",
            period="morning",
            sources_processed=5,
            articles_received=20,
            articles_included=10,
            triage_pending_count=0,
        )
        assert content.triage_pending_count == 0

    def test_triage_pending_count_positive(self):
        """Contract 1: DigestContent can accept triage_pending_count=5."""
        content = DigestContent(
            date="2026-04-09",
            period="morning",
            sources_processed=5,
            articles_received=20,
            articles_included=10,
            triage_pending_count=5,
        )
        assert content.triage_pending_count == 5

    def test_render_no_triage_section_when_none(self, writer):
        """Contract 2: No triage section when triage_pending_count is None."""
        content = DigestContent(
            date="2026-04-09",
            period="morning",
            sources_processed=5,
            articles_received=20,
            articles_included=10,
            triage_pending_count=None,
        )
        md = writer.render(content)
        assert "## 待瀏覽" not in md
        assert "cyris triage-ui" not in md

    def test_render_no_triage_section_when_zero(self, writer):
        """Contract 2: No triage section when triage_pending_count is 0."""
        content = DigestContent(
            date="2026-04-09",
            period="morning",
            sources_processed=5,
            articles_received=20,
            articles_included=10,
            triage_pending_count=0,
        )
        md = writer.render(content)
        assert "## 待瀏覽" not in md
        assert "cyris triage-ui" not in md

    def test_render_triage_section_with_count_5(self, writer):
        """Contract 2: Triage section appears when triage_pending_count > 0."""
        content = DigestContent(
            date="2026-04-09",
            period="morning",
            sources_processed=5,
            articles_received=20,
            articles_included=10,
            triage_pending_count=5,
        )
        md = writer.render(content)
        assert "## 待瀏覽" in md
        assert "還有 5 篇待瀏覽" in md
        assert "`cyris triage-ui`" in md

    def test_render_triage_section_with_count_1(self, writer):
        """Contract 2: Triage section handles singular count correctly."""
        content = DigestContent(
            date="2026-04-09",
            period="morning",
            sources_processed=5,
            articles_received=20,
            articles_included=10,
            triage_pending_count=1,
        )
        md = writer.render(content)
        assert "## 待瀏覽" in md
        assert "還有 1 篇待瀏覽" in md
        assert "`cyris triage-ui`" in md

    def test_triage_section_appears_after_statistics(self, writer):
        """Contract 2: Triage section appears after Statistics section."""
        content = DigestContent(
            date="2026-04-09",
            period="morning",
            sources_processed=5,
            articles_received=20,
            articles_included=10,
            usage=UsageStats(input_tokens=1000, output_tokens=500, api_calls=3, model="sonnet"),
            triage_pending_count=3,
        )
        md = writer.render(content)

        # Find positions
        stats_pos = md.find("## 統計")
        triage_pos = md.find("## 待瀏覽")

        assert stats_pos != -1, "Statistics section should exist"
        assert triage_pos != -1, "Triage section should exist"
        assert triage_pos > stats_pos, "Triage section should appear after Statistics"

    def test_triage_section_has_separator(self, writer):
        """Contract 2: Triage section has proper separator before it."""
        content = DigestContent(
            date="2026-04-09",
            period="morning",
            sources_processed=5,
            articles_received=20,
            articles_included=10,
            triage_pending_count=7,
        )
        md = writer.render(content)

        # Check that there's a separator (---) before the triage section
        assert "---\n\n## 待瀏覽" in md
