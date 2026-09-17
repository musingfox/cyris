"""The one dedup rule both ArticleRepository implementations apply on save.

URL stays the only key, because D1's primary key is the URL and every consumer
(votes, triage, scores, tags, stories) looks rows up by it. A newsletter issue is
identified by URL plus subject instead: when a sender's repeated link (a nav or
settings URL) is already held by an earlier issue of the same source under a
different subject, the later issue is stored under its synthetic
`newsletter:{id}` URL and loses that link, rather than being dropped.
"""

from cyris.domain.models import NEWSLETTER_SOURCE_TYPE, Article

Holders = dict[str, tuple[str, str]]
"""URL -> (title, source_name) of the row already holding it."""


def synthetic_url(article: Article) -> str:
    return f"newsletter:{article.id}"


def resolve_stored_url(article: Article, holders: Holders) -> str | None:
    """The URL to store `article` under, or None when it is a duplicate."""
    held = holders.get(article.url)
    if held is None:
        return article.url
    title, source_name = held
    if (
        article.source_type != NEWSLETTER_SOURCE_TYPE
        or source_name != article.source_name
        or title == article.title
    ):
        return None
    synthetic = synthetic_url(article)
    return None if synthetic in holders else synthetic
