"""Cross-boundary Protocols for the service layer.

Only genuine IO boundaries get a Protocol here; single-implementation
components are injected directly.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from cyris.domain.models import Article, ArticleState, SourceConfig, StoredArticle, UsageStats
from cyris.service_layer.parse import extract_json


@dataclass(frozen=True)
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int


class LLMClient(Protocol):
    """Single-turn LLM completion boundary."""

    model: str

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 16384,
        temperature: float | None = None,
    ) -> LLMResponse: ...


class ArticleRepository(Protocol):
    """Persistence boundary for the article store.

    Declares only what use cases need; ArticleStore satisfies it structurally.
    """

    def save(self, articles: list[Article], now: datetime | None = None): ...

    def load_by_time_range(
        self,
        start: datetime,
        end: datetime,
        state_filter: ArticleState | None = None,
    ) -> list[StoredArticle]: ...

    def get_by_urls(self, urls: list[str]) -> list[StoredArticle]: ...

    def update_scores(
        self, url_to_score_lang: dict[str, tuple[float, str]], scan_days: int = 30
    ) -> int: ...

    def update_states(
        self, url_to_state: dict[str, tuple[ArticleState, str | None]], digest_date: str
    ) -> int: ...

    def count_by_state(self) -> dict[ArticleState, int]: ...

    def accept(self, urls: list[str]) -> int: ...

    def reject(self, urls: list[str], reason: str) -> int: ...

    def reset_to_pending(self, url: str) -> bool: ...


@runtime_checkable
class FetchSource(Protocol):
    """Protocol for pluggable article fetch sources."""

    async def fetch_articles(
        self,
        after: datetime,
        before: datetime,
        sources: dict[str, SourceConfig],
        aliases: dict[str, str] | None = None,
        limit: int = 200,
        cookies: dict[str, str] | None = None,
    ) -> list[Article]:
        """Fetch articles within a time window."""
        ...

    async def mark_as_read(self, article_ids: list[int | str]) -> None:
        """Mark articles as read. No-op if source doesn't support it."""
        ...

    async def health_check(self) -> bool:
        """Check if the source is reachable."""
        ...


async def complete_json(
    llm: LLMClient,
    prompt: str,
    *,
    system: str | None = None,
    max_tokens: int = 16384,
    temperature: float | None = None,
    usage: UsageStats | None = None,
) -> dict:
    """Call the LLM and parse a JSON object from its response, accumulating usage."""
    response = await llm.complete(
        prompt, system=system, max_tokens=max_tokens, temperature=temperature
    )
    if usage is not None:
        usage.add(response.input_tokens, response.output_tokens)
    return extract_json(response.text)
