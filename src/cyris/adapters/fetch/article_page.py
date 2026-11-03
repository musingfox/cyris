"""A teaser feed article's own page, read for its body during one run.

Some feeds carry one sentence per entry, and a summary of that sentence is all the
model can write. An RSS article whose text is shorter than the summarize window
has its page fetched and its body extracted through `html_text`, and the longer
of the two texts is kept. The text lives in the returned articles only: nothing
here writes it to a store, so the next run reads the feed's text again.
"""

from __future__ import annotations

import asyncio
import codecs
import ipaddress
import logging
import socket
from collections import Counter
from urllib.parse import urlsplit

import httpx

from cyris.adapters.fetch.html_text import page_text
from cyris.adapters.fetch.keywords import is_media_link
from cyris.domain.models import Article, SourceConfig

logger = logging.getLogger(__name__)

# An issue holds about a hundred articles and a third of them are teasers
# (measured 2026-11-03), so eight at once reads them in about five waves. Many
# teasers share a publisher, and eight keeps any one site to a reader's handful.
PAGE_CONCURRENCY = 8
# One page, its redirects and its body included. With the concurrency above, a
# run's added time is at most ceil(teasers / PAGE_CONCURRENCY) * this: about 50s
# for 33 teasers, against a run container capped at 900s whose slowest run took
# 196s (`RUN_INACTIVITY_TIMEOUT_MS` in workers/app/src/index.js).
PAGE_TIMEOUT_SECONDS = 10
# The most one page may hold, counted as it arrives: an article page is a few
# hundred kilobytes, and a link to a large file is not an article.
MAX_PAGE_BYTES = 5 * 1024 * 1024
# Names the fetcher and where to read about it, as the RSS Worker's does; a site
# that blocks it is answered by the feed text, which the run keeps.
USER_AGENT = "cyris (+https://github.com/musingfox/cyris)"

_HTML_TYPES = ("text/html", "application/xhtml+xml")


class _PageRefusedError(Exception):
    """A page this module does not read: a private address, the wrong type, too large."""


def _private_literal(host: str) -> bool:
    """Whether `host` names this machine or a non-public address without a lookup.

    That is an IP address written out and not global, any IPv4 spelling a resolver
    reads that `ipaddress` does not (`127.1`, `2130706433`, `0x7f000001`, octal),
    and localhost or a name under it (RFC 6761 reserves them for loopback).
    """
    # WHY no DNS: a name that resolves to a private address is not refused here; the owner
    # accepted that risk (architecture.md §3.2), and through the proxy the proxy resolves it.
    name = host.strip("[]").lower()
    name = name.removesuffix(".")
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        return not ipaddress.ip_address(name).is_global
    except ValueError:
        pass
    try:
        socket.inet_aton(name)
    except (OSError, ValueError):
        return False
    # A shorthand IPv4 address: refused whatever it names, as no page is linked so.
    return True


def _web_page(url: str) -> bool:
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    return (
        parts.scheme in ("http", "https")
        and bool(host)
        and not _private_literal(host)
        # A video's page text is the site's navigation, not the video.
        and not is_media_link(host, parts.path)
    )


async def _refuse_private(request: httpx.Request) -> None:
    # Every hop of a redirect passes here, not only the first address.
    if _private_literal(request.url.host):
        raise _PageRefusedError(f"private address {request.url.host}")


async def _last_hop(client: httpx.AsyncClient, url: str) -> httpx.Response:
    """The streamed response at the end of `url`'s redirects, its body not yet read.

    Each redirect is closed unread. httpx's `follow_redirects` reads every hop's
    body whole, decompressed, before the caller sees the final response, so no cap
    here could count it.
    """
    request = client.build_request("GET", url)
    for _ in range(client.max_redirects + 1):
        response = await client.send(request, stream=True)
        if response.next_request is None:
            return response
        await response.aclose()
        request = response.next_request
    raise _PageRefusedError(f"more than {client.max_redirects} redirects")


