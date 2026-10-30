"""The email channel: Cloudflare Email Service's REST send, and the digest message."""

import json
import logging

import httpx
import pytest
import respx
from layered_issue import UNNAMED_SOURCES, four_layer_issue

from cyris.adapters.mail import build_digest_mail, send_alert_mail, send_digest_mail, send_mail
from cyris.domain.models import DigestContent, DigestItem

pytestmark = pytest.mark.unit

ACCOUNT = "acct-123"
SEND_URL = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/email/sending/send"


def _content() -> DigestContent:
    return DigestContent(
        date="2026-09-27",
        period="morning",
        sources_processed=12,
        articles_received=300,
        articles_included=15,
    )


def _result(**lists: list[str]) -> dict:
    result = {"delivered": [], "permanent_bounces": [], "queued": []} | lists
    return {"success": True, "errors": [], "messages": [], "result": result}


class TestSendMail:
    @respx.mock
    async def test_a_delivered_message_posts_the_fields_and_says_so(self):
        route = respx.post(SEND_URL).mock(
            return_value=httpx.Response(200, json=_result(delivered=["me@example.org"]))
        )

        status = await send_mail(
            ACCOUNT, "tok", "digest@example.org", "me@example.org", "Subject", "Body"
        )

        assert status == "delivered"
        assert route.call_count == 1
        request = route.calls.last.request
        assert request.headers["Authorization"] == "Bearer tok"
        assert json.loads(request.content) == {
            "to": "me@example.org",
            "from": "digest@example.org",
            "subject": "Subject",
            "text": "Body",
        }

    @respx.mock
    async def test_a_queued_message_counts_as_sent(self):
        route = respx.post(SEND_URL).mock(
            return_value=httpx.Response(200, json=_result(queued=["me@example.org"]))
        )

        assert await send_mail(ACCOUNT, "tok", "d@example.org", "me@example.org", "S", "B") == (
            "queued"
        )
        assert route.call_count == 1

    @respx.mock
    async def test_the_html_part_rides_along_when_given(self):
        route = respx.post(SEND_URL).mock(
            return_value=httpx.Response(200, json=_result(delivered=["me@example.org"]))
        )

        await send_mail(
            ACCOUNT, "tok", "d@example.org", "me@example.org", "S", "B", html="<p>B</p>"
        )

        assert route.call_count == 1
        assert json.loads(route.calls.last.request.content)["html"] == "<p>B</p>"

    @respx.mock
    async def test_a_permanent_bounce_under_success_is_still_a_failure(self):
        route = respx.post(SEND_URL).mock(
            return_value=httpx.Response(200, json=_result(permanent_bounces=["me@example.org"]))
        )

        with pytest.raises(RuntimeError, match="me@example.org bounced permanently"):
            await send_mail(ACCOUNT, "tok", "d@example.org", "me@example.org", "S", "B")

        assert route.call_count == 1

    @respx.mock
    async def test_a_refusal_carries_the_api_message(self):
        route = respx.post(SEND_URL).mock(
            return_value=httpx.Response(
                403,
                json={
                    "success": False,
                    "errors": [
                        {"code": 10102, "message": "email.sending.error.authentication.forbidden"}
                    ],
                    "messages": [],
                    "result": None,
                },
            )
        )

        with pytest.raises(RuntimeError, match="authentication.forbidden"):
            await send_mail(ACCOUNT, "tok", "d@example.org", "me@example.org", "S", "B")

        assert route.call_count == 1

    @respx.mock
    async def test_a_body_that_is_not_json_names_the_status(self):
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(502, text="Bad gateway"))

        with pytest.raises(RuntimeError, match="HTTP 502"):
            await send_mail(ACCOUNT, "tok", "d@example.org", "me@example.org", "S", "B")

        assert route.call_count == 1

    @respx.mock
    async def test_an_unreachable_api_is_named(self):
        route = respx.post(SEND_URL).mock(side_effect=httpx.ConnectError("no route"))

        with pytest.raises(RuntimeError, match="could not reach the Email Sending API"):
            await send_mail(ACCOUNT, "tok", "d@example.org", "me@example.org", "S", "B")

        assert route.call_count == 1


