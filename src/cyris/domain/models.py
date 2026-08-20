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
    paywall: bool = False
    language: str = "auto"
    email_match: str | None = None
    cookie_domain: str | None = None


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
    event_ref: str | None = None
    is_tracked_topic: bool = False
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
    def estimated_cost(self) -> float:
        """Estimate cost in USD based on Sonnet pricing ($3/M input, $15/M output)."""
        return (self.input_tokens * 3 + self.output_tokens * 15) / 1_000_000

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
    tracked_updates: DigestSection | None = None
    featured_articles: list[DigestSection] = Field(default_factory=list)
    news_clusters: list[DigestSection] = Field(default_factory=list)
    thematic_summaries: list[DigestSection] = Field(default_factory=list)
    attention_sections: list[DigestSection] = Field(default_factory=list)
    filtered_headlines: list[DigestItem] = Field(default_factory=list)
    fan_sections: list[DigestSection] = Field(default_factory=list)
    triage_pending_count: int | None = None
    dead_link_count: int | None = None
    synthetic_url_count: int | None = None


class PreferenceProfile(BaseModel):
    """User preference profile generated from feedback history."""

    generated_at: str
    sample_size: int
    themes: list[str]
    signals: list[str]
    anti_signals: list[str]
    prompt_injection: str


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
    miniflux_ids: list[int]


class ProcessResult(BaseModel):
    """Result of processing articles through tier-based pipeline."""

    content: DigestContent
    accepted_urls: list[str]
    rejected_urls: list[str]


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
