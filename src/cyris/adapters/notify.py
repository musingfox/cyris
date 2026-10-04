"""Notification sender: Discord webhook."""

import logging
import re

import httpx

from cyris.adapters.output.html_digest import Story, _features
from cyris.domain.models import NO_LLM_MODEL, DigestContent, DigestSection

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


def period_label(period: str) -> str:
    """How a message names the period: `morning` reads as `Morning`."""
    return {"morning": "Morning", "evening": "Evening"}.get(period, period)


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


def _linked(title: str, link: str | None) -> str:
    return f"[{title}]({link})" if link else title


def _render_story_embed(story: Story) -> str:
    """A Top story or Features card as the page draws it: one summary, then its articles."""
    lines = [f"### {_linked(story.title, story.link)}"]
    if story.summary:
        lines.append(story.summary)
    if story.grouped:
        lines += [f"- {_linked(m.title, m.link)}" for m in story.members]
    if story.sources:
        lines.append(f"*{', '.join(story.sources)}*")
    lines.append("")
    return "\n".join(lines)


def build_discord_embeds(
    content: DigestContent, digest_url: str = "", publish_failed: bool = False
) -> list[dict]:
    """Build Discord embed objects from DigestContent.

    Returns a list of embed dicts, uncapped: `build_discord_payload` holds them to
    Discord's limits.
    Section embeds follow the page's section order (docs/design/ui-language.md §6):
    Top story, Features, In Focus, Following, On the Radar, The Wire. The stats come last.
    """
    label = period_label(content.period)
    embeds: list[dict] = []

    # --- Top story and Features: the stories the page and the mail draw ---
    stories = _features(content)
    for title, color, group in (
        ("⭐ Top story", 0xF1C40F, stories[:1]),
        ("📋 Features", 0x5865F2, stories[1:]),
    ):
        text = "\n".join(_render_story_embed(story) for story in group).strip()
        if text:
            embeds.append({"title": title, "description": text, "color": color})

    # --- News clusters ---
    if content.news_clusters:
        lines = []
        for section in content.news_clusters:
            lines.append(_render_section_embed(section))
        text = "\n".join(lines).strip()
        if text:
            embeds.append({"title": "📰 News", "description": text, "color": 0xFEE75C})

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
            embeds.append({"title": "📣 Following", "description": text, "color": 0xEB459E})

    # --- Attention sections ---
    if content.attention_sections:
        lines = []
        for section in content.attention_sections:
            lines.append(_render_section_embed(section))
        text = "\n".join(lines).strip()
        if text:
            embeds.append({"title": "👀 Worth a look", "description": text, "color": 0x9B59B6})

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
                    "description": text,
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


# Discord's limits on one webhook message (developers/resources/message, "Embed
# Limits"; developers/resources/webhook, "Execute Webhook"). Discord refuses a
# message over any of them whole.
_DISCORD_MAX_EMBEDS = 10
_DISCORD_TITLE_MAX = 256
_DISCORD_DESCRIPTION_MAX = 4096
_DISCORD_EMBED_TOTAL = 6000
_DISCORD_CONTENT_MAX = 2000
_MORE_ONLINE = "*… more in the online edition*"


def _cut_at_lines(text: str, room: int) -> str:
    """`text` if it fits in `room`, else its leading whole lines and a marker, else ""."""
    if len(text) <= room:
        return text
    kept: list[str] = []
    used = len(_MORE_ONLINE)
    for line in text.split("\n"):
        used += len(line) + 1
        if used > room:
            break
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join([*kept, _MORE_ONLINE]) if kept else ""


def _fit_embeds(embeds: list[dict]) -> list[dict]:
    """Hold a digest's embeds to Discord's limits; the stats embed, last, stays whole.

    Sections keep their order and are cut from the end, at line boundaries so no
    markdown link is split: the section the total runs out in keeps what fits,
    and every section after it is dropped.
    """
    *sections, stats = embeds
    budget = _DISCORD_EMBED_TOTAL - len(stats["title"]) - len(stats["description"])
    fitted = []
    for embed in sections[: _DISCORD_MAX_EMBEDS - 1]:
        title = embed["title"][:_DISCORD_TITLE_MAX]
        capped = _cut_at_lines(embed["description"], _DISCORD_DESCRIPTION_MAX)
        room = budget - len(title)
        description = capped if len(capped) <= room else _cut_at_lines(embed["description"], room)
        if description:
            fitted.append(embed | {"title": title, "description": description})
            budget -= len(title) + len(description)
        if description != capped:
            break
    return [*fitted, stats]


def build_discord_payload(
    content: DigestContent,
    digest_url: str = "",
    publish_failed: bool = False,
    degraded: bool = False,
) -> dict:
    """Build the Discord webhook payload for a digest.

    `degraded` is `is_degraded_run`'s verdict, passed in: only the run knows the
    configured provider, and a missing key leaves no trace of it in the usage.
    """
    payload = {"embeds": _fit_embeds(build_discord_embeds(content, digest_url, publish_failed))}
    if degraded and content.usage.model != NO_LLM_MODEL:
        payload["content"] = (
            f"⚠️ Degraded digest: LLM {content.usage.model} could not be used for every step "
            "this run, so some or all of it is unscored or plain excerpts."
        )[:_DISCORD_CONTENT_MAX]
    elif degraded:
        payload["content"] = (
            "⚠️ Degraded digest: the configured LLM could not be used this run, "
            "so some or all of it is unscored or plain excerpts."
        )
    return payload


async def send_discord(
    webhook_url: str,
    content: DigestContent,
    digest_url: str = "",
    publish_failed: bool = False,
    degraded: bool = False,
) -> None:
    """Send digest content to Discord via webhook.

    Does nothing if webhook_url is empty. Failures are logged but not raised.
    When digest_url is set, a link to the online (Cloudflare Pages) digest is
    included in the stats embed; when publishing was attempted and failed, the
    missing link is called out instead of silently omitted.
    """
    if not webhook_url:
        return

    payload = build_discord_payload(content, digest_url, publish_failed, degraded)

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(webhook_url, json=payload)
            resp.raise_for_status()
            logger.debug("Discord webhook sent: %d embeds", len(payload["embeds"]))
    except httpx.HTTPError:
        logger.warning("Discord webhook failed", exc_info=True)


async def send_discord_alert(webhook_url: str, subject: str, text: str) -> None:
    """Post a plain failure alert. Does nothing without a webhook.

    Failures are logged, not raised: the alert must not mask the run exception
    that triggered it. There is no digest behind this message.
    """
    if not webhook_url:
        return

    subject = redact_webhook_tokens(subject)
    text = redact_webhook_tokens(text)
    content = f"{subject}\n{text}"[:_DISCORD_CONTENT_MAX]
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(webhook_url, json={"content": content})
            resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("Discord alert failed: %s", redact_webhook_tokens(str(e)))
