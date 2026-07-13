"""Tests for email newsletter parsing."""

import pytest

from cyris.adapters.fetch.email_parser import _extract_links, parse_newsletter


class TestExtractLinks:
    def test_extracts_links(self):
        html = '<a href="https://example.com/1">One</a><a href="https://example.com/2">Two</a>'
        assert _extract_links(html) == ["https://example.com/1", "https://example.com/2"]

    def test_filters_tracking(self):
        html = '<a href="https://example.com/article">Good</a><a href="https://mailchimp.com/unsubscribe">Bad</a>'
        links = _extract_links(html)
        assert links == ["https://example.com/article"]

    def test_filters_patreon_boilerplate_keeps_posts(self):
        html = (
            '<a href="https://www.patreon.com/ieo/posts/semi-162564851?utm_source=post_link">Post</a>'
            '<a href="https://www.patreon.com/ieo?utm_source=creator_link">Get more</a>'
            '<a href="https://www.patreon.com/policy/legal">Creators</a>'
            '<a href="https://privacy.patreon.com/policies">Fwd junk</a>'
        )
        assert _extract_links(html) == [
            "https://www.patreon.com/ieo/posts/semi-162564851?utm_source=post_link"
        ]

    def test_deduplicates(self):
        html = '<a href="https://example.com">A</a><a href="https://example.com">B</a>'
        assert _extract_links(html) == ["https://example.com"]

    def test_empty_html(self):
        assert _extract_links("No links here") == []


class TestParseNewsletter:
    def test_valid_payload(self):
        payload = {
            "from": "list@example.com",
            "subject": "Issue #1",
            "html": '<a href="https://example.com/article">Link</a>',
            "text": "Plain text",
            "headers": {"Date": "Tue, 18 Mar 2026 08:00:00 +0800"},
        }
        result = parse_newsletter(payload, "Test Newsletter")
        assert result.subject == "Issue #1"
        assert result.from_email == "list@example.com"
        assert result.links == ["https://example.com/article"]
        assert result.source_name == "Test Newsletter"

    def test_missing_subject(self):
        with pytest.raises(ValueError, match="subject"):
            parse_newsletter({"from": "x@example.com"}, "Test")

    def test_missing_from(self):
        with pytest.raises(ValueError, match="from"):
            parse_newsletter({"subject": "Issue"}, "Test")

    def test_no_links(self):
        payload = {
            "from": "list@example.com",
            "subject": "Issue",
            "html": "No links",
            "text": "",
            "headers": {},
        }
        result = parse_newsletter(payload, "Test")
        assert result.links == []
