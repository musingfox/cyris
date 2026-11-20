"""Notification sender: Discord webhook."""

import logging
import re
from collections.abc import Callable

import httpx

from cyris.adapters.output.html_digest import Story, citation_links, issue_language
from cyris.adapters.run_alert import alert_message
from cyris.domain.area_layout import AreaSection
from cyris.domain.layers import Pebble, Sand, layer_by_lane
from cyris.domain.models import (
    NO_LLM_MODEL,
    DigestContent,
    DigestItem,
    DigestSection,
    NewsParagraph,
)
from cyris.i18n import issue_name, translator
from cyris.service_layer.alerts import RunAlert

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


# The embed of the area section that has no area: a digest whose area order is empty.
UNNAMED_SECTION_TITLE = "Digest"


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


def _render_fan_section(section: DigestSection) -> str:
    """A followed group as Discord markdown: its heading, then one line per article."""
    lines = [f"### {section.heading}"]
    for item in section.items:
        title_part = f"**[{item.title}]({item.urls[0]})**" if item.urls else f"**{item.title}**"
        summary_str = f" — {item.summary}" if item.summary else ""
        lines.append(f"- {title_part}{summary_str}")
    lines.append("")
    return "\n".join(lines)


def _render_row(item: DigestItem) -> list[str]:
    """A row the page draws as a headline: its title with sources, then its summary cut short."""
    source_str = f" ({', '.join(item.sources)})" if item.sources else ""
    title_part = f"**[{item.title}]({item.urls[0]})**" if item.urls else f"**{item.title}**"
    lines = [f"- {title_part}{source_str}"]
    if item.summary:
        summary = item.summary if len(item.summary) <= 80 else item.summary[:80] + "…"
        lines.append(f"  {summary}")
    return lines


def _render_paragraph(paragraph: NewsParagraph) -> str:
    """A news paragraph as the page draws it: its text, then one link per cited article."""
    links = " · ".join(
        _linked(label, link) for unit in paragraph.units for label, link in citation_links(unit)
    )
    return f"{paragraph.text}\n{links}\n" if links else f"{paragraph.text}\n"


def _render_area_section(section: AreaSection) -> str:
    """One area's cards, Following groups, clusters, rows and paragraphs, in page order."""
    cards = [
        _render_story_embed(Story(item.title, item.summary, (item,)))
        for feature in section.features
        for item in feature.items
    ]
    rows = [line for row in section.rows for line in _render_row(row.item)]
    parts = [
        *cards,
        *(_render_fan_section(group) for group in section.following),
        *(_render_section_embed(cluster) for cluster in section.clusters),
        "\n".join(rows),
        *(_render_paragraph(paragraph) for paragraph in section.paragraphs),
    ]
    return "\n".join(part for part in parts if part).strip()


def _render_tracking(topic: DigestSection) -> str:
    """A tracked topic as the page draws it: its name, then each hit's title link alone."""
    hits = [f"- {_linked(item.title, item.link)}" for item in topic.items]
    return "\n".join([f"### {topic.heading}", *hits, ""])


def _render_pebble(pebble: Pebble) -> str:
    """A pebble's card: its title link, then its summary; no source, to keep the message short."""
    item = pebble.item
    lines = [f"### {_linked(item.title, item.link)}"]
    if item.summary:
        lines.append(item.summary)
    lines.append("")
    return "\n".join(lines)


def _render_sand(sand: Sand) -> str:
    """A topic block: its heading and its line, whole; no article link and no source."""
    lines = [f"### {sand.heading}"]
    if sand.line:
        lines.append(sand.line)
    lines.append("")
    return "\n".join(lines)


def _continued(parts: list[str]) -> list[str]:
    """A layer's items as embed descriptions, each at most one embed long where it can be.

    Items are never split here: one item over the limit is a description of its own, and
    `_fit_embeds` leaves it out whole.
    """
    pages: list[str] = []
    page: list[str] = []
    for part in parts:
        if page and _size("\n".join([*page, part]).strip()) > _DISCORD_DESCRIPTION_MAX:
            pages.append("\n".join(page).strip())
            page = []
        page.append(part)
    pages.append("\n".join(page).strip())
    return [text for text in pages if text]


