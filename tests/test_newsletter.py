"""Tests for newsletter article fetching.

One email = one article whose content is the email body (text or stripped html).
No more per-link expansion; no http_client or network in this path.
"""

import hashlib
import logging
from datetime import datetime

import pytest

from cyris.adapters.fetch.email_parser import ParsedNewsletter
from cyris.adapters.fetch.newsletter import (
    _generate_article_id,
    harvest_url_candidates,
    newsletter_article,
    select_primary_content_url,
)
from cyris.domain.models import SourceConfig, Tier


@pytest.fixture
def parsed_body():
    return ParsedNewsletter(
        source_name="Test",
        subject="Issue #1",
        from_email="list@example.com",
        date=datetime(2026, 3, 18, tzinfo=None),
        html_content="",
        text_content="body here",
    )


@pytest.fixture
def source_summarize():
    return SourceConfig(name="Test Newsletter", tier=Tier.SUMMARIZE, tags=["tech"])


class TestGenerateArticleId:
    def test_deterministic(self):
        expected = hashlib.sha256(b"Test Newsletterhttps://example.com/1").hexdigest()
        assert _generate_article_id("Test Newsletter", "https://example.com/1") == expected


def _make_parsed(
    subject: str = "Test Subject",
    text_content: str = "本文內容",
    html_content: str = "",
    source_name: str = "Test",
) -> ParsedNewsletter:
    return ParsedNewsletter(
        source_name=source_name,
        subject=subject,
        from_email="list@example.com",
        date=datetime(2026, 7, 13),
        html_content=html_content,
        text_content=text_content,
    )


