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
from cyris.adapters.output.email_digest import raw_page_url, render_digest_email
from cyris.adapters.output.html_digest import Story, citation_links
from cyris.domain.area_layout import AreaSection
from cyris.domain.layers import Sand, layer_by_lane
from cyris.domain.models import DigestContent, DigestItem, DigestSection, NewsParagraph

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


def _line(item: DigestItem | Story) -> str:
    return f"- {item.title} — {item.link}" if item.link else f"- {item.title}"


def _wire_lines(item: DigestItem) -> list[str]:
    return [_line(item), f"  {item.summary}"] if item.summary else [_line(item)]


def _paragraph_lines(paragraph: NewsParagraph) -> list[str]:
    """The paragraph, then each cited article's link on its own indented line."""
    return [
        paragraph.text,
        *(
            f"  {label} — {link}" if link else f"  {label}"
            for unit in paragraph.units
            for label, link in citation_links(unit)
        ),
    ]


def _story_lines(story: Story) -> list[str]:
    if not story.grouped:
        return [_line(story)]
    return [_line(story), *(f"  {_line(member)}" for member in story.members)]


def build_digest_mail(
    content: DigestContent,
    digest_url: str = "",
    publish_failed: bool = False,
    raw_page: bool = False,
    degraded: bool = False,
) -> tuple[str, str]:
    """The subject and plain-text body: Tracking and the three layers, as the HTML part has them."""
    title = f"{period_label(content.period)} digest {content.date}"
    lines = [title, ""]
    if digest_url:
        lines.append(f"Read online: {digest_url}")
    elif publish_failed:
        lines.append("Publishing the online edition failed, so this issue has no link.")
    if degraded:
        lines += [
            "",
            "Some or all of this issue is unscored or plain excerpts: the configured LLM could "
            "not be used for every step this run. Check the provider and its key on Settings.",
        ]
    lines += [
        "",
        f"Kept {content.articles_included} of {content.articles_received} articles "
        f"from {content.sources_processed} sources.",
    ]
    if not content.articles_included:
        raw_url = raw_page_url(content, digest_url, raw_page)
        lines.append(
            "Nothing is in this issue: this run judged the "
            f"{content.articles_received} articles it received and kept none."
            + (f" All articles lists each one with its verdict: {raw_url}" if raw_url else "")
        )
    layers = layer_by_lane(content)
    blocks = [
        ("Tracking", [line for topic in layers.tracking for line in _tracking_lines(topic)]),
        ("Stories", [line for pebble in layers.pebbles for line in _pebble_lines(pebble.item)]),
        ("Topics", [line for sand in layers.sand for line in _sand_lines(sand)]),
        ("Headlines", [line for water in layers.water for line in _wire_lines(water.item)]),
    ]
    for label, entries in blocks:
        if entries:
            lines += ["", label, *entries]
    return title, "\n".join(lines)


def _tracking_lines(topic: DigestSection) -> list[str]:
    return [f"- {topic.heading}", *(f"  {_line(item)}" for item in topic.items)]


def _pebble_lines(item: DigestItem) -> list[str]:
    """A pebble's title and link, its summary, then its source: the one layer naming it."""
    return [
        _line(item),
        *([f"  {item.summary}"] if item.summary else []),
        *([f"  {', '.join(item.sources)}"] if item.sources else []),
    ]


def _sand_lines(sand: Sand) -> list[str]:
    """A topic's heading and line, then each article with its source, as the page's fold."""
    members = [
        f"  {_line(m)}" + (f" ({', '.join(m.sources)})" if m.sources else "") for m in sand.members
    ]
    return [f"- {sand.heading}", *([f"  {sand.line}"] if sand.line else []), *members]


def _area_entries(section: AreaSection) -> list[str]:
    features = [Story(i.title, i.summary, (i,)) for s in section.features for i in s.items]
    return [
        *(line for story in features for line in _story_lines(story)),
        *(_line(i) for s in section.following for i in s.items),
        *(f"- {cluster.heading}" for cluster in section.clusters),
        *(line for row in section.rows for line in _wire_lines(row.item)),
        *(line for paragraph in section.paragraphs for line in _paragraph_lines(paragraph)),
    ]


async def send_digest_mail(
    recipient: str,
    sender: str,
    content: DigestContent,
    digest_url: str = "",
    publish_failed: bool = False,
    raw_page: bool = False,
    degraded: bool = False,
    *,
    account_id: str,
    token: str,
) -> dict[str, str]:
    """Mail one issue's announcement; return the outcome for the run summary.

    The outcome is `{"status": "delivered"}` or `"queued"`, `{"status": "skipped"}`
    without a recipient, or `{"status": "failed", "reason": ...}`. Failures are logged
    and returned, not raised, as `send_discord` does: a lost message must not cost the
    run its record.
    """
    if not recipient:
        return {"status": "skipped"}
    subject, text = build_digest_mail(content, digest_url, publish_failed, raw_page, degraded)
    html = render_digest_email(content, digest_url, raw_page, publish_failed, degraded)
    try:
        status = await send_mail(account_id, token, sender, recipient, subject, text, html)
    except RuntimeError as e:
        logger.warning("Digest mail to %s failed: %s", recipient, e)
        return {"status": "failed", "reason": str(e)}
    logger.info("Digest mail %s to %s", status, recipient)
    return {"status": status}


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