def build_discord_embeds(
    content: DigestContent,
    digest_url: str = "",
    publish_failed: bool = False,
    *,
    language: str = "",
) -> list[dict]:
    """Build Discord embed objects from DigestContent, worded in the issue's language.

    An issue stored with no language is worded in `language`, the current setting.

    Returns a list of embed dicts, uncapped in total: `build_discord_payload` holds them
    to Discord's limits.
    The embeds follow the page (docs/design/ui-language.md §6 *digest*): Tracking, then
    Stories and Topics of `layer_by_lane`, an empty one left out. The Glance layer is
    left to the page, to keep the message short. The stats come last. A layer longer than
    one embed's description continues in embeds of the same title, so that `_fit_embeds`,
    which cuts from the end, takes the Topics first and the Stories last when the message
    runs out of room.
    """
    t = translator(issue_language(content, language))
    layers = layer_by_lane(content)
    blocks = [
        (t("layer.tracking"), [_render_tracking(topic) for topic in layers.tracking], 0xE67E22),
        (t("layer.stories"), [_render_pebble(pebble) for pebble in layers.pebbles], 0xF1C40F),
        (t("layer.topics"), [_render_sand(sand) for sand in layers.sand], 0x5865F2),
    ]
    embeds: list[dict] = []
    for title, parts, color in blocks:
        for text in _continued(parts):
            embeds.append({"title": title, "description": text, "color": color})

    # --- Stats ---
    pct = (
        f"{content.articles_included / content.articles_received * 100:.1f}%"
        if content.articles_received > 0
        else t("notify.no_share")
    )
    stats_lines = []
    if digest_url:
        stats_lines.append(f"📖 [{t('email.read_online')}]({digest_url})")
    elif publish_failed:
        stats_lines.append(f"⚠️ {t('notify.publish_failed')}")
    stats_lines += [
        f"📊 {t('notify.stat_sources')} **{content.sources_processed}**",
        f"📥 {t('notify.stat_articles')} **{content.articles_received}**",
        f"✅ {t('notify.stat_kept')} **{content.articles_included}** ({pct})",
    ]
    synthetic = content.synthetic_url_count
    dead = content.dead_link_count
    if (synthetic or 0) or (dead or 0):
        # The two counts have different scopes on purpose — synthetic covers every
        # article fetched this run, dead covers what actually reached the digest.
        # Saying so keeps them from reading as a contradiction next to the kept count.
        stats_lines.append(f"⚠️ {t('notify.link_health', synthetic=synthetic or 0, dead=dead or 0)}")
    if content.usage.api_calls > 0 and content.usage.estimated_cost is not None:
        stats_lines.append(f"💰 {t('notify.stat_cost')} **${content.usage.estimated_cost:.4f}**")

    embeds.append(
        {
            "title": issue_name(t, content.date, content.started_at),
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


def _size(text: str) -> int:
    """How much of a limit `text` takes: its UTF-8 bytes, never fewer than its characters.

    Counted in characters, the 2026-10-30 evening message measured 5929 of 6000 and was
    refused with 413 "Oversized fields: message.embeds=10833": the issue was mostly
    Chinese, three bytes a character. Bytes hold whichever unit Discord counts in.
    """
    return len(text.encode("utf-8"))


def _cut_title(title: str) -> str:
    """`title` cut to `_DISCORD_TITLE_MAX` bytes, never inside a character."""
    return title.encode("utf-8")[:_DISCORD_TITLE_MAX].decode("utf-8", errors="ignore")


# Where one item ends and the next begins in a layer's description: a blank line before the
# next `### ` heading. A summary's own blank lines are not a boundary.
_ITEM_BREAK = re.compile(r"\n\n(?=### )")


def _cut_at_items(text: str, room: int, more: str) -> str:
    """`text` if it fits in `room`, else its leading whole items and the marker `more`.

    An item is never cut: a summary is shown whole or its item is left out. When not even
    the first item fits, the marker stands alone, or `""` if it does not fit either.
    """
    if _size(text) <= room:
        return text
    kept: list[str] = []
    used = _size(more) + 1
    for item in _ITEM_BREAK.split(text):
        used += _size(item) + 2
        if used > room:
            break
        kept.append(item.rstrip())
    if kept:
        return "\n".join(["\n\n".join(kept), more])
    return more if _size(more) <= room else ""


def _fit_embeds(embeds: list[dict], more: str) -> list[dict]:
    """Hold a digest's embeds to Discord's limits; the stats embed, last, stays whole.

    Sections keep their order and are cut from the end, at item boundaries so no
    summary or markdown link is split: the section the total runs out in keeps the
    items that fit whole, and every section after it is dropped. A cut section ends in
    the marker `more`.
    """
    *sections, stats = embeds
    budget = _DISCORD_EMBED_TOTAL - _size(stats["title"]) - _size(stats["description"])
    fitted = []
    for embed in sections[: _DISCORD_MAX_EMBEDS - 1]:
        title = _cut_title(embed["title"])
        capped = _cut_at_items(embed["description"], _DISCORD_DESCRIPTION_MAX, more)
        room = budget - _size(title)
        description = (
            capped if _size(capped) <= room else _cut_at_items(embed["description"], room, more)
        )
        if description:
            fitted.append(embed | {"title": title, "description": description})
            budget -= _size(title) + _size(description)
        if description != embed["description"]:
            break
    return [*fitted, stats]


def build_discord_payload(
    content: DigestContent,
    digest_url: str = "",
    publish_failed: bool = False,
    degraded: bool = False,
    *,
    language: str = "",
) -> dict:
    """Build the Discord webhook payload for a digest.

    `degraded` is `is_degraded_run`'s verdict, passed in: only the run knows the
    configured provider, and a missing key leaves no trace of it in the usage.
    `language` is the current setting, which words an issue stored with none.
    """
    t = translator(issue_language(content, language))
    embeds = build_discord_embeds(content, digest_url, publish_failed, language=language)
    payload = {"embeds": _fit_embeds(embeds, f"*{t('notify.more_online')}*")}
    if degraded and content.usage.model != NO_LLM_MODEL:
        payload["content"] = f"⚠️ {t('notify.degraded_model', model=content.usage.model)}"[
            :_DISCORD_CONTENT_MAX
        ]
    elif degraded:
        payload["content"] = f"⚠️ {t('notify.degraded')}"
    return payload


async def send_discord(
    webhook_url: str,
    content: DigestContent,
    digest_url: str = "",
    publish_failed: bool = False,
    degraded: bool = False,
    *,
    language: str = "",
) -> dict[str, str]:
    """Send digest content to Discord via webhook; return the outcome for the run summary.

    The outcome is `{"status": "sent"}`, `{"status": "skipped"}` without a webhook, or
    `{"status": "failed", "reason": ...}`. Failures are logged and returned, not raised:
    a lost message must not cost the run its record.
    When digest_url is set, a link to the online (Cloudflare Pages) digest is
    included in the stats embed; when publishing was attempted and failed, the
    missing link is called out instead of silently omitted. `language` is the current
    `digest.output_language`, which words an issue stored with none.
    """
    if not webhook_url:
        return {"status": "skipped"}

    payload = build_discord_payload(
        content, digest_url, publish_failed, degraded, language=language
    )

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(webhook_url, json=payload)
            resp.raise_for_status()
            logger.debug("Discord webhook sent: %d embeds", len(payload["embeds"]))
    except httpx.HTTPStatusError as e:
        reason = f"HTTP {e.response.status_code}: {_discord_message(e.response)}"
        logger.warning("Discord webhook failed: %s", reason, exc_info=True)
        return {"status": "failed", "reason": reason}
    except httpx.HTTPError as e:
        reason = redact_webhook_tokens(f"{type(e).__name__}: {e}")
        logger.warning("Discord webhook failed: %s", reason, exc_info=True)
        return {"status": "failed", "reason": reason}
    return {"status": "sent"}


def _discord_message(resp: httpx.Response) -> str:
    """Discord's own words for a refusal: the body's `message`, else the body itself.

    A proxy can answer with any body, so a non-object one falls back to the text, and the
    reason is capped and redacted: it is stored in the run summary.
    """
    try:
        body = resp.json()
    except ValueError:
        body = None
    message = body.get("message") if isinstance(body, dict) else None
    return redact_webhook_tokens(str(message or resp.text)[:500])


async def send_discord_alert(
    webhook_url: str,
    alert: RunAlert,
    *,
    wording: Callable[[RunAlert], tuple[str, str]] | None = None,
) -> None:
    """Post a plain failure alert, worded in its language. Does nothing without a webhook.

    `wording` is a hosted tenant's `reader_alert_message`, which withholds the error.

    A failed post is logged, not raised: the alert must not mask the run exception
    that triggered it. Wording that fails raises to `send_alert`, whose per-channel
    guard logs it. There is no digest behind this message.
    """
    if not webhook_url:
        return

    subject, text = (wording or alert_message)(alert)
    subject = redact_webhook_tokens(subject)
    text = redact_webhook_tokens(text)
    content = f"{subject}\n{text}"[:_DISCORD_CONTENT_MAX]
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(webhook_url, json={"content": content})
            resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("Discord alert failed: %s", redact_webhook_tokens(str(e)))
