"""FetchSource that polls RSS/Atom feeds directly, with no buffer in between.

The fallback wired in when the RSS Worker isn't configured. A digest-time poll
sees only what a feed's current snapshot holds, and high-volume feeds hold 2-4h
against a 24h window — measured, 141 of 317 articles went missing. Accumulation
is the whole point of workers/rss/ (hourly poll into D1); this adapter has none,
so it is correct only for feeds whose snapshot outlives the window.
"""

from __future__ import annotations

import asyncio
import html
import io
import logging
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

import feedparser

from cyris.adapters.fetch.email_parser import strip_tracking_params
from cyris.adapters.http_client import HttpClient
from cyris.domain.models import Article, Lane, SourceConfig

logger = logging.getLogger(__name__)

# ponytail: fixed fan-out cap; make it configurable only if a run actually stalls
MAX_CONCURRENT_FEEDS = 10


def _entry_published(entry: Mapping[str, object]) -> datetime | None:
    """Return a tz-aware publish time, or None when the entry has no usable date.

    feedparser normalises `*_parsed` to UTC but hands back a naive struct_time;
    comparing that against the tz-aware digest window raises TypeError.
    """
    for key in ("published_parsed", "updated_parsed"):
        # Membership first, not truthiness. An entry whose date failed to parse
        # carries `published_parsed = None`, and reading `updated_parsed` off it
        # hits feedparser's issue-310 fallback, which synthesises the value from
        # `published_parsed` and warns every time it does.
        if key not in entry:
            continue
        parsed = entry[key]
        if parsed:
            return datetime(*parsed[:6], tzinfo=UTC)
    return None


class RssSource:
    """FetchSource that fetches and parses feed URLs from sources.yaml."""

    def __init__(self, http_client: HttpClient | None = None) -> None:
        self._http = http_client or HttpClient()

    async def fetch_articles(
        self,
        after: datetime,
        before: datetime,
        sources: dict[str, SourceConfig],
    ) -> list[Article]:
        """Fetch every configured feed concurrently and window-filter the entries."""
        feeds = [s for s in sources.values() if s.url and s.type == "rss"]
        if not feeds:
            return []

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_FEEDS)

        async def one(source: SourceConfig) -> list[Article]:
            async with semaphore:
                return await self._fetch_feed(source, after, before)

        results = await asyncio.gather(*(one(s) for s in feeds), return_exceptions=True)

        articles: list[Article] = []
        for source, result in zip(feeds, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("Feed failed: %s (%s): %s", source.name, source.url, result)
                continue
            articles.extend(result)

        logger.info("Fetched %d entries from %d feeds", len(articles), len(feeds))
        return articles

    async def _fetch_feed(
        self,
        source: SourceConfig,
        after: datetime,
        before: datetime,
    ) -> list[Article]:
        response = await self._http.get(source.url or "")
        response.raise_for_status()
        # feedparser.parse(url) would do its own blocking urllib fetch — hand it the body
        # so the shared client's User-Agent and redirect handling apply. As a stream:
        # bare bytes that name a path are opened as that local file.
        parsed = await asyncio.to_thread(feedparser.parse, io.BytesIO(response.content))

        articles = []
        for entry in parsed.entries:
            published_at = _entry_published(entry)
            if published_at is None:
                logger.debug("Entry without date skipped: %s", getattr(entry, "link", ""))
                continue
            if not (after <= published_at < before):
                continue

            # Without stripping, the same article arrives under two URLs and
            # defeats the store's URL primary key.
            link = getattr(entry, "link", "")
            url = strip_tracking_params(link)
            if not url:
                continue

            content = _entry_content(entry)

            articles.append(
                Article(
                    id=getattr(entry, "id", "") or url,
                    # A CDATA title keeps its entities as text, and the page escapes it again.
                    title=html.unescape(getattr(entry, "title", "")),
                    url=url,
                    content=content,
                    author=getattr(entry, "author", None) or None,
                    published_at=published_at,
                    source_name=source.name,
                    source_lane=source.lane or Lane.SKIM,
                    source_tags=source.tags,
                    image_url=_entry_image(entry, link),
                )
            )
        return articles

    async def health_check(self) -> bool:
        return True


_IMG_SRC = re.compile(r"""<img\b[^>]*?\ssrc\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def _image_address(raw: str | None, base: str) -> str | None:
    """An http(s) URL resolved against the entry's link, else None, as the Worker's is."""
    if not raw:
        return None
    try:
        url = urljoin(base, raw.replace("&amp;", "&"))
        scheme = urlsplit(url).scheme
    except ValueError:  # a malformed authority, such as `http://[x/`; the picture is optional
        return None
    return url if scheme in ("http", "https") else None


def _is_image(node: Mapping[str, str]) -> bool:
    return node.get("medium") == "image" or node.get("type", "").startswith("image/")


def _entry_image(entry: Mapping[str, Any], link: str) -> str | None:
    """The picture the feed names for its entry, in `workers/rss/src/parse.js`'s order.

    The two parsers must give one entry the same URL. feedparser lists every
    `media:thumbnail` and `media:content` as `media_thumbnail` and `media_content`
    dicts, those inside `media:group` included, and every enclosure under
    `enclosures` with an `href`. A media:content or enclosure counts only when it
    says it is an image.
    """
    candidates = [
        *(t.get("url") for t in entry.get("media_thumbnail", [])),
        *(m.get("url") for m in entry.get("media_content", []) if _is_image(m)),
        *(e.get("href") for e in entry.get("enclosures", []) if _is_image(e)),
    ]
    for raw in candidates:
        if found := _image_address(raw, link):
            return found
    img = _IMG_SRC.search(_entry_content(entry))
    return _image_address(img.group(1), link) if img else None


def _entry_content(entry: object) -> str:
    """Prefer full content over the summary."""
    content = getattr(entry, "content", None)
    if content:
        return content[0].get("value", "")
    return getattr(entry, "summary", "")
