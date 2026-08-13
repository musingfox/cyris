"""Tests for email newsletter parsing."""

from datetime import UTC, datetime, timedelta

import pytest

from cyris.adapters.fetch.email_parser import (
    extract_ref_urls,
    parse_newsletter,
    strip_tracking_params,
    unwrap_tracking_redirect,
)


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
        assert not hasattr(result, "links")
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
        assert not hasattr(result, "links")


class TestStripTrackingParams:
    def test_strips_mailchimp_e_param(self):
        # T1
        url = "https://mailchi.mp/manny-li/067-imax?e=deadbeef00"
        assert strip_tracking_params(url) == "https://mailchi.mp/manny-li/067-imax"

    def test_keeps_non_tracking_query_params(self):
        # T2
        url = "https://us1.campaign-archive.com/?u=abc123&id=def456&e=deadbeef00"
        assert strip_tracking_params(url) == "https://us1.campaign-archive.com/?u=abc123&id=def456"

    def test_strips_utm_params_keeps_others(self):
        # T3
        url = "https://example.com/post?utm_source=news&utm_medium=email&ref=keep"
        assert strip_tracking_params(url) == "https://example.com/post?ref=keep"

    def test_no_query_params_unchanged(self):
        # T4
        url = "https://example.com/post"
        assert strip_tracking_params(url) == url

    def test_non_url_returns_as_is(self):
        # T5
        assert strip_tracking_params("not a url at all") == "not a url at all"

    def test_strips_c_param(self):
        # pinning: c param stripped (required fix)
        url = "https://example.com/?c=abc123&ref=keep"
        assert strip_tracking_params(url) == "https://example.com/?ref=keep"


class TestNewsletterSendDateParsed:
    def test_iso_z_date_parsed_to_utc(self):
        # T1
        payload = {
            "from": "list@example.com",
            "subject": "Issue",
            "html": "",
            "text": "",
            "headers": {"Date": "2026-07-28T04:00:00.000Z"},
        }
        result = parse_newsletter(payload, "Test")
        assert result.date == datetime(2026, 7, 28, 4, 0, tzinfo=UTC)

    def test_rfc2822_with_positive_offset(self):
        # T2
        payload = {
            "from": "list@example.com",
            "subject": "Issue",
            "html": "",
            "text": "",
            "headers": {"Date": "Tue, 18 Mar 2026 08:00:00 +0800"},
        }
        result = parse_newsletter(payload, "Test")
        assert result.date.utcoffset() == timedelta(hours=8)
        assert result.date.hour == 8

    def test_invalid_date_falls_back_to_now_utc(self):
        # T3
        payload = {
            "from": "list@example.com",
            "subject": "Issue",
            "html": "",
            "text": "",
            "headers": {"Date": "not a date"},
        }
        result = parse_newsletter(payload, "Test")
        assert result.date.tzinfo is not None

    def test_missing_date_falls_back_to_now_utc(self):
        # T4
        payload = {
            "from": "list@example.com",
            "subject": "Issue",
            "html": "",
            "text": "",
            "headers": {},
        }
        result = parse_newsletter(payload, "Test")
        assert result.date.tzinfo is not None


