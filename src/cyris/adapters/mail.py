"""Notification sender: email through Cloudflare Email Service's REST API.

No Worker in between: the REST send takes an account API token from any backend.
Mail to a verified Email Routing destination address is free on every plan and
needs no onboarded sending domain; the sender must be an address on one of the
account's routing domains.
https://developers.cloudflare.com/email-service/api/send-emails/rest-api/
"""

import logging

import httpx

from cyris.adapters.cloudflare import API_ROOT, TIMEOUT_SECONDS
from cyris.adapters.notify import period_label, redact_webhook_tokens
from cyris.adapters.output.email_digest import render_digest_email
from cyris.adapters.output.html_digest import _features
from cyris.domain.models import DigestContent, DigestItem

logger = logging.getLogger(__name__)


async def send_mail(
    account_id: str,
    token: str,
    sender: str,
    recipient: str,
    subject: str,
    text: str,
    html: str | None = None,
) -> str:
    """Send one message; return "delivered" or "queued".

    Raises RuntimeError carrying the API's own message. A permanent bounce comes
    back under `success: true`, so success alone is not the answer.
    """
    message = {"to": recipient, "from": sender, "subject": subject, "text": text}
    if html is not None:
        message["html"] = html
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{API_ROOT}/accounts/{account_id}/email/sending/send",
                headers={"Authorization": f"Bearer {token}"},
                json=message,
            )
    except httpx.HTTPError as e:
        raise RuntimeError(f"could not reach the Email Sending API: {e}") from e

    try:
        body = resp.json()
    except ValueError:
        raise RuntimeError(f"the Email Sending API answered HTTP {resp.status_code}") from None

    if not body.get("success"):
        errors = body.get("errors") or [{"message": f"HTTP {resp.status_code}"}]
        raise RuntimeError("; ".join(str(e.get("message", e)) for e in errors))

    result = body.get("result") or {}
    if recipient in (result.get("permanent_bounces") or []):
        raise RuntimeError(f"{recipient} bounced permanently")
    return "queued" if recipient in (result.get("queued") or []) else "delivered"


def _line(item: DigestItem) -> str:
    return f"- {item.title} — {item.link}" if item.link else f"- {item.title}"


def build_digest_mail(
    content: DigestContent, digest_url: str = "", publish_failed: bool = False
) -> tuple[str, str]:
    """The subject and plain-text body: the issue's sections, as the HTML part orders them."""
    title = f"{period_label(content.period)} digest {content.date}"
    lines = [title, ""]
    if digest_url:
        lines.append(f"Read online: {digest_url}")
    elif publish_failed:
        lines.append("Publishing the online edition failed, so this issue has no link.")
    lines += [
        "",
        f"Kept {content.articles_included} of {content.articles_received} articles "
        f"from {content.sources_processed} sources.",
    ]
    features = _features(content)
    sections = [
        ("Top story", [_line(item) for item in features[:1]]),
        ("Features", [_line(item) for item in features[1:]]),
        ("In Focus", [f"- {cluster.heading}" for cluster in content.news_clusters]),
        ("Following", [_line(i) for sec in content.fan_sections for i in sec.items]),
        ("On the Radar", [_line(i) for sec in content.attention_sections for i in sec.items]),
        ("The Wire", [_line(item) for item in content.filtered_headlines]),
    ]
    for label, entries in sections:
        if entries:
            lines += ["", label, *entries]
    return title, "\n".join(lines)


async def send_digest_mail(
    recipient: str,
    sender: str,
    content: DigestContent,
    digest_url: str = "",
    publish_failed: bool = False,
    raw_page: bool = False,
    *,
    account_id: str,
    token: str,
) -> None:
    """Mail one issue's announcement. Does nothing without a recipient.

    Failures are logged, not raised, as `send_discord` does: a lost message must
    not cost the run its record.
    """
    if not recipient:
        return
    subject, text = build_digest_mail(content, digest_url, publish_failed)
    html = render_digest_email(content, digest_url, raw_page, publish_failed)
    try:
        status = await send_mail(account_id, token, sender, recipient, subject, text, html)
        logger.info("Digest mail %s to %s", status, recipient)
    except RuntimeError as e:
        logger.warning("Digest mail to %s failed: %s", recipient, e)


async def send_alert_mail(
    recipient: str,
    sender: str,
    subject: str,
    text: str,
    *,
    account_id: str,
    token: str,
) -> None:
    """Mail a plain-text failure alert. Does nothing without a recipient.

    Failures are logged, not raised, as `send_discord_alert` does: a lost
    message must not mask the run exception that triggered it.
    """
    if not recipient:
        return
    subject = redact_webhook_tokens(subject)
    text = redact_webhook_tokens(text)
    try:
        await send_mail(account_id, token, sender, recipient, subject, text)
    except RuntimeError as e:
        logger.warning("Alert mail to %s failed: %s", recipient, redact_webhook_tokens(str(e)))