async def _fetch(client: httpx.AsyncClient, url: str) -> str:
    """The body text of the page at `url`; raises when there is none to read."""
    response = await _last_hop(client, url)
    try:
        if not response.is_success:
            # No retry: a 4xx will not change, and a 5xx falls back to the feed text.
            raise _PageRefusedError(f"HTTP {response.status_code}")
        media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type not in _HTML_TYPES:
            raise _PageRefusedError(f"not a page ({media_type or 'no type'})")
        # Asked for identity; an answer encoded anyway could inflate past any cap.
        encoding = response.headers.get("content-encoding", "").strip().lower()
        if encoding not in ("", "identity"):
            raise _PageRefusedError(f"encoded ({encoding})")
        body = bytearray()
        async for chunk in response.aiter_raw():
            body += chunk
            if len(body) > MAX_PAGE_BYTES:
                raise _PageRefusedError(f"larger than {MAX_PAGE_BYTES} bytes")
        markup = _markup(bytes(body), response.charset_encoding)
    finally:
        await response.aclose()
    # Off the event loop: a page can be megabytes of markup.
    return await asyncio.to_thread(page_text, markup)


def _markup(body: bytes, charset: str | None) -> str | bytes:
    """A charset the server named is decoded here; without one, trafilatura reads
    the page's own `<meta charset>` from the bytes."""
    if not charset:
        return body
    try:
        codecs.lookup(charset)
    except LookupError:
        # A name no codec knows (`x-bogus`, `none`): utf-8 is what most pages are.
        charset = "utf-8"
    return body.decode(charset, errors="replace")


async def _lengthen_one(
    client: httpx.AsyncClient, gate: asyncio.Semaphore, article: Article, outcomes: Counter
) -> Article:
    async with gate:
        try:
            async with asyncio.timeout(PAGE_TIMEOUT_SECONDS):
                text = await _fetch(client, article.url)
        except TimeoutError:
            reason = f"no page within {PAGE_TIMEOUT_SECONDS}s"
        except (httpx.HTTPError, httpx.InvalidURL, ValueError, _PageRefusedError) as e:
            reason = str(e) or type(e).__name__
        except Exception as e:
            # Anything else, an extractor's own error included: the pages share one
            # gather, and one page must not cost the others their body.
            reason = f"unexpected {type(e).__name__}: {e}"
            logger.warning("Reading %s failed unexpectedly", article.url, exc_info=True)
        else:
            if len(text) > len(article.content):
                outcomes["lengthened"] += 1
                return article.model_copy(update={"content": text})
            reason = "page body no longer than the feed text"
    outcomes["kept"] += 1
    logger.info("Article page of %s (%s) not used: %s", article.url, article.source_name, reason)
    return article


async def lengthen_articles(
    articles: list[Article],
    *,
    summarize_snippet_length: int,
    sources: dict[str, SourceConfig],
) -> list[Article]:
    """`articles` in order, each RSS teaser on its page's body where that is longer.

    A teaser is an article of an RSS source whose text is shorter than
    `summarize_snippet_length`, the most of it a summary prompt reads. Anything
    else, or a page that fails, is returned as it came: one page never fails the run.
    """

    def teaser(article: Article) -> bool:
        source = sources.get(article.source_name)
        return (
            source is not None
            and source.type == "rss"
            and len(article.content) < summarize_snippet_length
            and _web_page(article.url)
        )

    short = [i for i, a in enumerate(articles) if teaser(a)]
    if not short:
        return list(articles)

    gate = asyncio.Semaphore(PAGE_CONCURRENCY)
    outcomes: Counter = Counter()
    async with httpx.AsyncClient(
        timeout=PAGE_TIMEOUT_SECONDS,
        # Uncompressed, so `MAX_PAGE_BYTES` counts what arrives, not what one chunk
        # inflates to.
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
        event_hooks={"request": [_refuse_private]},
    ) as client:
        lengthened = await asyncio.gather(
            *(_lengthen_one(client, gate, articles[i], outcomes) for i in short)
        )
    out = list(articles)
    for i, article in zip(short, lengthened, strict=True):
        out[i] = article
    logger.info(
        "Article pages: %d teaser(s), %d read from their page, %d kept their feed text",
        len(short),
        outcomes["lengthened"],
        outcomes["kept"],
    )
    return out