class TestDigestMail:
    def test_the_message_names_the_issue_and_links_it(self):
        subject, text = build_digest_mail(
            _content(), "https://digest.example.org/2026-09-27-morning"
        )

        assert subject == "Morning digest 2026-09-27"
        assert "https://digest.example.org/2026-09-27-morning" in text
        assert "Kept 15 of 300 articles from 12 sources" in text

    def test_a_failed_publish_says_there_is_no_link(self):
        _, text = build_digest_mail(_content(), "", publish_failed=True)

        assert "Publishing the online edition failed" in text
        assert "http" not in text

    def test_the_text_part_is_tracking_then_the_three_layers(self):
        _, text = build_digest_mail(four_layer_issue())

        lines = text.splitlines()
        assert lines[lines.index("Tracking") :] == [
            "Tracking",
            "- Tracked topic",
            "  - A tracked hit — https://tracked.example.test/hit",
            "",
            "Stories",
            "- A long read — https://pebble.example.test/read",
            "  Why it matters.",
            "  SRC-PEBBLE",
            "- A followed post — https://fan.example.test/post",
            "  SRC-FAN",
            "",
            "Topics",
            "- Two reports of one event",
            "  Excerpt a",
            "  - Report a — https://wire-a.example.test/a (SRC-WIRE-a)",
            "  - Report b — https://wire-b.example.test/b (SRC-WIRE-b)",
            "",
            "Headlines",
            "- One report — https://lone.example.test/1",
            "  Its summary.",
            "- A headline — https://headline.example.test/1",
            "  The filter's sentence.",
        ]

    def test_an_empty_layer_has_no_label(self):
        content = four_layer_issue().model_copy(update={"news_clusters": [], "tracked_topics": []})

        _, text = build_digest_mail(content)

        labels = {"Tracking", "Stories", "Topics", "Headlines"}
        assert [line for line in text.splitlines() if line in labels] == ["Stories", "Headlines"]

    def test_no_source_is_named_outside_a_pebble_or_a_sand_member(self):
        _, text = build_digest_mail(four_layer_issue())
        outside = [line for line in text.splitlines() if not line.startswith("  - Report")]

        for source in UNNAMED_SOURCES:
            assert not any(source in line for line in outside), source

    def test_the_text_part_puts_a_wire_rows_summary_on_the_line_below_it(self):
        content = _content()
        content.filtered_headlines = [
            DigestItem(
                title="Wire One", summary="One sentence.", sources=["W"], urls=["https://w.test/1"]
            ),
            DigestItem(title="Wire Two", summary="", sources=["W"], urls=["https://w.test/2"]),
        ]

        _, text = build_digest_mail(content, "https://digest.example.org/x")

        lines = text.splitlines()
        at = lines.index("- Wire One — https://w.test/1")
        assert lines[at:] == [
            "- Wire One — https://w.test/1",
            "  One sentence.",
            "- Wire Two — https://w.test/2",
        ]

    @respx.mock
    async def test_it_sends_through_the_rest_api(self):
        route = respx.post(SEND_URL).mock(
            return_value=httpx.Response(200, json=_result(delivered=["me@example.org"]))
        )

        await send_digest_mail(
            "me@example.org",
            "digest@example.org",
            _content(),
            digest_url="https://digest.example.org/x",
            account_id=ACCOUNT,
            token="tok",
        )

        assert route.call_count == 1
        assert route.calls.last.request.headers["Authorization"] == "Bearer tok"
        sent = json.loads(route.calls.last.request.content)
        assert sent["subject"] == "Morning digest 2026-09-27"
        assert "https://digest.example.org/x" in sent["text"]
        assert sent["html"].startswith("<!DOCTYPE html>")
        assert 'href="https://digest.example.org/x"' in sent["html"]

    @pytest.mark.parametrize("degraded", [True, False])
    @respx.mock
    async def test_the_degraded_verdict_reaches_the_html_body(self, degraded):
        route = respx.post(SEND_URL).mock(
            return_value=httpx.Response(200, json=_result(delivered=["me@example.org"]))
        )

        await send_digest_mail(
            "me@example.org",
            "digest@example.org",
            _content(),
            degraded=degraded,
            account_id=ACCOUNT,
            token="tok",
        )

        assert route.call_count == 1
        sent = json.loads(route.calls.last.request.content)
        assert (
            '<p class="notice">Some or all of this issue is unscored or plain excerpts'
            in sent["html"]
        ) is degraded

    @respx.mock
    async def test_no_recipient_sends_nothing(self):
        await send_digest_mail("", "digest@example.org", _content(), account_id=ACCOUNT, token="t")

        assert respx.calls.call_count == 0

    @respx.mock
    async def test_a_failure_is_logged_and_not_raised(self, caplog):
        route = respx.post(SEND_URL).mock(
            return_value=httpx.Response(
                403,
                json={"success": False, "errors": [{"code": 10102, "message": "forbidden"}]},
            )
        )

        with caplog.at_level(logging.WARNING, logger="cyris.adapters.mail"):
            await send_digest_mail(
                "me@example.org", "d@example.org", _content(), account_id=ACCOUNT, token="t"
            )

        assert route.call_count == 1
        assert "forbidden" in caplog.text

    @respx.mock
    async def test_the_outcome_names_a_failure_and_its_reason(self):
        route = respx.post(SEND_URL).mock(
            return_value=httpx.Response(
                403,
                json={"success": False, "errors": [{"code": 10102, "message": "forbidden"}]},
            )
        )

        outcome = await send_digest_mail(
            "me@example.org", "d@example.org", _content(), account_id=ACCOUNT, token="t"
        )

        assert route.call_count == 1
        assert outcome == {"status": "failed", "reason": "forbidden"}

    @pytest.mark.parametrize("status", ["delivered", "queued"])
    @respx.mock
    async def test_the_outcome_names_what_the_api_did_with_the_mail(self, status):
        route = respx.post(SEND_URL).mock(
            return_value=httpx.Response(200, json=_result(**{status: ["me@example.org"]}))
        )

        outcome = await send_digest_mail(
            "me@example.org", "d@example.org", _content(), account_id=ACCOUNT, token="t"
        )

        assert route.call_count == 1
        assert outcome == {"status": status}

    async def test_the_outcome_of_no_recipient_is_skipped(self):
        outcome = await send_digest_mail(
            "", "d@example.org", _content(), account_id=ACCOUNT, token="t"
        )

        assert outcome == {"status": "skipped"}


