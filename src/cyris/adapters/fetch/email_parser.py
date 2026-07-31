"""Email newsletter parsing utilities."""

import logging
from datetime import datetime
from email.utils import parsedate_to_datetime
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

    html_content = payload.get("html", "")
    text_content = payload.get("text", "")

    # Parse date from headers
    date_str = (payload.get("headers") or {}).get("Date", "")
    try:
        date = parsedate_to_datetime(date_str)
    except Exception:
        date = datetime.now()

    return ParsedNewsletter(
        source_name=source_name,
        subject=subject,
        from_email=from_email,
        date=date,
        html_content=html_content,
        text_content=text_content,
    )
