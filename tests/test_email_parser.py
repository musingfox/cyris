"""Tests for email newsletter parsing."""

import pytest

from cyris.adapters.fetch.email_parser import parse_newsletter, strip_tracking_params


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
