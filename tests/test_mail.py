"""The email channel: Cloudflare Email Service's REST send, and the digest message."""

import json
import logging

import httpx
import pytest
import respx

from cyris.adapters.mail import build_digest_mail, send_alert_mail, send_digest_mail, send_mail
from cyris.domain.models import DigestContent, DigestItem, DigestSection

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
        respx.post(SEND_URL).mock(
            return_value=httpx.Response(200, json=_result(queued=["me@example.org"]))
        )

        assert await send_mail(ACCOUNT, "tok", "d@example.org", "me@example.org", "S", "B") == (
            "queued"
        )

    @respx.mock
    async def test_the_html_part_rides_along_when_given(self):
        route = respx.post(SEND_URL).mock(
            return_value=httpx.Response(200, json=_result(delivered=["me@example.org"]))
        )

        await send_mail(
            ACCOUNT, "tok", "d@example.org", "me@example.org", "S", "B", html="<p>B</p>"
        )

        assert json.loads(route.calls.last.request.content)["html"] == "<p>B</p>"

    @respx.mock
    async def test_a_permanent_bounce_under_success_is_still_a_failure(self):
        respx.post(SEND_URL).mock(
            return_value=httpx.Response(200, json=_result(permanent_bounces=["me@example.org"]))
        )

        with pytest.raises(RuntimeError, match="me@example.org bounced permanently"):
            await send_mail(ACCOUNT, "tok", "d@example.org", "me@example.org", "S", "B")

    @respx.mock
    async def test_a_refusal_carries_the_api_message(self):
        respx.post(SEND_URL).mock(
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

    @respx.mock
    async def test_a_body_that_is_not_json_names_the_status(self):
        respx.post(SEND_URL).mock(return_value=httpx.Response(502, text="Bad gateway"))

        with pytest.raises(RuntimeError, match="HTTP 502"):
            await send_mail(ACCOUNT, "tok", "d@example.org", "me@example.org", "S", "B")

    @respx.mock
    async def test_an_unreachable_api_is_named(self):
        respx.post(SEND_URL).mock(side_effect=httpx.ConnectError("no route"))

        with pytest.raises(RuntimeError, match="could not reach the Email Sending API"):
            await send_mail(ACCOUNT, "tok", "d@example.org", "me@example.org", "S", "B")


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

    def test_the_text_part_lists_what_the_issue_holds(self):
        content = _content()
        content.news_clusters = [DigestSection(heading="Cluster Heading", items=[])]
        content.filtered_headlines = [
            DigestItem(title="Wire One", summary="", sources=["W"], urls=["https://w.test/1"])
        ]

        _, text = build_digest_mail(content, "https://digest.example.org/x")

        assert "In Focus" in text and "Cluster Heading" in text
        assert "The Wire" in text and "Wire One — https://w.test/1" in text

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

        sent = json.loads(route.calls.last.request.content)
        assert sent["subject"] == "Morning digest 2026-09-27"
        assert "https://digest.example.org/x" in sent["text"]
        assert sent["html"].startswith("<!DOCTYPE html>")
        assert 'href="https://digest.example.org/x"' in sent["html"]

    @respx.mock
    async def test_no_recipient_sends_nothing(self):
        await send_digest_mail("", "digest@example.org", _content(), account_id=ACCOUNT, token="t")

        assert respx.calls.call_count == 0

    @respx.mock
    async def test_a_failure_is_logged_and_not_raised(self, caplog):
        respx.post(SEND_URL).mock(
            return_value=httpx.Response(
                403,
                json={"success": False, "errors": [{"code": 10102, "message": "forbidden"}]},
            )
        )

        with caplog.at_level(logging.WARNING, logger="cyris.adapters.mail"):
            await send_digest_mail(
                "me@example.org", "d@example.org", _content(), account_id=ACCOUNT, token="t"
            )

        assert "forbidden" in caplog.text


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