class TestNewsletterBodyIsTheArticle:
    def test_summarize_now_body_not_links(self, source_summarize):
        # T1
        p = _make_parsed(
            subject="曼報 #67｜IMAX",
            text_content="本期主文內容……（本文）",
            html_content='<a href="https://xx.list-manage.com/track/click?u=1&id=2">x</a>' * 25,
        )
        art = newsletter_article(p, source_summarize)
        assert art is not None
        assert art.content == "本期主文內容……（本文）"
        assert art.url.startswith("newsletter:")
        # url == 'newsletter:' + sha256(name + subject).hexdigest()
        expected_id = _generate_article_id("Test Newsletter", "曼報 #67｜IMAX")
        assert art.url == f"newsletter:{expected_id}"
        assert art.source_tier == Tier.SUMMARIZE

    def test_track_click_wrapper_not_treated_as_self_link(self):
        # T2: use hostname check on href (not substring), so e= tracking stays out of public url
        html = (
            '<a href="https://xx.list-manage.com/track/click?u=1&id=2&url='
            'https%3A%2F%2Fmailchi.mp%2Fabc%2Fno-28&e=deadbeef00">View this email</a>'
        )
        p = _make_parsed(text_content="本文", html_content=html)
        src = SourceConfig(name="Test", tier=Tier.SUMMARIZE, tags=[])
        art = newsletter_article(p, src)
        assert art is not None
        assert art.url.startswith("newsletter:")

    def test_fan_prefers_clean_mailchi_view_link(self):
        # T3
        html = '<a href="https://mailchi.mp/abc/no-28">View this email</a><p>…</p>'
        p = _make_parsed(
            subject="粉虱通訊 No. 28", text_content="本期開場白……", html_content=html
        )
        src = SourceConfig(name="粉虱通訊", tier=Tier.FAN, tags=[])
        art = newsletter_article(p, src)
        assert art is not None
        assert art.url == "https://mailchi.mp/abc/no-28"
        assert art.content == "本期開場白……"
        assert art.source_tier == Tier.FAN

    def test_html_unescape_and_whitespace(self):
        # T4
        p = _make_parsed(text_content="", html_content="<div><p>Hello &amp; goodbye</p></div>")
        src = SourceConfig(name="Test", tier=Tier.SUMMARIZE, tags=[])
        art = newsletter_article(p, src)
        assert art is not None
        assert art.content == "Hello & goodbye"
        assert art.url.startswith("newsletter:")

    def test_empty_body_returns_none_and_warns(self, caplog):
        # T5
        p = _make_parsed(subject="Issue #9", text_content="", html_content="")
        src = SourceConfig(name="Test Newsletter", tier=Tier.SUMMARIZE, tags=[])
        with caplog.at_level(logging.WARNING):
            art = newsletter_article(p, src)
        assert art is None
        assert "Issue #9" in caplog.text and "Test Newsletter" in caplog.text

    def test_different_subjects_get_different_urls(self, source_summarize):
        # T6
        p1 = _make_parsed(subject="曼報 #66", text_content="x")
        p2 = _make_parsed(subject="曼報 #67", text_content="y")
        a1 = newsletter_article(p1, source_summarize)
        a2 = newsletter_article(p2, source_summarize)
        assert a1 is not None and a2 is not None and a1.url != a2.url

    def test_parsed_newsletter_has_no_links_field(self):
        # T7
        p = _make_parsed()
        assert hasattr(p, "links") is False

    def test_summarize_article_carries_unwrapped_reference_urls(self, source_summarize):
        p = _make_parsed(
            subject="曼報 #67｜IMAX",
            text_content="本期主文內容……（本文）",
            html_content=(
                '<a href="https://xx.list-manage.com/track/click?url='
                'https%3A%2F%2Fexample.com%2Fpost%3Fe%3Dtracking">Read</a>'
            ),
        )
        art = newsletter_article(p, source_summarize)
        assert art is not None
        assert art.ref_urls == ["https://example.com/post"]
        assert art.url.startswith("newsletter:")

    def test_view_url_is_not_a_reference_url(self, source_summarize):
        p = _make_parsed(
            text_content="本文",
            html_content=(
                '<a href="https://mailchi.mp/abc/no-28">View</a>'
                '<a href="https://xx.list-manage.com/track/click?url='
                'https%3A%2F%2Fexample.com%2Fa">Read</a>'
            ),
        )
        art = newsletter_article(p, source_summarize)
        assert art is not None
        assert art.url == "https://mailchi.mp/abc/no-28"
        assert art.ref_urls == ["https://example.com/a"]

    def test_plain_text_article_has_no_reference_urls(self, source_summarize):
        art = newsletter_article(
            _make_parsed(text_content="本文", html_content=""), source_summarize
        )
        assert art is not None
        assert art.ref_urls == []

    def test_always_one_article_from_body(self, source_summarize):
        # extra coverage: html only
        p = _make_parsed(text_content="", html_content="<p>only html</p>")
        art = newsletter_article(p, source_summarize)
        assert art is not None
        assert art.content == "only html"

    def test_campaign_archive_normal_host_accepted(self):
        # normal campaign-archive.com host is used as view url
        html = '<a href="https://campaign-archive.com/x?u=1">View</a>'
        p = _make_parsed(text_content="body", html_content=html)
        src = SourceConfig(name="t", tier=Tier.SUMMARIZE, tags=[])
        art = newsletter_article(p, src)
        assert art is not None
        assert art.url == "https://campaign-archive.com/x?u=1"

    def test_campaign_archive_numbered_host_accepted(self):
        # campaign-archive1.com (and sub) is accepted (old code missed)
        html = '<a href="https://us1.campaign-archive1.com/?u=abc&id=def">View</a>'
        p = _make_parsed(text_content="body", html_content=html)
        src = SourceConfig(name="t", tier=Tier.SUMMARIZE, tags=[])
        art = newsletter_article(p, src)
        assert art is not None
        assert "campaign-archive1.com" in art.url

    def test_campaign_archive_evil_not_accepted(self):
        # substring evil like campaign-archive.com.evil.net must NOT be adopted
        html = '<a href="https://campaign-archive.com.evil.net/z">View</a>'
        p = _make_parsed(text_content="body", html_content=html)
        src = SourceConfig(name="t", tier=Tier.SUMMARIZE, tags=[])
        art = newsletter_article(p, src)
        assert art is not None
        assert art.url.startswith("newsletter:")

    def test_mailchi_mp_normal_host_accepted(self):
        html = '<a href="https://mailchi.mp/abc/28">View</a>'
        p = _make_parsed(text_content="body", html_content=html)
        src = SourceConfig(name="t", tier=Tier.SUMMARIZE, tags=[])
        art = newsletter_article(p, src)
        assert art is not None
        assert art.url == "https://mailchi.mp/abc/28"

    def test_mailchi_mp_subdomain_host_accepted(self):
        html = '<a href="https://us1.mailchi.mp/def/29">View</a>'
        p = _make_parsed(text_content="body", html_content=html)
        src = SourceConfig(name="t", tier=Tier.SUMMARIZE, tags=[])
        art = newsletter_article(p, src)
        assert art is not None
        assert art.url == "https://us1.mailchi.mp/def/29"

    def test_mailchi_mp_evil_not_accepted(self):
        html = '<a href="https://mailchi.mp.evil.net/evil">View</a>'
        p = _make_parsed(text_content="body", html_content=html)
        src = SourceConfig(name="t", tier=Tier.SUMMARIZE, tags=[])
        art = newsletter_article(p, src)
        assert art is not None
        assert art.url.startswith("newsletter:")


