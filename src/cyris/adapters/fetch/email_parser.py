"""Email newsletter parsing utilities."""

import logging
import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class ParsedNewsletter(BaseModel):
    """Parsed newsletter email data."""

    source_name: str
    subject: str
    from_email: str
    date: datetime
    links: list[str]
    html_content: str
    text_content: str


class LinkExtractor(HTMLParser):
    """Extract href values from <a> tags."""

    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for name, value in attrs:
                if name == "href" and value and value.startswith("http"):
                    self.links.append(value)


def _extract_links(html: str) -> list[str]:
    """Extract and deduplicate HTTP links from HTML."""
    parser = LinkExtractor()
    try:
        parser.feed(html)
    except Exception:
        logger.warning("HTML parsing error", exc_info=True)
        return []
    # Deduplicate preserving order, filter tracking/unsubscribe links
    seen = set()
    result = []
    skip_patterns = re.compile(
        r"(unsubscribe|tracking|click\.|mailchimp|list-manage"
        # Patreon boilerplate: legal/marketing pages and the bare creator landing
        # page (patreon.com/<creator> with no /posts/), but keep real /posts/ links.
        r"|privacy\.patreon\.com"
        r"|patreon\.com/(policy|about|careers|press|login|signup|settings)"
        r"|patreon\.com/[\w-]+(\?|$))",
        re.I,
    )
    for link in parser.links:
        if link not in seen and not skip_patterns.search(link):
            seen.add(link)
            result.append(link)
    return result


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

    links = _extract_links(html_content)

    return ParsedNewsletter(
        source_name=source_name,
        subject=subject,
        from_email=from_email,
        date=date,
        links=links,
        html_content=html_content,
        text_content=text_content,
    )