class TestNewsletterSubjectPrefixStripped:
    def test_strips_fwd_prefix(self):
        # T1
        payload = {
            "from": "x@example.com",
            "subject": "Fwd: 粉虱通訊 No. 28｜夏天的尾巴",
            "html": "",
            "text": "",
            "headers": {},
        }
        result = parse_newsletter(payload, "Test")
        assert result.subject == "粉虱通訊 No. 28｜夏天的尾巴"

    def test_strips_nested_fwd_re(self):
        # T2
        payload = {
            "from": "x@example.com",
            "subject": "Fwd: Re: Issue #1",
            "html": "",
            "text": "",
            "headers": {},
        }
        result = parse_newsletter(payload, "Test")
        assert result.subject == "Issue #1"

    def test_re_colon_only_keeps_original(self):
        # T3
        payload = {
            "from": "x@example.com",
            "subject": "Re:",
            "html": "",
            "text": "",
            "headers": {},
        }
        result = parse_newsletter(payload, "Test")
        assert result.subject == "Re:"

    def test_no_colon_not_prefix(self):
        # T4
        payload = {
            "from": "x@example.com",
            "subject": "Reflections on IMAX",
            "html": "",
            "text": "",
            "headers": {},
        }
        result = parse_newsletter(payload, "Test")
        assert result.subject == "Reflections on IMAX"

    def test_missing_subject_raises_before_strip(self):
        # T5
        with pytest.raises(ValueError, match="subject"):
            parse_newsletter({"from": "x@example.com"}, "Test")

    def test_strips_chinese_forward_prefix(self):
        # pinning: 轉寄: stripped
        payload = {
            "from": "x@example.com",
            "subject": "轉寄: 粉虱通訊 No. 28",
            "html": "",
            "text": "",
            "headers": {},
        }
        result = parse_newsletter(payload, "Test")
        assert result.subject == "粉虱通訊 No. 28"

    def test_strips_chinese_reply_prefix(self):
        # pinning: 回覆: stripped
        payload = {
            "from": "x@example.com",
            "subject": "回覆: Issue #1",
            "html": "",
            "text": "",
            "headers": {},
        }
        result = parse_newsletter(payload, "Test")
        assert result.subject == "Issue #1"

    def test_tolerates_leading_trailing_whitespace_in_prefix(self):
        # pinning: tolerate leading/trailing ws around prefix
        payload = {
            "from": "x@example.com",
            "subject": "  轉寄:   foo bar  ",
            "html": "",
            "text": "",
            "headers": {},
        }
        result = parse_newsletter(payload, "Test")
        assert result.subject == "foo bar"






class TestExtractNewsletterRefUrls:
    def test_extracts_unwrapped_content_links(self):
        html = """
        <a href="https://xx.list-manage.com/track/click?u=1&amp;id=2&amp;url=https%3A%2F%2Fexample.com%2Fa%3Futm_source%3Dnl%26e%3Ddeadbeef00">Article</a>
        <a href="https://xx.list-manage.com/track/click?u=1&amp;id=2&amp;url=https%3A%2F%2Fwww.patreon.com%2Fposts%2Ffoo-123">Patreon</a>
        <a href="https://mailchi.mp/newsletter/view">View online</a>
        <a href="mailto:news@example.com">Email</a>
        <a href="https://xx.list-manage.com/unsubscribe?u=1">Unsubscribe</a>
        """
        assert extract_ref_urls(html) == [
            "https://example.com/a",
            "https://www.patreon.com/posts/foo-123",
        ]

    def test_deduplicates_unwrapped_and_bare_links(self):
        html = """
        <a href="https://example.com/a">Article</a>
        <a href="https://xx.list-manage.com/track/click?url=https%3A%2F%2Fexample.com%2Fa">Again</a>
        """
        assert extract_ref_urls(html) == ["https://example.com/a"]

    def test_empty_html_has_no_links(self):
        assert extract_ref_urls("") == []

    def test_skips_shares_and_images(self):
        html = """
        <a href="https://twitter.com/intent/tweet?url=https://example.com/a">Tweet</a>
        <a href="https://facebook.com/sharer/sharer.php?u=https://example.com/a">Share</a>
        <a href="https://example.com/assets/article.png">Image</a>
        """
        assert extract_ref_urls(html) == []

    def test_skips_track_click_links_without_targets(self):
        html = "".join(
            f'<a href="https://xx.list-manage.com/track/click?u=1&id={i}">Click</a>'
            for i in range(25)
        )
        assert extract_ref_urls(html) == []


class TestUnwrapTrackClickUrl:
    def test_unwraps_and_cleans_tracking_params(self):
        url = (
            "https://xx.list-manage.com/track/click?u=1&id=2&"
            "url=https%3A%2F%2Fexample.com%2Fpost%3Futm_source%3Dnl&e=deadbeef00"
        )
        result = unwrap_tracking_redirect(url)
        assert result == "https://example.com/post"
        assert "e=" not in result

    def test_non_tracking_url_is_unchanged(self):
        url = "https://example.com/a?x=1"
        assert unwrap_tracking_redirect(url) == url

    def test_track_click_without_target_is_unchanged(self):
        url = "https://xx.list-manage.com/track/click?u=1&id=2"
        assert unwrap_tracking_redirect(url) == url

    def test_non_list_manage_track_click_is_unchanged(self):
        url = "https://evil.net/track/click?url=https%3A%2F%2Fx.com"
        assert unwrap_tracking_redirect(url) == url
