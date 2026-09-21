"""Notification sender: Discord webhook."""

import logging
import re

import httpx

from cyris.domain.models import DigestContent, DigestSection, is_degraded_run

logger = logging.getLogger(__name__)

# The hosts are Discord's API endpoints, i.e. protocol structure, not vocabulary.
_DISCORD_WEBHOOK = re.compile(
    r"^https://(?:discord|discordapp)\.com/api(?:/v\d+)?/webhooks/([^/]+)/([^/]+)$"
)

# What a masked token renders as. Named because two other modules have to
# recognise it: the settings page prefills it, and the probe must refuse it
# before spending a request on a token that was never real.
WEBHOOK_MASK = "\u2022" * 4


def parse_discord_webhook_url(url: str) -> tuple[str, str] | None:
    """Return (id, token) if url is a Discord webhook, else None. No network."""
    if not url:
        return None
    match = _DISCORD_WEBHOOK.match(url)
    if not match:
        return None
    return match.group(1), match.group(2)


def is_masked_webhook_url(url: str) -> bool:
    """True if url carries the mask, i.e. came back from this module rather than Discord."""
    return WEBHOOK_MASK[0] in url


def mask_discord_webhook_url(url: str) -> str:
    """Render a webhook URL with its posting token replaced."""
    if not url:
        return ""
    if parse_discord_webhook_url(url) is None:
        return WEBHOOK_MASK
    return f"{url.rsplit('/', 1)[0]}/{WEBHOOK_MASK}"


# The same endpoint structure as `_DISCORD_WEBHOOK`, unanchored: this one finds
# the URL *inside* a log line or a traceback rather than validating one on its own.
_DISCORD_WEBHOOK_IN_TEXT = re.compile(
    r"(https://(?:discord|discordapp)\.com/api(?:/v\d+)?/webhooks/[^/\s]+)/([^/\s\"'<>]+)"
)


def redact_webhook_tokens(text: str) -> str:
    """Mask the posting token of every Discord webhook URL appearing in `text`.

    The id is left readable: it names which webhook without being the credential,
    and a masked line nobody can trace back to a webhook is no longer a log line.
    """
    return _DISCORD_WEBHOOK_IN_TEXT.sub(rf"\1/{WEBHOOK_MASK}", text)


def _render_section_embed(section: DigestSection) -> str:
    """Render a DigestSection as Discord markdown text."""
    lines: list[str] = []
    if not section.items:
        return ""

    first = section.items[0]
    # Heading with link
    if first.urls:
        lines.append(f"### [{section.heading}]({first.urls[0]})")
    else:
        lines.append(f"### {section.heading}")

    if section.description:
        lines.append(section.description)

    if first.summary:
        lines.append(first.summary)

    all_sources: list[str] = []
    for item in section.items:
        all_sources.extend(item.sources)
    unique_sources = list(dict.fromkeys(all_sources))
    if unique_sources:
        lines.append(f"*{', '.join(unique_sources)}*")

    lines.append("")
    return "\n".join(lines)


