"""Tests for the defuddle full-text extraction adapter."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from cyris.adapters.fetch.defuddle import (
    _strip_leading_title_headings,
    extract_markdown,
    fetch_full_markdown,
)

BUN_HOME = Path.home() / ".bun/bin/bun"
NODE_MODULES = Path(__file__).parent.parent / "node_modules" / "defuddle"


def _fake_bun(tmp_path: Path, script: str) -> str:
    """Write an executable stand-in for the bun binary."""
    path = tmp_path / "fake-bun"
    path.write_text(f"#!/bin/sh\n{script}\n")
    path.chmod(0o755)
    return str(path)


class TestStripLeadingTitleHeadings:
    def test_drops_site_name_and_title_chain(self):
        md = "## Site Name\n\n## The Title\n\nBody text."
        assert _strip_leading_title_headings(md, "The Title") == "Body text."

    def test_keeps_content_when_no_title_match(self):
        md = "## Legit Section\n\nBody text."
        assert _strip_leading_title_headings(md, "The Title") == md

    def test_no_title_returns_unchanged(self):
        md = "## The Title\n\nBody."
        assert _strip_leading_title_headings(md, "") == md


class TestExtractMarkdown:
    def test_success_strips_duplicate_title(self, tmp_path):
        payload_file = tmp_path / "payload.json"
        payload_file.write_text(json.dumps({"title": "T", "content": "## T\n\nbody"}))
        bun = _fake_bun(tmp_path, f"cat > /dev/null; cat {payload_file}")

        assert extract_markdown("<html/>", "https://x.test/a", bun) == "body"

    def test_nonzero_exit_returns_none(self, tmp_path):
        bun = _fake_bun(tmp_path, "cat > /dev/null; exit 1")
        assert extract_markdown("<html/>", "https://x.test/a", bun) is None

    def test_invalid_json_returns_none(self, tmp_path):
        bun = _fake_bun(tmp_path, "cat > /dev/null; echo not-json")
        assert extract_markdown("<html/>", "https://x.test/a", bun) is None

    def test_empty_content_returns_none(self, tmp_path):
        bun = _fake_bun(tmp_path, """cat > /dev/null; printf '{"title":"T","content":""}'""")
        assert extract_markdown("<html/>", "https://x.test/a", bun) is None

    def test_missing_binary_returns_none(self, tmp_path):
        with patch("cyris.adapters.fetch.defuddle.shutil.which", return_value=None):
            assert (
                extract_markdown("<html/>", "https://x.test/a", str(tmp_path / "no-bun")) is None
            )

    def test_missing_configured_path_falls_back_to_path_lookup(self, tmp_path):
        payload_file = tmp_path / "payload.json"
        payload_file.write_text(json.dumps({"title": "", "content": "body"}))
        bun_on_path = _fake_bun(tmp_path, f"cat > /dev/null; cat {payload_file}")

        with patch("cyris.adapters.fetch.defuddle.shutil.which", return_value=bun_on_path):
            assert extract_markdown("<html/>", "https://x.test/a", str(tmp_path / "no-bun")) == (
                "body"
            )


class TestFetchFullMarkdown:
    def test_longer_candidate_wins(self, tmp_path):
        with (
            patch("cyris.adapters.fetch.defuddle._fetch_html", return_value="<p>page</p>"),
            patch(
                "cyris.adapters.fetch.defuddle.extract_markdown",
                side_effect=["teaser", "full feed article text"],
            ),
        ):
            result = fetch_full_markdown("https://x.test/a", "<p>feed</p>", "bun")

        assert result == "full feed article text"

    def test_all_failures_return_none(self):
        with (
            patch("cyris.adapters.fetch.defuddle._fetch_html", return_value=None),
            patch("cyris.adapters.fetch.defuddle.extract_markdown", return_value=None),
        ):
            assert fetch_full_markdown("https://x.test/a", "<p>feed</p>", "bun") is None


@pytest.mark.skipif(
    not (BUN_HOME.exists() and NODE_MODULES.exists()),
    reason="requires bun and `bun install` in the repo root",
)
def test_shim_end_to_end():
    """Real bun + defuddle: relative links resolve against the base URL."""
    html = (
        "<html><head><title>Post</title></head><body><article>"
        '<h1>Post</h1><p>Hello <a href="/other">link</a> world, with enough '
        "prose to count as content for defuddle scoring purposes.</p>"
        "</article></body></html>"
    )
    result = extract_markdown(html, "https://example.com/post", str(BUN_HOME))

    assert result is not None
    assert "https://example.com/other" in result
