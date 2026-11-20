"""Notification sender: email through Cloudflare Email Service's REST API.

No Worker in between: the REST send takes an account API token from any backend.
Mail to a verified Email Routing destination address is free on every plan and
needs no onboarded sending domain; the sender must be an address on one of the
account's routing domains.
https://developers.cloudflare.com/email-service/api/send-emails/rest-api/
"""

import logging
from collections.abc import Callable

import httpx

from cyris.adapters.cloudflare import API_ROOT, TIMEOUT_SECONDS
from cyris.adapters.notify import redact_webhook_tokens
from cyris.adapters.output.email_digest import raw_page_url, render_digest_email
from cyris.adapters.output.html_digest import Story, citation_links, issue_language
from cyris.adapters.run_alert import alert_message
from cyris.domain.area_layout import AreaSection
from cyris.domain.layers import Sand, layer_by_lane
from cyris.domain.models import DigestContent, DigestItem, DigestSection, NewsParagraph
from cyris.i18n import issue_name, translator
from cyris.service_layer.alerts import RunAlert

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
    *,
    language: str = "",
) -> tuple[str, str]:
    """The subject and plain-text body: Tracking, Stories and Topics, as the HTML part has them.

    Glance is listed only when those three are empty (`Layers.mail_water`).

    Worded as the HTML part is: in the issue's language, else `language`, the current setting.
    """
    t = translator(issue_language(content, language))
    title = t("mail.subject", issue=issue_name(t, content.date, content.started_at))
    lines = [title, ""]
    if digest_url:
        lines.append(t("mail.read_online_at", url=digest_url))
    elif publish_failed:
        lines.append(t("mail.publish_failed_text"))
    if degraded:
        lines += ["", t("issue.degraded")]
    lines += [
        "",
        t(
            "mail.kept",
            included=content.articles_included,
            received=content.articles_received,
            sources=content.sources_processed,
        ),
    ]
    if not content.articles_included:
        if raw_url := raw_page_url(content, digest_url, raw_page):
            lines.append(t("mail.empty_raw_at", url=raw_url))
        else:
            lines.append(t("issue.empty"))
    layers = layer_by_lane(content)
    blocks = [
        (
            t("layer.tracking"),
            [line for topic in layers.tracking for line in _tracking_lines(topic)],
        ),
        (
            t("layer.stories"),
            [line for pebble in layers.pebbles for line in _pebble_lines(pebble.item)],
        ),
        (t("layer.topics"), [line for sand in layers.sand for line in _sand_lines(sand)]),
        (
            t("layer.glance"),
            [line for water in layers.mail_water for line in _pebble_lines(water.item)],
        ),
    ]
    for label, entries in blocks:
        if entries:
            lines += ["", label, *entries]
    return title, "\n".join(lines)


def _tracking_lines(topic: DigestSection) -> list[str]:
    return [f"- {topic.heading}", *(f"  {_line(item)}" for item in topic.items)]


def _pebble_lines(item: DigestItem) -> list[str]:
    """A pebble's title and link, then its summary; no source, as in the HTML part."""
    return [_line(item), *([f"  {item.summary}"] if item.summary else [])]


def _sand_lines(sand: Sand) -> list[str]:
    """A topic's heading and its line, whole; no article link and no source."""
    return [f"- {sand.heading}", *([f"  {sand.line}"] if sand.line else [])]


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
    language: str = "",
) -> dict[str, str]:
    """Mail one issue's announcement; return the outcome for the run summary.

    `language` is the current `digest.output_language`, which words an issue stored with none.

    The outcome is `{"status": "delivered"}` or `"queued"`, `{"status": "skipped"}`
    without a recipient, or `{"status": "failed", "reason": ...}`. Failures are logged
    and returned, not raised, as `send_discord` does: a lost message must not cost the
    run its record.
    """
    if not recipient:
        return {"status": "skipped"}
    subject, text = build_digest_mail(
        content, digest_url, publish_failed, raw_page, degraded, language=language
    )
    html = render_digest_email(
        content, digest_url, raw_page, publish_failed, degraded, language=language
    )
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
    alert: RunAlert,
    *,
    account_id: str,
    token: str,
    wording: Callable[[RunAlert], tuple[str, str]] | None = None,
) -> None:
    """Mail a plain-text failure alert, worded in its language. Does nothing without a recipient.

    `wording` is a hosted tenant's `reader_alert_message`, which withholds the error.

    A failed send is logged, not raised, as `send_discord_alert` does: a lost
    message must not mask the run exception that triggered it. Wording that fails
    raises to `send_alert`, whose per-channel guard logs it.
    """
    if not recipient:
        return
    subject, text = (wording or alert_message)(alert)
    subject = redact_webhook_tokens(subject)
    text = redact_webhook_tokens(text)
    try:
        await send_mail(account_id, token, sender, recipient, subject, text)
    except RuntimeError as e:
        logger.warning("Alert mail to %s failed: %s", recipient, redact_webhook_tokens(str(e)))
