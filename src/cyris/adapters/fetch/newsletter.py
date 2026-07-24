"""Newsletter article fetching."""

import hashlib
import html
import logging
import re

from cyris.adapters.fetch.email_parser import ParsedNewsletter
from cyris.adapters.fetch.extractor import extract_full_text
from cyris.adapters.http_client import HttpClient
from cyris.domain.models import Article, SourceConfig, Tier

logger = logging.getLogger(__name__)


def _generate_article_id(source_name: str, url: str) -> str:
    """Generate deterministic article ID from source name and URL."""
    return hashlib.sha256(f"{source_name}{url}".encode()).hexdigest()


def _body_article(parsed: ParsedNewsletter, source: SourceConfig) -> Article:
    """The email body as a single Article.

    Fan newsletters ARE the content: the essay lives in the mail itself, and
    Mailchimp wraps every link in list-manage tracking URLs that the link
    filter drops — so link-chasing yields nothing.
    """
    article_id = _generate_article_id(source.name, parsed.subject)
    # Mailchimp's "view in browser" link is the issue's canonical web URL;
    # fall back to a synthetic unique URL so store dedup stays per-issue.
    web_view = next(
        (link for link in parsed.links if "mailchi.mp" in link or "campaign-archive" in link),
        None,
    )
    content = parsed.text_content.strip() or " ".join(
        html.unescape(re.sub(r"<[^>]+>", " ", parsed.html_content)).split()
    )
    return Article(
        id=article_id,
        title=parsed.subject,
        url=web_view or f"newsletter:{article_id}",
        content=content,
        published_at=parsed.date,
        source_name=source.name,
        source_tier=source.tier,
        source_tags=source.tags,
    )


async def fetch_newsletter_articles(
    parsed: ParsedNewsletter,
    source: SourceConfig,
    http_client: HttpClient,
    cookies: dict[str, str] | None = None,
) -> list[Article]:
    """Fetch full text for each link in newsletter and return as Articles.

    Fan tier skips link fetching entirely: the email body becomes one Article.
    """
    if source.tier == Tier.FAN:
        return [_body_article(parsed, source)]
    articles = []
    for url in parsed.links:
        try:
            extracted = await extract_full_text(url, http_client, cookies=cookies)
            if not extracted.content:
                logger.debug("No content extracted from %s, skipping", url)
                continue
            article = Article(
                id=_generate_article_id(source.name, url),
                title=extracted.title or parsed.subject,
                url=url,
                content=extracted.content,
                author=extracted.author,
                published_at=parsed.date,
                source_name=source.name,
                source_tier=source.tier,
                source_tags=source.tags,
            )
            articles.append(article)
        except Exception:
            logger.error("Failed to fetch article from %s", url, exc_info=True)
    return articles
