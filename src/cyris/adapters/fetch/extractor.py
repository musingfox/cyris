"""Full-text content extraction from web pages."""

import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime

from cyris.adapters.http_client import HttpClient

logger = logging.getLogger(__name__)

PAYWALL_CONTENT_THRESHOLD = 300


@dataclass
class ExtractedContent:
    """Extracted article content from a URL."""

    url: str
    title: str = ""
    content: str = ""
    author: str | None = None
    published_at: datetime | None = None
    raw_html: str = ""


def _is_likely_paywalled(content: str) -> bool:
    """Check if content is likely behind a paywall based on length."""
    return len(content) < PAYWALL_CONTENT_THRESHOLD


async def extract_full_text(
    url: str,
    http_client: HttpClient,
    cookies: dict[str, str] | None = None,
) -> ExtractedContent:
    """Fetch URL and extract clean text via trafilatura.

    Fallback chain:
    1. Direct fetch
    2. Retry with cookies if provided and:
       - Direct fetch got 402/403, OR
       - Direct fetch got 200 but content is suspiciously short
    3. Return partial content with warning if all fail
    """
    import trafilatura

    result = ExtractedContent(url=url)

    try:
        response = await http_client.get(url)
        raw_html = response.text
    except Exception:
        logger.warning("Failed to fetch %s", url, exc_info=True)
        return result

    initial_status = response.status_code

    # Extract content from initial fetch
    result.raw_html = raw_html
    if initial_status < 400:
        extracted = trafilatura.extract(
            raw_html,
            include_comments=False,
            include_tables=True,
            output_format="txt",
        )
        if extracted:
            result.content = extracted

        metadata = trafilatura.extract_metadata(raw_html)
        if metadata:
            result.title = metadata.title or ""
            result.author = metadata.author
            if metadata.date:
                with contextlib.suppress(ValueError, TypeError):
                    result.published_at = datetime.fromisoformat(metadata.date)

    # Determine if we should retry with cookies
    should_retry = False
    if cookies:
        # Retry if explicit paywall status
        if initial_status in (402, 403):
            should_retry = True
            logger.debug("HTTP %d detected, retrying with cookies", initial_status)
        # Retry if status OK but content suspiciously short
        elif initial_status == 200 and _is_likely_paywalled(result.content):
            should_retry = True
            content_len = len(result.content)
            logger.debug("Short content detected (%d chars), retrying with cookies", content_len)

    if should_retry:
        try:
            retry_response = await http_client.get(url, cookies=cookies)
            retry_html = retry_response.text

            if retry_response.status_code == 200:
                retry_extracted = trafilatura.extract(
                    retry_html,
                    include_comments=False,
                    include_tables=True,
                    output_format="txt",
                )
                # Use retry result if it's longer
                if retry_extracted and len(retry_extracted) > len(result.content):
                    logger.debug(
                        "Cookie retry yielded longer content (%d → %d chars)",
                        len(result.content),
                        len(retry_extracted),
                    )
                    result.content = retry_extracted
                    result.raw_html = retry_html

                    # Update metadata from retry
                    retry_metadata = trafilatura.extract_metadata(retry_html)
                    if retry_metadata:
                        result.title = retry_metadata.title or result.title
                        result.author = retry_metadata.author or result.author
                        if retry_metadata.date:
                            with contextlib.suppress(ValueError, TypeError):
                                result.published_at = datetime.fromisoformat(retry_metadata.date)
        except Exception:
            logger.warning("Cookie retry failed for %s", url, exc_info=True)

    if initial_status >= 400 and not result.content:
        logger.warning("HTTP %d for %s", initial_status, url)

    return result
