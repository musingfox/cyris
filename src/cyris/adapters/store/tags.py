"""Normalized article tags in D1."""

from cyris.adapters.store.d1 import D1Queryable
from cyris.domain.tags import normalize_tag


class D1TagStore:
    def __init__(self, client: D1Queryable) -> None:
        self._db = client

    def save(self, url_to_tags: dict[str, list[str]]) -> None:
        for url, tags in url_to_tags.items():
            normalized = {
                value
                for tag in tags
                if (value := normalize_tag(tag)) is not None
            }
            for tag in sorted(normalized):
                self._db.query("INSERT OR IGNORE INTO tags (name) VALUES (?)", [tag])
                self._db.query(
                    "INSERT OR IGNORE INTO article_tags (article_url, tag) VALUES (?, ?)",
                    [url, tag],
                )