# Shaped like the real text/plain bodies — join link first, web-version link
# second, both on the sender's host — but invented. The real samples live outside
# the repo and are exercised by tests/test_newsletter_real_fixtures.py.
JOIN_THEN_WEB_VERSION_BODY = (
    "？點擊這裡訂閱 (https://letter.example.com/join) 。\n"
    "\n"
    "閱讀本信件有困難嗎？點擊這裡登入閱讀網頁版 "
    "(https://letter.example.com/posts/power-and-compute-financing) 。\n"
    "https://letter.example.com/\n"
    "\n"
    "** 本期主題：電力與算力融資\n"
)


class TestTextViewUrl:
    def test_labelled_web_version_link_wins_over_the_join_link(self, source_summarize):
        art = newsletter_article(
            _make_parsed(text_content=JOIN_THEN_WEB_VERSION_BODY, html_content=""), source_summarize
        )
        assert art is not None
        assert art.url == ("https://letter.example.com/posts/power-and-compute-financing")

    def test_english_marker(self, source_summarize):
        body = "Hi.\nView in browser (https://example.com/posts/1?utm_source=x)\n"
        art = newsletter_article(_make_parsed(text_content=body, html_content=""), source_summarize)
        assert art is not None
        assert art.url == "https://example.com/posts/1"

    def test_sender_domain_post_wins_over_esp_archive(self, source_summarize):
        art = newsletter_article(
            _make_parsed(
                text_content=JOIN_THEN_WEB_VERSION_BODY,
                html_content='<a href="https://mailchi.mp/abc/28">View</a>',
            ),
            source_summarize,
        )
        assert art is not None
        assert art.url == ("https://letter.example.com/posts/power-and-compute-financing")

    def test_no_marker_stays_synthetic(self, source_summarize):
        body = "本文\n訂閱請點 (https://letter.example.com/join)\n"
        art = newsletter_article(_make_parsed(text_content=body, html_content=""), source_summarize)
        assert art is not None
        assert art.url.startswith("newsletter:")

    def test_nav_line_picks_the_url_after_the_marker_not_the_join_link(self, source_summarize):
        # a footer that collapses nav links onto one line: /join is identical every issue,
        # so adopting it would dedup every later issue away
        body = "訂閱 (https://site.com/join) | 網頁版 (https://site.com/posts/1)\n"
        art = newsletter_article(_make_parsed(text_content=body, html_content=""), source_summarize)
        assert art is not None
        assert art.url == "https://site.com/posts/1"

    def test_trailing_sentence_punctuation_is_not_part_of_the_url(self, source_summarize):
        for body, expected in (
            ("View in browser: https://example.com/posts/1.\n", "https://example.com/posts/1"),
            ("請看網頁版：https://example.com/posts/1。\n", "https://example.com/posts/1"),
        ):
            art = newsletter_article(
                _make_parsed(text_content=body, html_content=""), source_summarize
            )
            assert art is not None
            assert art.url == expected

    def test_mailchimp_click_wrapper_is_unwrapped(self, source_summarize):
        body = (
            "View in browser (https://xx.list-manage.com/track/click?url="
            "https%3A%2F%2Fexample.com%2Fposts%2F1%3Fe%3Dtracking)\n"
        )
        art = newsletter_article(_make_parsed(text_content=body, html_content=""), source_summarize)
        assert art is not None
        assert art.url == "https://example.com/posts/1"


