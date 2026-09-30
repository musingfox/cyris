"""Newsletter article fetching.

Now one email == one article; content is the email body (text preferred, else stripped html).
No network, no per-link expansion, fetch is sync.
"""

import hashlib
import html
import logging
import re
from contextlib import suppress
from email.utils import parseaddr
from urllib.parse import urlparse, urlsplit

from cyris.adapters.fetch.email_parser import (
    NEWSLETTER_TRACKING_PARAMS,
    ParsedNewsletter,
    _HrefParser,
    is_content_url,
    strip_tracking_params,
    unwrap_tracking_redirect,
)
from cyris.adapters.fetch.keywords import canonical_host, is_view_url_host, view_in_browser_re
from cyris.domain.models import Article, SourceConfig

logger = logging.getLogger(__name__)

_MIN_CONTENT_PATH_DEPTH = 2


def _generate_article_id(source_name: str, key: str) -> str:
    """Generate deterministic article ID from source name and (subject or url)."""
    return hashlib.sha256(f"{source_name}{key}".encode()).hexdigest()


def _find_newsletter_view_url(html: str) -> str | None:
    """Find the ESP-hosted public view link; hosts are `view_url_hosts` in keywords.json.

    Critical: parse .hostname of the href itself, never 'foo in href' substring.
    This prevents using a track/click wrapper (which has encoded target mailchi url)
    as the canonical url, which would leak the e= tracking param into published digest.
    """
    if not html:
        return None
    # HTMLParser, not a regex over raw html: it decodes &amp; in the attribute, so
    # a recipient parameter written as "&amp;e=" is seen as "e" and stripped.
    parser = _HrefParser()
    with suppress(Exception):
        parser.feed(html)
    for href in parser.hrefs:
        try:
            if is_view_url_host((urlsplit(href).hostname or "").lower()):
                return strip_tracking_params(href, extra_params=NEWSLETTER_TRACKING_PARAMS)
        except Exception:
            continue
    return None


_URL_RE = re.compile(r"https?://[^\s()<>\"']+")


def _normalize_candidate(url: str) -> str:
    # No html.unescape here: HTMLParser already unescaped the attribute, and text
    # URLs were never escaped. Running it anyway expands Python's semicolon-less
    # legacy entities — &section=, &times=, &copy= — and mangles the query string.
    unwrapped = unwrap_tracking_redirect(url)
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


def sender_domain(from_email: str) -> str:
    """The domain of a From header's address, lowercased; empty when it has none."""
    _, at, domain = parseaddr(from_email or "")[1].rpartition("@")
    return domain.strip(".").lower() if at else ""


def _is_sender_host(host: str, sender_host: str, from_domain: str) -> bool:
    # A configured homepage names the host exactly. Without one, the From domain is
    # the next known fact: the sender owns that domain and its subdomains.
    if sender_host:
        return host == sender_host
    return bool(from_domain) and (host == from_domain or host.endswith("." + from_domain))


def _distinct_pages(urls: list[str]) -> list[str]:
    """Collapse links that name one page into one, keeping first-seen order.

    A page and a query-string variant of it are one page (a share or tracking
    suffix the strip lists do not cover yet), and so is one path on two hosts that
    `host_aliases` in keywords.json names as one site. Different hosts are otherwise
    different pages, even under one parent domain, and query variants with no bare
    form stay apart: on one path, different queries can be different pages.
    """
    by_page: dict[tuple[str, str], list[str]] = {}
    for url in urls:
        parsed = urlparse(url)
        key = (canonical_host((parsed.hostname or "").lower()), parsed.path.rstrip("/"))
        by_page.setdefault(key, []).append(url)
    pages: list[str] = []
    for variants in by_page.values():
        bare = [url for url in variants if not urlparse(url).query]
        pages.extend(bare[:1] or variants)
    return pages


def select_primary_content_url(
    candidates: list[str], sender_host: str | None = None, from_domain: str | None = None
) -> str | None:
    """Return a canonical sender URL only when candidates identify one unambiguously.

    Path depth and frequency describe link shape, not canonical confidence. A URL
    is canonical only when the message links a single content page on the sender's
    own host: the configured homepage host, else the From address's domain. The
    sender is known, never voted in by link counts. Anything else returns None and
    callers use the explicit view-link or synthetic-URL fallback.
    """
    sender_host = (sender_host or "").lower()
    from_domain = (from_domain or "").lower()
    if not sender_host and not from_domain:
        return None
    sender_urls = list(
        dict.fromkeys(
            url
            for url in candidates
            if is_content_url(url)
            and _is_sender_host((urlparse(url).hostname or "").lower(), sender_host, from_domain)
            and _path_depth(url) >= _MIN_CONTENT_PATH_DEPTH
        )
    )
    pages = _distinct_pages(sender_urls)
    return pages[0] if len(pages) == 1 else None


def _find_view_url_in_text(text: str) -> str | None:
    """Find the web-version link in a plain-text email body.

    Text/plain newsletters render anchors as "label (url)", so the canonical post URL
    sits on the same line as its "read on the web" label. Take the URL that *follows*
    the label, never the line's first one: footers collapse nav links onto a single
    subscribe-then-web-version line, and a join URL is identical every issue,
    so adopting it would make the store dedup every later issue away as a duplicate.
    """
    for line in (text or "").splitlines():
        marker = view_in_browser_re().search(line)
        if not marker:
            continue
        url = _URL_RE.search(line, marker.end())
        if url:
            # trailing sentence punctuation is not part of the URL ("…/posts/1。")
            return _normalize_candidate(url.group().rstrip(".,;:。，、"))
    return None


def newsletter_article(parsed: ParsedNewsletter, source: SourceConfig) -> Article | None:
    """Return the email body as the Article for this newsletter issue.

    0 or 1 article. Content from text_content or unescaped html.
    Prefer the one sender-owned post URL; else the labelled web-version link in the
    text body; else the hostname-matched ESP archive; else synthetic newsletter:ID url.
    Empty body -> None + WARNING (singular per D5).
    """
    article_id = _generate_article_id(source.name, parsed.subject)
    sender_host = ""
    if source.homepage:
        with suppress(ValueError):
            sender_host = urlparse(source.homepage).hostname or ""
    view_url = (
        select_primary_content_url(
            harvest_url_candidates(parsed.html_content, parsed.text_content),
            sender_host,
            sender_domain(parsed.from_email),
        )
        or _find_view_url_in_text(parsed.text_content)
        or _find_newsletter_view_url(parsed.html_content)
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
    ref_urls: list[str] = []
    if view_url is None and source.homepage:
        # A configured publisher homepage is the only reader link allowed without
        # a confident per-issue canonical URL. Arbitrary email-body links belong to
        # the newsletter content, not to the one Article this email represents.
        homepage = strip_tracking_params(source.homepage.strip())
        if urlparse(homepage).scheme in {"http", "https"}:
            ref_urls = [homepage]

    return Article(
        id=article_id,
        title=parsed.subject,
        url=view_url or f"newsletter:{article_id}",
        content=content,
        published_at=parsed.date,
        source_name=source.name,
        source_tier=source.tier,
        source_tags=source.tags,
        ref_urls=ref_urls,
        source_type=source.type,
    )
