"""Core data models for Cyris digest pipeline."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Tier(StrEnum):
    """Content processing tier determining filtering depth."""

    FILTER = "filter"
    SUMMARIZE = "summarize"
    FAN = "fan"  # followed groups/newsletters: passthrough, never scored/filtered/summarized


class SourceConfig(BaseModel):
    """Configuration for a single content source."""

    name: str
    url: str | None = None
    type: str = "rss"
    tier: Tier = Tier.FILTER
    tags: list[str] = Field(default_factory=list)
    language: str = "auto"
    email_match: str | None = None
    # Where this source publishes. Its host is what makes "the sender's own domain"
    # knowable instead of guessed from link statistics.
    homepage: str | None = None


class Article(BaseModel):
    """A fetched article ready for processing."""

    id: int | str
    title: str
    url: str
    content: str
    author: str | None = None
    published_at: datetime
    source_name: str
    source_tier: Tier
    source_tags: list[str] = Field(default_factory=list)
    ref_urls: list[str] = Field(default_factory=list)


class DigestItem(BaseModel):
    """A single item in the digest output."""

    title: str
    summary: str
    sources: list[str]
    urls: list[str]
    score: float | None = None
    ref_urls: list[str] = Field(default_factory=list)

    @property
    def link(self) -> str | None:
        """First clickable link, or None. Synthetic store URLs (newsletter:<id>) are dead."""
        return next(
            (u for u in (*self.ref_urls, *self.urls) if u.startswith(("http://", "https://"))),
            None,
        )


class DigestSection(BaseModel):
    """A thematic section containing multiple digest items."""

    heading: str
    description: str | None = None
    items: list[DigestItem] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


# USD per million tokens, (input, output), keyed by the exact model id the
# adapter reports. Read off each vendor's own pricing page on 2026-08-25.
# Only models this repo actually runs are here on purpose: a miss returns None,
# which is the one behaviour that cannot repeat the bug this table replaced.
# Workers AI is deliberately absent — it bills in neurons rather than tokens,
# and `WorkersAIClient.neurons` is the receipt for that.
_PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "gemini-3.7-flash": (0.75, 3.75),  # promotional; 1.50/7.50 from 2027-01-01
    "gemini-3.6-flash": (0.75, 3.75),  # same, and same expiry
    "gemini-2.5-flash": (0.30, 2.50),
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5.6-terra": (2.00, 12.00),
}


class UsageStats(BaseModel):
    """LLM API usage statistics for a digest run."""

    input_tokens: int = 0
    output_tokens: int = 0
    api_calls: int = 0
    model: str = ""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def estimated_cost(self) -> float | None:
        """USD for this run, or None when this model's rate card is not known here.

        None is an answer, not a gap. The old version applied Sonnet's $3/$15 to
        whatever had run, so a Gemini digest reported four times its real cost
        and nothing in the output said which vendor the number came from. An
        unpriced model now prints no number at all rather than borrowing one.
        """
        price = _PRICES_PER_MTOK.get(self.model)
        if price is None:
            return None
        input_price, output_price = price
        return (self.input_tokens * input_price + self.output_tokens * output_price) / 1_000_000

    def add(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.api_calls += 1


class DigestContent(BaseModel):
    """Complete digest content ready for rendering."""

    date: str
    period: str
    sources_processed: int
    articles_received: int
    articles_included: int
    usage: UsageStats = Field(default_factory=UsageStats)
    featured_articles: list[DigestSection] = Field(default_factory=list)
    news_clusters: list[DigestSection] = Field(default_factory=list)
    thematic_summaries: list[DigestSection] = Field(default_factory=list)
    attention_sections: list[DigestSection] = Field(default_factory=list)
    filtered_headlines: list[DigestItem] = Field(default_factory=list)
    fan_sections: list[DigestSection] = Field(default_factory=list)
    triage_pending_count: int | None = None
    dead_link_count: int | None = None
    synthetic_url_count: int | None = None


class ArticleState(StrEnum):
    """Article lifecycle state in persistent storage."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    AWAITING_TRIAGE = "awaiting_triage"


class StoredArticle(BaseModel):
    """Persistent article with lifecycle state. Wraps Article data."""

    url: str  # primary key for dedup
    original_id: int | str
    title: str
    content: str
    author: str | None = None
    published_at: datetime
    source_name: str
    source_tier: Tier
    source_tags: list[str] = Field(default_factory=list)
    ref_urls: list[str] = Field(default_factory=list)

    state: ArticleState = ArticleState.PENDING
    first_seen_at: datetime
    digest_date: str | None = None
    rejection_reason: str | None = None
    score: float | None = None
    language: str | None = None
    scored_at: datetime | None = None
    triaged_at: datetime | None = None
    exported_at: datetime | None = None

    @classmethod
    def from_article(cls, article: Article, first_seen_at: datetime) -> "StoredArticle":
        """Create StoredArticle from Article with default state."""
        return cls(
            url=article.url,
            original_id=article.id,
            title=article.title,
            content=article.content,
            author=article.author,
            published_at=article.published_at,
            source_name=article.source_name,
            source_tier=article.source_tier,
            source_tags=article.source_tags,
            ref_urls=article.ref_urls,
            first_seen_at=first_seen_at,
        )

    def to_article(self) -> Article:
        """Reconstruct Article from stored data."""
        return Article(
            id=self.original_id,
            title=self.title,
            url=self.url,
            content=self.content,
            author=self.author,
            published_at=self.published_at,
            source_name=self.source_name,
            source_tier=self.source_tier,
            source_tags=self.source_tags,
            ref_urls=self.ref_urls,
        )


class SaveResult(BaseModel):
    """Result of saving articles to store."""

    saved_count: int
    skipped_count: int


class StoryRecord(BaseModel):
    """A news cluster's full pre-truncation membership, keyed for one digest window.

    Deliberately carries no tags: the normalized ones already live on the member
    articles (`article_tags`), and a second home for raw LLM strings would drift.
    """

    id: str  # "{digest_date}-{period}-{n}", deterministic per window
    heading: str
    urls: list[str]


class ProcessResult(BaseModel):
    """Result of processing articles through tier-based pipeline."""

    content: DigestContent
    accepted_urls: list[str]
    rejected_urls: list[str]
    url_to_tags: dict[str, list[str]] = Field(default_factory=dict)
    story_records: list[StoryRecord] = Field(default_factory=list)


class TriageFeedbackData(BaseModel):
    """Triage feedback extracted from article store."""

    accepted_articles: list[StoredArticle]
    rejected_articles: list[StoredArticle]
    date_range_start: str  # ISO format
    date_range_end: str  # ISO format

    @property
    def accepted_count(self) -> int:
        return len(self.accepted_articles)

    @property
    def rejected_count(self) -> int:
        return len(self.rejected_articles)