class TestDigestMailText:
    EMPTY = "Nothing is in this issue: this run judged the 300 articles it received and kept none."
    DEGRADED = "Some or all of this issue is unscored or plain excerpts"

    @staticmethod
    def _empty() -> DigestContent:
        return _content().model_copy(update={"articles_included": 0})

    def test_an_empty_issue_says_why_and_links_all_articles(self):
        _, text = build_digest_mail(
            self._empty(), "https://digest.example.org/2026-09-27-morning", raw_page=True
        )

        assert (
            f"{self.EMPTY} All articles lists each one with its verdict: "
            "https://digest.example.org/2026-09-27-morning-raw"
        ) in text

    @pytest.mark.parametrize(
        ("digest_url", "raw_page"),
        [("https://digest.example.org/2026-09-27-morning", False), ("", True)],
        ids=["no-raw", "no-site"],
    )
    def test_an_empty_issue_without_a_raw_page_links_nothing(self, digest_url, raw_page):
        _, text = build_digest_mail(self._empty(), digest_url, raw_page=raw_page)

        assert self.EMPTY in text
        assert "-raw" not in text

    def test_an_issue_with_articles_has_no_empty_sentence(self):
        _, text = build_digest_mail(_content(), raw_page=True)

        assert "kept none" not in text

    @pytest.mark.parametrize("degraded", [True, False])
    def test_only_a_degraded_issue_carries_the_degraded_line(self, degraded):
        _, text = build_digest_mail(_content(), degraded=degraded)

        assert (self.DEGRADED in text) is degraded

    @pytest.mark.parametrize("degraded", [True, False])
    @respx.mock
    async def test_the_sent_text_part_carries_the_verdict(self, degraded):
        route = respx.post(SEND_URL).mock(
            return_value=httpx.Response(200, json=_result(delivered=["me@example.org"]))
        )

        await send_digest_mail(
            "me@example.org",
            "digest@example.org",
            _content(),
            degraded=degraded,
            account_id=ACCOUNT,
            token="tok",
        )

        assert route.call_count == 1
        sent = json.loads(route.calls.last.request.content)
        assert (self.DEGRADED in sent["text"]) is degraded


