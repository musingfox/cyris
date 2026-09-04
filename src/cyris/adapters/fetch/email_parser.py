"""Email newsletter parsing utilities."""

import logging
from contextlib import suppress
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from pydantic import BaseModel

from cyris.adapters.fetch.keywords import (
    base_tracking_params,
    is_rejected_host,
    is_rejected_path,
    is_share_link,
    subject_prefix_re,
    tracking_params,
    tracking_redirect_param,
)

logger = logging.getLogger(__name__)


def strip_tracking_params(url: str, extra_params: frozenset[str] | None = None) -> str:
    """Strip tracking query params from a URL.

    The stripped set is `base_tracking_params` in keywords.json plus any `utm_`-prefixed
    key; `extra_params` adds to it, which is how newsletter callers also drop the
    per-recipient parameters that RSS callers keep. Non-URL or unparsable input is
    returned verbatim (no exception); a trailing `?` is removed when nothing remains.
    """
    if not url or not isinstance(url, str):
        return url
    try:
        p = urlparse(url)
        if not p.scheme or not p.netloc:
            return url
        tracking = base_tracking_params()
        if extra_params:
            tracking = tracking | set(extra_params)
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
    if not isinstance(url, str) or not url:
        return url
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if hostname is None:
            return url
        param = tracking_redirect_param(hostname.lower(), parsed.path)
        if param is None:
            return url

        target = dict(parse_qsl(parsed.query, keep_blank_values=True)).get(param)
        return strip_tracking_params(target) if target else url
    except Exception:
        return url


class _HrefParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.hrefs.extend(value for name, value in attrs if name == "href" and value)


_MAX_REF_URLS = 5
# Re-exported: callers import it from here, the values live in keywords.json.
NEWSLETTER_TRACKING_PARAMS = tracking_params()


def is_content_url(url: str) -> bool:
    """True when url is a content link (not ESP, share, unsubscribe, or image)."""
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    hostname = parsed.hostname
    if hostname is None:
        return False
    hostname = hostname.lower()
    path = parsed.path.lower()
    return not (
        parsed.scheme not in {"http", "https"}
        or is_rejected_host(hostname)
        or is_share_link(hostname, path)
        # "unsubscribe" anywhere in the path, "checkout" only as a whole segment --
        # /checkout-ux-redesign is an article, and its per-subscriber rid would
        # otherwise be offered as this issue's canonical link on the public digest.
        or is_rejected_path(path)
    )


def extract_ref_urls(html: str) -> list[str]:
    """Extract ordered, unique content URLs from newsletter HTML (first _MAX_REF_URLS)."""
    if not html:
        return []

    parser = _HrefParser()
    with suppress(Exception):
        parser.feed(html)
    ref_urls: list[str] = []
    seen: set[str] = set()

    for href in parser.hrefs:
        try:
            url = unwrap_tracking_redirect(href)
        except Exception:
            continue
        if not is_content_url(url):
            continue

        url = strip_tracking_params(url, extra_params=NEWSLETTER_TRACKING_PARAMS)
        if url not in seen:
            seen.add(url)
            ref_urls.append(url)
            # ponytail: hard cap keeps link-farm newsletters from flooding the reference links;
            # make it configurable only if a real source needs more
            if len(ref_urls) >= _MAX_REF_URLS:
                break

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
    # The prefix words come from keywords.json; the repetition and the ws do not.
    original = subject
    cleaned = subject_prefix_re().sub("", subject).strip()
    subject = cleaned if cleaned else original

    html_content = payload.get("html", "")
    text_content = payload.get("text", "")

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