class TestNewsletterIssueUrlUniqueness:
    def test_adjacent_issues_keep_distinct_post_urls(self, source_summarize):
        a = newsletter_article(
            _make_parsed(
                subject="Issue A",
                text_content="訂閱 (https://s.com/join)\n網頁版 (https://s.com/posts/a)",
            ),
            source_summarize,
        )
        b = newsletter_article(
            _make_parsed(
                subject="Issue B",
                text_content="訂閱 (https://s.com/join)\n網頁版 (https://s.com/posts/b)",
            ),
            source_summarize,
        )
        assert a is not None and b is not None
        assert a.url == "https://s.com/posts/a"
        assert b.url == "https://s.com/posts/b"
        assert a.url != b.url
        assert a.url != "https://s.com/join"
        assert b.url != "https://s.com/join"

    def test_join_only_issues_use_distinct_synthetic_urls(self, source_summarize):
        a = newsletter_article(
            _make_parsed(subject="Issue A", text_content="訂閱 (https://s.com/join)"),
            source_summarize,
        )
        b = newsletter_article(
            _make_parsed(subject="Issue B", text_content="訂閱 (https://s.com/join)"),
            source_summarize,
        )
        assert a is not None and b is not None
        assert a.url.startswith("newsletter:")
        assert b.url.startswith("newsletter:")
        assert a.url != b.url


class TestHarvestUrlCandidates:
    def test_html_entity_decoded_and_post_id_stripped(self):
        assert harvest_url_candidates(
            html_content='<a href="https://a.com/p/x?utm_source=n&amp;post_id=7">t</a>',
            text_content="",
        ) == ["https://a.com/p/x"]

    def test_query_survives_a_param_named_like_a_legacy_entity(self):
        # &section= / &times= / &copy= are legacy entities Python expands without a
        # semicolon, so a second unescape pass would collapse the whole query.
        assert harvest_url_candidates(
            html_content='<a href="https://a.com/p/x?ref=y&amp;section=top">t</a>',
            text_content="",
        ) == ["https://a.com/p/x?ref=y&section=top"]

    def test_labelled_url_in_text_strips_trailing_ideographic_full_stop(self):
        assert harvest_url_candidates(
            html_content="",
            text_content="網頁版 (https://a.com/posts/1) 。\n",
        ) == ["https://a.com/posts/1"]

    def test_bare_url_in_text_strips_trailing_period(self):
        assert harvest_url_candidates(
            html_content="",
            text_content="View in browser: https://a.com/posts/1.\n",
        ) == ["https://a.com/posts/1"]

    def test_track_click_unwrapped_and_e_stripped(self):
        assert harvest_url_candidates(
            html_content="",
            text_content="https://xx.list-manage.com/track/click?url=https%3A%2F%2Fa.com%2Fposts%2F1%3Fe%3Dtok",
        ) == ["https://a.com/posts/1"]

    def test_duplicate_anchors_are_kept(self):
        assert harvest_url_candidates(
            html_content='<a href="https://a.com/p/x">1</a><a href="https://a.com/p/x">2</a>',
            text_content="",
        ) == ["https://a.com/p/x", "https://a.com/p/x"]

    def test_malformed_html_still_returns_collected_anchors(self):
        assert harvest_url_candidates(
            html_content='<a href="https://a.com/p/x">1</a><b>未閉合',
            text_content="",
        ) == ["https://a.com/p/x"]