def build_discord_embeds(
    content: DigestContent, digest_url: str = "", publish_failed: bool = False
) -> list[dict]:
    """Build Discord embed objects from DigestContent.

    Returns a list of embed dicts ready for the Discord webhook payload.
    Discord limits: 4096 chars per embed description, max 10 embeds per message.
    Embed order mirrors the HTML digest's section order.
    """
    period_labels = {"morning": "Morning", "evening": "Evening"}
    label = period_labels.get(content.period, content.period)
    embeds: list[dict] = []

    # --- Featured articles ---
    if content.featured_articles:
        lines = []
        for section in content.featured_articles:
            lines.append(_render_section_embed(section))
        text = "\n".join(lines).strip()
        if text:
            embeds.append({"title": "⭐ Featured", "description": text[:4096], "color": 0xF1C40F})

    # --- News clusters ---
    if content.news_clusters:
        lines = []
        for section in content.news_clusters:
            lines.append(_render_section_embed(section))
        text = "\n".join(lines).strip()
        if text:
            embeds.append({"title": "📰 News", "description": text[:4096], "color": 0xFEE75C})

    # --- Fan sections (followed groups — own channel) ---
    if content.fan_sections:
        lines = []
        for section in content.fan_sections:
            lines.append(f"### {section.heading}")
            for item in section.items:
                title_part = (
                    f"**[{item.title}]({item.urls[0]})**" if item.urls else f"**{item.title}**"
                )
                summary_str = f" — {item.summary}" if item.summary else ""
                lines.append(f"- {title_part}{summary_str}")
            lines.append("")
        text = "\n".join(lines).strip()
        if text:
            embeds.append({"title": "📣 Following", "description": text[:4096], "color": 0xEB459E})

    # --- Thematic summaries ---
    if content.thematic_summaries:
        lines = []
        for section in content.thematic_summaries:
            lines.append(_render_section_embed(section))
        text = "\n".join(lines).strip()
        if text:
            embeds.append({"title": "📋 Themes", "description": text[:4096], "color": 0x5865F2})

    # --- Attention sections ---
    if content.attention_sections:
        lines = []
        for section in content.attention_sections:
            lines.append(_render_section_embed(section))
        text = "\n".join(lines).strip()
        if text:
            embeds.append(
                {"title": "👀 Worth a look", "description": text[:4096], "color": 0x9B59B6}
            )

    # --- Filtered headlines ---
    if content.filtered_headlines:
        lines = []
        for item in content.filtered_headlines:
            source_str = f" ({', '.join(item.sources)})" if item.sources else ""
            title_part = f"**[{item.title}]({item.urls[0]})**" if item.urls else f"**{item.title}**"
            lines.append(f"- {title_part}{source_str}")
            if item.summary:
                summary = item.summary if len(item.summary) <= 80 else item.summary[:80] + "…"
                lines.append(f"  {summary}")
        text = "\n".join(lines).strip()
        if text:
            embeds.append(
                {
                    "title": f"📌 Other headlines ({len(content.filtered_headlines)})",
                    "description": text[:4096],
                    "color": 0x95A5A6,
                }
            )

    # --- Stats ---
    pct = (
        f"{content.articles_included / content.articles_received * 100:.1f}%"
        if content.articles_received > 0
        else "N/A"
    )
    stats_lines = []
    if digest_url:
        stats_lines.append(f"📖 [Read online]({digest_url})")
    elif publish_failed:
        stats_lines.append("⚠️ Publishing the online edition failed")
    stats_lines += [
        f"📊 Sources **{content.sources_processed}**",
        f"📥 Articles **{content.articles_received}**",
        f"✅ Kept **{content.articles_included}** ({pct})",
    ]
    synthetic = content.synthetic_url_count
    dead = content.dead_link_count
    if (synthetic or 0) or (dead or 0):
        # The two counts have different scopes on purpose — synthetic covers every
        # article fetched this run, dead covers what actually reached the digest.
        # Saying so keeps them from reading as a contradiction next to the kept count.
        stats_lines.append(
            f"⚠️ {synthetic or 0} newsletter issue(s) fetched with no canonical link; "
            f"{dead or 0} item(s) in this digest have no link to follow"
        )
    if content.usage.api_calls > 0 and content.usage.estimated_cost is not None:
        stats_lines.append(f"💰 Cost **${content.usage.estimated_cost:.4f}**")

    embeds.append(
        {
            "title": f"{label} {content.date}",
            "description": "\n".join(stats_lines),
            "color": 0x57F287,
        }
    )

    return embeds


def build_discord_payload(
    content: DigestContent, digest_url: str = "", publish_failed: bool = False
) -> dict:
    """Build the Discord webhook payload for a digest."""
    payload = {"embeds": build_discord_embeds(content, digest_url, publish_failed)}
    if is_degraded_run(content.usage):
        payload["content"] = (
            f"⚠️ Degraded digest: LLM {content.usage.model} was configured but this run used 0 "
            "input tokens, so scores and summaries are excerpts."
        )
    return payload


async def send_discord(
    webhook_url: str,
    content: DigestContent,
    digest_url: str = "",
    publish_failed: bool = False,
) -> None:
    """Send digest content to Discord via webhook.

    Does nothing if webhook_url is empty. Failures are logged but not raised.
    When digest_url is set, a link to the online (Cloudflare Pages) digest is
    included in the stats embed; when publishing was attempted and failed, the
    missing link is called out instead of silently omitted.
    """
    if not webhook_url:
        return

    payload = build_discord_payload(content, digest_url, publish_failed)

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(webhook_url, json=payload)
            resp.raise_for_status()
            logger.debug("Discord webhook sent: %d embeds", len(payload["embeds"]))
    except httpx.HTTPError:
        logger.warning("Discord webhook failed", exc_info=True)
