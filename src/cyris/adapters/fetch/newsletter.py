"""Newsletter article fetching.

Now one email == one article; content is the email body (text preferred, else stripped html).
No network, no per-link expansion, fetch is sync.
"""

import hashlib
import html
import logging
import re
from collections import Counter
from contextlib import suppress
from urllib.parse import urlparse, urlsplit

from cyris.adapters.fetch.email_parser import (
    NEWSLETTER_TRACKING_PARAMS,
    ParsedNewsletter,
    _HrefParser,
    extract_ref_urls,
    is_content_url,
    strip_tracking_params,
    unwrap_tracking_redirect,
)
from cyris.domain.models import Article, SourceConfig

logger = logging.getLogger(__name__)

_MIN_CONTENT_PATH_DEPTH = 2


def _generate_article_id(source_name: str, key: str) -> str:
    """Generate deterministic article ID from source name and (subject or url)."""
    return hashlib.sha256(f"{source_name}{key}".encode()).hexdigest()


def _find_newsletter_view_url(html: str) -> str | None:
    """Find public view link by hostname only (mailchi.mp or campaign-archive).

    Critical: parse .hostname of the href itself, never 'foo in href' substring.
    This prevents using a track/click wrapper (which has encoded target mailchi url)
    as the canonical url, which would leak the e= tracking param into published digest.
    """
    if not html:
        return None
    # extract hrefs (stdlib re sufficient for controlled newsletter html)
    hrefs = re.findall(r'<a[^>]+href=["\']([^"\']+)["\']', html, re.I)
    for href in hrefs:
        try:
            host = (urlsplit(href).hostname or "").lower()
            if re.fullmatch(r"(.+\.)?mailchi\.mp", host) or re.fullmatch(
                r"(.+\.)?campaign-archive\d*\.com", host
            ):
                return strip_tracking_params(href)
        except Exception:
            continue
    return None


_VIEW_MARKERS = ("網頁版", "線上閱讀", "view in browser", "view this email", "view online")
_VIEW_MARKER_RE = re.compile("|".join(re.escape(m) for m in _VIEW_MARKERS), re.I)
_URL_RE = re.compile(r"https?://[^\s()<>\"']+")


def _normalize_candidate(url: str) -> str:
    unwrapped = unwrap_tracking_redirect(html.unescape(url))
    return strip_tracking_params(unwrapped, extra_params=NEWSLETTER_TRACKING_PARAMS)


def harvest_url_candidates(html_content: str = "", text_content: str = "") -> list[str]:
    """Collect normalized URL candidates from HTML anchors and plain text (duplicates kept)."""
    candidates: list[str] = []
    if html_content:
        parser = _HrefParser()
        with suppress(Exception):
            parser.feed(html_content)
        candidates.extend(_normalize_candidate(href) for href in parser.hrefs if href)
    if text_content:
        for match in _URL_RE.finditer(text_content):
            raw = match.group().rstrip(".,;:。，、")
            if raw:
                candidates.append(_normalize_candidate(raw))
    return candidates


def _path_depth(url: str) -> int:
    try:
        path = urlparse(url).path
    except ValueError:
        return 0
    return len([part for part in path.split("/") if part])


def select_primary_content_url(candidates: list[str]) -> str | None:
    """Pick the sender-owned, deepest, most frequent content URL, or None."""
    content = [url for url in candidates if is_content_url(url)]
    if not content:
        return None

    host_order: list[str] = []
    host_count: Counter[str] = Counter()
    for url in content:
        host = (urlparse(url).hostname or "").lower()
        host_count[host] += 1
        if host not in host_order:
            host_order.append(host)
    dominant = max(host_order, key=lambda h: (host_count[h], -host_order.index(h)))

    eligible = [
        url
        for url in content
        if (urlparse(url).hostname or "").lower() == dominant
        and _path_depth(url) >= _MIN_CONTENT_PATH_DEPTH
    ]
    if not eligible:
        return None

    freq: Counter[str] = Counter()
    first: dict[str, int] = {}
    for i, url in enumerate(eligible):
        freq[url] += 1
        first.setdefault(url, i)
    return max(first, key=lambda u: (_path_depth(u), freq[u], -first[u]))


def _find_view_url_in_text(text: str) -> str | None:
    """Find the web-version link in a plain-text email body.

    Text/plain newsletters render anchors as "label (url)", so the canonical post URL
    sits on the same line as its "read on the web" label. Take the URL that *follows*
    the label, never the line's first one: footers collapse nav links onto a single
    "訂閱 (…/join) | 網頁版 (…/posts/1)" line, and a join URL is identical every issue,
    so adopting it would make the store dedup every later issue away as a duplicate.
    """
    for line in (text or "").splitlines():
        marker = _VIEW_MARKER_RE.search(line)
        if not marker:
            continue
        url = _URL_RE.search(line, marker.end())
        if url:
            # trailing sentence punctuation is not part of the URL ("…/posts/1。")
            return strip_tracking_params(unwrap_tracking_redirect(url.group().rstrip(".,;:。，、")))
    return None


def newsletter_article(parsed: ParsedNewsletter, source: SourceConfig) -> Article | None:
    """Return the email body as the Article for this newsletter issue.

    0 or 1 article. Content from text_content or unescaped html.
    If html has clean public view link (by hostname), use it (stripped); else the
    labelled web-version link in the text body; else synthetic newsletter:ID url.
    Empty body -> None + WARNING (singular per D5).
    """
    article_id = _generate_article_id(source.name, parsed.subject)
    view_url = _find_newsletter_view_url(parsed.html_content) or _find_view_url_in_text(
        parsed.text_content
    )
    raw = parsed.text_content.strip() or " ".join(
        html.unescape(re.sub(r"<[^>]+>", " ", parsed.html_content)).split()
    )
    content = raw.strip()
    if not content:
        logger.warning(
            "Empty newsletter body for subject=%s from source=%s; skipping ingest",
            parsed.subject,
            source.name,
        )
        return None
    return Article(
        id=article_id,
        title=parsed.subject,
        url=view_url or f"newsletter:{article_id}",
        content=content,
        published_at=parsed.date,
        source_name=source.name,
        source_tier=source.tier,
        source_tags=source.tags,
        ref_urls=extract_ref_urls(parsed.html_content),
    )
