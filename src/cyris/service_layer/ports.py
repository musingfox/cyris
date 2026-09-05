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
    # Cloudflare bills Workers AI in neurons and reports them per request, which is
    # better than deriving a number from published rates. None everywhere else: a
    # provider that does not bill this way has nothing to report, and a zero would
    # read as a measurement.
    neurons: float | None = None


class LLMClient(Protocol):
    """Single-turn LLM completion boundary."""

    model: str

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse: ...


class ArticleRepository(Protocol):
    """Persistence boundary for the article store.

    Every method a caller actually uses belongs here, not just the ones the
    digest run touches: `ArticleStore` and `D1ArticleStore` both satisfy this
    structurally, and a replacement that covers less used to fail at the CLI or
    the triage UI rather than at import — there is no type checker here to say
    otherwise. `tests/test_protocol_conformance.py` is where it fails now.
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

    def list_articles(
        self,
        state: ArticleState | list[ArticleState] | None = None,
        limit: int = 100,
        offset: int = 0,
        sort_by: str = "first_seen_at",
        descending: bool = True,
    ) -> list[StoredArticle]: ...

    def update_article_state(
        self, url: str, state: ArticleState, reason: str | None = None
    ) -> bool: ...

    def update_triage_timestamp(self, urls: list[str], triaged_at: datetime) -> int: ...

    def delete_articles(
        self, state: ArticleState | list[ArticleState], older_than_days: int | None = None
    ) -> int:
        """Delete matching articles, except human-triaged ones.

        Rows with a non-null `triaged_at` are never deleted regardless of the
        filters: that stamp marks a real human decision (digest vote, triage
        UI, `cyris articles accept|reject`), and those rows are the training
        signal that seeds vote similarity. Every implementation must honor
        this exclusion — a backend that deletes stamped rows silently erodes
        the corpus.
        """
        ...


@runtime_checkable
class FetchSource(Protocol):
    """Protocol for pluggable article fetch sources."""

    async def fetch_articles(
        self,
        after: datetime,
        before: datetime,
        sources: dict[str, SourceConfig],
        limit: int = 200,
    ) -> list[Article]:
        """Fetch articles within a time window."""
        ...

    async def health_check(self) -> bool:
        """Check if the source is reachable."""
        ...


class EmbeddingUsage(Protocol):
    """What one embedding provider spent, for the side-by-side log.

    `input_tokens` and `neurons` are None where the API does not report them —
    Gemini's `batchEmbedContents` returns bare vectors — rather than filled with a
    guess that would read like a measurement.
    """

    requests: int
    embedded: int
    api_seconds: float
    input_tokens: int | None
    neurons: float | None

    def as_dict(self) -> dict[str, object]: ...


class Embedder(Protocol):
    """Text-to-vector boundary, for judging articles against what was voted on."""

    # Declared because a caller reads it: `embed-compare` cannot answer "which
    # provider costs less" without it, and an implementation that omits it would
    # fail there rather than at import — `tests/test_protocol_conformance.py`
    # catches the omission first.
    usage: EmbeddingUsage

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one unit-length vector per input, in the same order."""
        ...


async def complete_json(
    llm: LLMClient,
    prompt: str,
    *,
    system: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    usage: UsageStats | None = None,
) -> dict:
    """Call the LLM and parse a JSON object from its response, accumulating usage.

    max_tokens=None lets each adapter default to its provider's output limit —
    thinking tokens count against the cap, so callers should not guess a number.
    """
    response = await llm.complete(
        prompt, system=system, max_tokens=max_tokens, temperature=temperature
    )
    if usage is not None:
        usage.add(response.input_tokens, response.output_tokens, response.neurons)
    return extract_json(response.text)
