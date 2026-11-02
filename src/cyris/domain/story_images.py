"""Which feed picture a Stories item or a Topics block shows (UI spec §4 *story image*).

A picture that two or more of one source's articles share in an issue is the site's
default, not the article's: on 2026-10-30 one news site gave the same logo to 29 of its
73 links. The count runs over every article the issue was made from, not only the ones
it shows, so a logo shared with an article that landed in Headlines is still caught.
A topic shows the picture of its first member that has one of its own (`domain.layers`).
"""

from collections import Counter
from collections.abc import Iterable

from cyris.domain.models import Article, DigestSection


def article_images(articles: Iterable[Article]) -> dict[str, str]:
    """Each article's own picture by URL, leaving out every source's shared default."""
    pictured = {a.url: a for a in articles if a.image_url}.values()
    shares = Counter((a.source_name, a.image_url) for a in pictured)
    return {
        a.url: a.image_url
        for a in pictured
        if a.image_url and shares[(a.source_name, a.image_url)] == 1
    }


def stamp_story_images(sections: Iterable[DigestSection], articles: Iterable[Article]) -> None:
    """Give each single-article item its article's own picture; a merged item gets none."""
    images = article_images(articles)
    for section in sections:
        for item in section.items:
            if len(item.urls) == 1:
                item.image_url = images.get(item.urls[0])


def stamp_topic_images(clusters: Iterable[DigestSection], articles: Iterable[Article]) -> None:
    """Give each news cluster's members their article's own picture; its vote item gets none."""
    images = article_images(articles)
    for cluster in clusters:
        for member in cluster.members:
            if len(member.urls) == 1:
                member.image_url = images.get(member.urls[0])