class TestSelectPrimaryContentUrl:
    def test_configured_homepage_host_beats_a_more_frequent_third_party(self):
        candidates = [
            "https://s.com/posts/issue-1",
            "https://blog.acme.com/a/b",
            "https://blog.acme.com/a/c",
            "https://blog.acme.com/a/d",
        ]
        assert select_primary_content_url(candidates, "s.com") == "https://s.com/posts/issue-1"

    def test_falls_back_to_frequency_when_homepage_host_is_absent(self):
        candidates = [
            "https://s.com/posts/issue-1",
            "https://blog.acme.com/a/b",
            "https://blog.acme.com/a/c",
        ]
        assert (
            select_primary_content_url(candidates, "never-linked.com")
            == "https://blog.acme.com/a/b"
        )

    def test_depth_outranks_frequency(self):
        assert (
            select_primary_content_url(
                [
                    "https://a.com/p/one",
                    "https://a.com/p/one",
                    "https://a.com/p/one",
                    "https://a.com/blog/2026/deep",
                ]
            )
            == "https://a.com/blog/2026/deep"
        )

    def test_frequency_breaks_depth_ties(self):
        assert (
            select_primary_content_url(
                ["https://a.com/x/y", "https://a.com/x/y", "https://a.com/s/t"]
            )
            == "https://a.com/x/y"
        )

    def test_dominant_host_then_first_seen_on_ties(self):
        assert (
            select_primary_content_url(
                [
                    "https://a.com/x/y",
                    "https://b.com/deep/er/path",
                    "https://a.com/p/q",
                    "https://a.com/r/s",
                ]
            )
            == "https://a.com/x/y"
        )

    def test_all_shallow_paths_yield_none(self):
        assert (
            select_primary_content_url(
                ["https://a.com/join", "https://a.com/join", "https://a.com/"]
            )
            is None
        )

    def test_esp_hosts_yield_none(self):
        assert (
            select_primary_content_url(
                ["https://mailchi.mp/abc/no-28", "https://xx.list-manage.com/a/b"]
            )
            is None
        )

    def test_empty_candidates_yield_none(self):
        assert select_primary_content_url([]) is None

    def test_full_hostname_not_etld_plus_one(self):
        assert (
            select_primary_content_url(
                [
                    "https://www.patreon.com/ieo/posts/x/y",
                    "https://open.patreon.com/ieo/posts/x/y",
                    "https://www.patreon.com/settings/email/ieo",
                    "https://www.patreon.com/ieo/posts/x/y",
                ]
            )
            == "https://www.patreon.com/ieo/posts/x/y"
        )


class TestNewsletterViewUrlResolutionOrder:
    def test_esp_archive_guard_when_no_sender_post(self):
        html = '<a href="https://mailchi.mp/abc/28">View</a>'
        p = _make_parsed(text_content="本期開場白……", html_content=html)
        src = SourceConfig(name="t", tier=Tier.SUMMARIZE, tags=[])
        art = newsletter_article(p, src)
        assert art is not None
        assert art.url == "https://mailchi.mp/abc/28"

    def test_language_keyword_guard_when_path_too_shallow(self, source_summarize):
        art = newsletter_article(
            _make_parsed(text_content="網頁版 (https://site.com/x)\n", html_content=""),
            source_summarize,
        )
        assert art is not None
        assert art.url == "https://site.com/x"

    def test_synthetic_url_when_no_links(self):
        src = SourceConfig(name="Test Newsletter", tier=Tier.SUMMARIZE, tags=[])
        art = newsletter_article(
            _make_parsed(
                subject="Issue #1",
                text_content="本文",
                html_content="<p>no links</p>",
                source_name="Test Newsletter",
            ),
            src,
        )
        assert art is not None
        expected = "newsletter:" + hashlib.sha256(b"Test NewsletterIssue #1").hexdigest()
        assert art.url == expected

    def test_primary_url_strips_e_and_utm(self, source_summarize):
        art = newsletter_article(
            _make_parsed(
                text_content="hello",
                html_content='<a href="https://site.com/posts/hello?e=tok&utm_source=n">x</a>',
            ),
            source_summarize,
        )
        assert art is not None
        assert art.url == "https://site.com/posts/hello"
        assert "e=" not in art.url
        assert "utm_" not in art.url
