"""Newsletter article fetching."""

import hashlib
import logging

from cyris.adapters.fetch.email_parser import ParsedNewsletter
from cyris.adapters.fetch.extractor import extract_full_text
from cyris.adapters.http_client import HttpClient
from cyris.domain.models import Article, SourceConfig

logger = logging.getLogger(__name__)


def _generate_article_id(source_name: str, url: str) -> str:
    """Generate deterministic article ID from source name and URL."""
    return hashlib.sha256(f"{source_name}{url}".encode()).hexdigest()


async def fetch_newsletter_articles(
    parsed: ParsedNewsletter,
    source: SourceConfig,
    http_client: HttpClient,
    cookies: dict[str, str] | None = None,
) -> list[Article]:
    """Fetch full text for each link in newsletter and return as Articles."""
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
