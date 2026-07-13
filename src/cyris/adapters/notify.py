"""Notification senders: ntfy.sh and Discord webhook."""

import logging

import httpx

from cyris.domain.models import DigestContent, DigestSection

logger = logging.getLogger(__name__)


async def send_ntfy(
    topic: str,
    title: str,
    message: str,
    server: str = "https://ntfy.sh",
    tags: str = "",
    priority: str = "default",
) -> None:
    """Send a notification via ntfy.sh using JSON API.

    Does nothing if topic is empty. Failures are logged but not raised.
    """
    if not topic:
        return

    url = f"{server.rstrip('/')}"
    payload: dict[str, object] = {
        "topic": topic,
        "title": title,
        "message": message,
    }
    if tags:
        payload["tags"] = tags.split(",")
    if priority != "default":
        payload["priority"] = {"low": 2, "default": 3, "high": 4, "urgent": 5}.get(priority, 3)

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            logger.debug("ntfy sent: %s", title)
    except httpx.HTTPError:
        logger.warning("ntfy notification failed: %s", title, exc_info=True)


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


def build_discord_embeds(content: DigestContent) -> list[dict]:
    """Build Discord embed objects from DigestContent.

    Returns a list of embed dicts ready for the Discord webhook payload.
    Discord limits: 4096 chars per embed description, max 10 embeds per message.
    Embed order mirrors the Obsidian digest structure.
    """
    period_labels = {"morning": "早報", "evening": "晚報"}
    label = period_labels.get(content.period, content.period)
    embeds: list[dict] = []

    # --- Tracked topic updates ---
    if content.tracked_updates and content.tracked_updates.items:
        section = content.tracked_updates
        lines: list[str] = []
        lines.append(f"### {section.heading}")
        for item in section.items:
            title_part = f"**[{item.title}]({item.urls[0]})**" if item.urls else f"**{item.title}**"
            summary_str = f" — {item.summary}" if item.summary else ""
            source_str = f" ({', '.join(item.sources)})" if item.sources else ""
            lines.append(f"- {title_part}{summary_str}{source_str}")
        text = "\n".join(lines).strip()
        if text:
            embeds.append(
                {"title": "🔔 追蹤主題更新", "description": text[:4096], "color": 0xED4245}
            )

    # --- Featured articles ---
    if content.featured_articles:
        lines = []
        for section in content.featured_articles:
            lines.append(_render_section_embed(section))
        text = "\n".join(lines).strip()
        if text:
            embeds.append({"title": "⭐ 精選文章", "description": text[:4096], "color": 0xF1C40F})

    # --- News clusters ---
    if content.news_clusters:
        lines = []
        for section in content.news_clusters:
            lines.append(_render_section_embed(section))
        text = "\n".join(lines).strip()
        if text:
            embeds.append({"title": "📰 新聞聚合", "description": text[:4096], "color": 0xFEE75C})

    # --- Thematic summaries ---
    if content.thematic_summaries:
        lines = []
        for section in content.thematic_summaries:
            lines.append(_render_section_embed(section))
        text = "\n".join(lines).strip()
        if text:
            embeds.append({"title": "📋 主題摘要", "description": text[:4096], "color": 0x5865F2})

    # --- Attention sections ---
    if content.attention_sections:
        lines = []
        for section in content.attention_sections:
            lines.append(_render_section_embed(section))
        text = "\n".join(lines).strip()
        if text:
            embeds.append({"title": "👀 值得關注", "description": text[:4096], "color": 0x9B59B6})

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
                    "title": f"📌 其他標題（{len(content.filtered_headlines)}篇）",
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
    stats_lines = [
        f"📊 來源 **{content.sources_processed}** 個",
        f"📥 文章 **{content.articles_received}** 篇",
        f"✅ 保留 **{content.articles_included}** 篇（{pct}）",
    ]
    if content.usage.api_calls > 0:
        stats_lines.append(f"💰 費用 **${content.usage.estimated_cost:.4f}**")

    embeds.append(
        {
            "title": f"{label} {content.date}",
            "description": "\n".join(stats_lines),
            "color": 0x57F287,
        }
    )

    return embeds


async def send_discord(
    webhook_url: str,
    content: DigestContent,
) -> None:
    """Send digest content to Discord via webhook.

    Does nothing if webhook_url is empty. Failures are logged but not raised.
    """
    if not webhook_url:
        return

    embeds = build_discord_embeds(content)
    payload = {"embeds": embeds}

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(webhook_url, json=payload)
            resp.raise_for_status()
            logger.debug("Discord webhook sent: %d embeds", len(embeds))
    except httpx.HTTPError:
        logger.warning("Discord webhook failed", exc_info=True)