class TestAlertMail:
    @respx.mock
    async def test_posts_plain_text_without_html(self):
        route = respx.post(SEND_URL).mock(
            return_value=httpx.Response(200, json=_result(delivered=["me@example.org"]))
        )

        await send_alert_mail(
            "me@example.org",
            "digest@example.org",
            "S",
            "B",
            account_id=ACCOUNT,
            token="tok",
        )

        assert route.call_count == 1
        assert route.calls.last.request.headers["Authorization"] == "Bearer tok"
        assert json.loads(route.calls.last.request.content) == {
            "to": "me@example.org",
            "from": "digest@example.org",
            "subject": "S",
            "text": "B",
        }

    @respx.mock
    async def test_empty_recipient_sends_nothing(self):
        await send_alert_mail("", "digest@example.org", "S", "B", account_id=ACCOUNT, token="tok")

        assert respx.calls.call_count == 0

    @respx.mock
    async def test_refusal_is_logged_not_raised(self, caplog):
        route = respx.post(SEND_URL).mock(
            return_value=httpx.Response(
                403,
                json={"success": False, "errors": [{"code": 10102, "message": "forbidden"}]},
            )
        )

        with caplog.at_level(logging.WARNING):
            result = await send_alert_mail(
                "me@example.org",
                "digest@example.org",
                "S",
                "B",
                account_id=ACCOUNT,
                token="tok-secret",
            )

        assert result is None
        assert route.call_count == 1
        assert "forbidden" in caplog.text
        assert "tok-secret" not in caplog.text

    @respx.mock
    async def test_subject_and_text_redact_webhook_tokens(self):
        route = respx.post(SEND_URL).mock(
            return_value=httpx.Response(200, json=_result(delivered=["me@example.org"]))
        )
        leak = (
            "HTTPStatusError: Client error '404 Not Found' for url "
            "'https://discord.com/api/webhooks/999/s3cr3t-token'"
        )
        masked = "https://discord.com/api/webhooks/999/\u2022\u2022\u2022\u2022"

        await send_alert_mail(
            "me@example.org",
            "digest@example.org",
            leak,
            leak,
            account_id=ACCOUNT,
            token="tok",
        )

        assert route.call_count == 1
        sent = json.loads(route.calls.last.request.content)
        assert "s3cr3t-token" not in sent["subject"]
        assert "s3cr3t-token" not in sent["text"]
        assert masked in sent["subject"]
        assert masked in sent["text"]


def _paragraph_issue():
    from cyris.domain.models import DigestContent, DigestItem, NewsParagraph

    pair = DigestItem(
        title="Quake",
        summary="",
        sources=["Wire A", "Wire B"],
        urls=["https://a.test/quake", "https://b.test/quake"],
        area="World",
    )
    letter = DigestItem(
        title="Letter",
        summary="",
        sources=["Letters"],
        urls=["newsletter:abc"],
        area="World",
    )
    return DigestContent(
        date="2026-10-27",
        period="morning",
        sources_processed=2,
        articles_received=3,
        articles_included=3,
        area_order=["World"],
        news_paragraphs=[
            NewsParagraph(text="A quake struck; a letter followed.", units=[pair, letter])
        ],
    )


def test_the_plain_text_part_takes_a_legacy_paragraphs_layers_from_its_units() -> None:
    _, body = build_digest_mail(_paragraph_issue())
    assert body.endswith(
        "Topics\n- Quake\n  A quake struck; a letter followed.\n"
        "  - Wire A — https://a.test/quake\n  - Wire B — https://b.test/quake\n"
        "\nHeadlines\n- Letter"
    )
