"""Multi-strategy source name matcher for Miniflux feed titles."""

import logging
from difflib import SequenceMatcher

from cyris.domain.models import SourceConfig

logger = logging.getLogger(__name__)

FUZZY_THRESHOLD = 0.85


class SourceMatcher:
    """Match Miniflux feed titles to source configs using multiple strategies."""

    def __init__(
        self,
        sources: dict[str, SourceConfig],
        alias_map: dict[str, str] | None = None,
    ) -> None:
        self._sources = sources
        self._aliases = alias_map or {}
        # Pre-build lowercase lookup
        self._lower_map = {name.lower(): name for name in sources}

    def match(self, feed_title: str) -> SourceConfig | None:
        """Match a feed title to a source config.

        Strategies (in order):
        1. Exact match
        2. Alias map lookup
        3. Case-insensitive match
        4. Fuzzy match (SequenceMatcher ratio > 0.85)

        Returns None if no match found.
        """
        # 1. Exact match
        if feed_title in self._sources:
            return self._sources[feed_title]

        # 2. Alias map
        canonical = self._aliases.get(feed_title)
        if canonical and canonical in self._sources:
            return self._sources[canonical]

        # 3. Case-insensitive
        lower = feed_title.lower()
        if lower in self._lower_map:
            return self._sources[self._lower_map[lower]]

        # 4. Fuzzy match
        best_ratio = 0.0
        best_name = None
        for name in self._sources:
            ratio = SequenceMatcher(None, lower, name.lower()).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_name = name

        if best_ratio >= FUZZY_THRESHOLD and best_name is not None:
            logger.info(
                "Fuzzy matched '%s' → '%s' (%.0f%%)", feed_title, best_name, best_ratio * 100
            )
            return self._sources[best_name]

        logger.warning("Unmatched feed: '%s' — add to sources.yaml or aliases", feed_title)
        return None
