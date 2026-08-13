"""Email newsletter parsing utilities."""

import logging
import re
from contextlib import suppress
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from pydantic import BaseModel

logger = logging.getLogger(__name__)


def strip_tracking_params(url: str) -> str:
    """Strip tracking query params (utm_*, e, fbclid etc) from newsletter URLs.

    Non-URL or unparsable input returned verbatim (no exception). Only ? kept if other
    params remain; trailing ? removed when empty.
    """
    if not url or not isinstance(url, str):
        return url
    try:
        p = urlparse(url)
        if not p.scheme or not p.netloc:
            return url
        # keep params that are not tracking
        tracking = {"e", "c", "fbclid", "gclid", "mc_cid", "mc_eid"}
        qsl = [
            (k, v)
            for k, v in parse_qsl(p.query, keep_blank_values=True)
            if not (k.startswith("utm_") or k in tracking)
        ]
        new_query = urlencode(qsl, doseq=True)
        new_p = p._replace(query=new_query)
        cleaned = urlunparse(new_p)
        if not new_query and cleaned.endswith("?"):
            cleaned = cleaned[:-1]
        return cleaned
    except Exception:
        return url


def unwrap_tracking_redirect(url: str) -> str:
    """Return the target from a Mailchimp track/click URL, if present."""
    parsed = urlparse(url)
    hostname = parsed.hostname
    if (
        hostname is None
        or (hostname != "list-manage.com" and not hostname.endswith(".list-manage.com"))
        or parsed.path != "/track/click"
    ):
        return url

    target = dict(parse_qsl(parsed.query, keep_blank_values=True)).get("url")
    return strip_tracking_params(target) if target else url


class _HrefParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.hrefs.extend(value for name, value in attrs if name == "href" and value)


def extract_ref_urls(html: str) -> list[str]:
    """Extract ordered, unique content URLs from newsletter HTML."""
    if not html:
        return []

    parser = _HrefParser()
    with suppress(Exception):
        parser.feed(html)
    ref_urls: list[str] = []
    seen: set[str] = set()
    image_suffixes = (".avif", ".bmp", ".gif", ".ico", ".jpeg", ".jpg", ".png", ".svg", ".webp")

    for href in parser.hrefs:
        try:
            url = unwrap_tracking_redirect(href)
        except Exception:
            continue
        try:
            parsed = urlparse(url)
        except ValueError:
            continue
        hostname = parsed.hostname
        if hostname is None:
            continue
        path = parsed.path.lower()
        is_campaign_archive = bool(re.search(r"(?:^|\.)campaign-archive\d*\.com$", hostname))
        is_facebook = hostname == "facebook.com" or hostname.endswith(".facebook.com")
        is_linkedin = hostname == "linkedin.com" or hostname.endswith(".linkedin.com")
        is_telegram = hostname == "t.me" or hostname.endswith(".t.me")
        is_twitter = (
            hostname == "twitter.com"
            or hostname.endswith(".twitter.com")
            or hostname == "x.com"
            or hostname.endswith(".x.com")
        )
        is_share = (
            (is_facebook and "sharer" in path)
            or (is_linkedin and (path == "/share" or path.startswith("/share/")))
            or (is_telegram and (path == "/share" or path.startswith("/share/")))
            or (is_twitter and "/intent/" in path)
        )
        if (
            parsed.scheme not in {"http", "https"}
            or hostname == "mailchi.mp"
            or hostname.endswith(".mailchi.mp")
            or is_campaign_archive
            or hostname == "list-manage.com"
            or hostname.endswith(".list-manage.com")
            or is_share
            or "unsubscribe" in path
            or path.endswith(image_suffixes)
        ):
            continue

        url = strip_tracking_params(url)
        if url not in seen:
            seen.add(url)
            ref_urls.append(url)

    return ref_urls


class ParsedNewsletter(BaseModel):
    """Parsed newsletter email data."""

    source_name: str
    subject: str
    from_email: str
    date: datetime
    html_content: str
    text_content: str


def parse_newsletter(payload: dict, source_name: str) -> ParsedNewsletter:
    """Parse Cloudflare Email Routing webhook payload into ParsedNewsletter."""
    from_email = payload.get("from", "")
    subject = payload.get("subject")
    if not subject:
        raise ValueError("Missing required field: subject")
    if not from_email:
        raise ValueError("Missing required field: from")

    # Strip common forward/reply prefixes (repeated, case-insens). If strip leaves empty, keep orig.
    # tolerate leading/trailing ws; support Chinese 轉寄:/回覆: from Gmail
    original = subject
    cleaned = re.sub(
        r"^\s*(?:(?:Re|Fwd|Fw|RE|FW|FWD|轉寄|回覆)[:：]\s*)+",
        "",
        subject,
        flags=re.IGNORECASE,
    ).strip()
    subject = cleaned if cleaned else original

    html_content = payload.get("html", "")
    text_content = payload.get("text", "")

    # Parse date from headers
    date_str = (payload.get("headers") or {}).get("Date", "")
    date = None
    if date_str:
        with suppress(Exception):
            date = parsedate_to_datetime(date_str)
        if date is None:
            with suppress(Exception):
                ds = date_str
                if ds.upper().endswith("Z"):
                    ds = ds[:-1] + "+00:00"
                date = datetime.fromisoformat(ds)
    if date is None or getattr(date, "tzinfo", None) is None:
        date = datetime.now(UTC) if date is None else date.replace(tzinfo=UTC)

    return ParsedNewsletter(
        source_name=source_name,
        subject=subject,
        from_email=from_email,
        date=date,
        html_content=html_content,
        text_content=text_content,
    )
