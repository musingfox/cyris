"""DigestPipeline - stage-based digest processing pipeline."""

import logging

from cyris.domain.models import (
    Article,
    DigestContent,
    EmbeddingStore,
    PreferenceProfile,
    ProcessResult,
    SourceConfig,
    Tier,
    UsageStats,
)
from cyris.domain.selection import select_digest_articles, split_summarize_tier_by_score
from cyris.service_layer.cluster_news import cluster_news, filter_news
from cyris.service_layer.filtering import filter_articles
from cyris.service_layer.ports import LLMClient
from cyris.service_layer.summarize import build_attention_sections, summarize_articles
from cyris.utils.timezone import now_in_timezone

logger = logging.getLogger(__name__)


class DigestPipeline:
    """Stage-based pipeline for digest articles.

    Learning data (preference profile, embedding centroid) is loaded by the
    caller and injected — the pipeline itself does no IO beyond LLM calls.
    """

    def __init__(
        self,
        llm: LLMClient,
        preference_profile: PreferenceProfile | None = None,
        embedding_store: EmbeddingStore | None = None,
        max_digest_output: int = 15,
        summarize_snippet_length: int = 1000,
        filter_snippet_length: int = 500,
        score_threshold: int = 70,
        output_language: str = "繁體中文",
        style_prompt: str = "",
    ) -> None:
        self._llm = llm
        self.max_digest_output = max_digest_output
        self.summarize_snippet_length = summarize_snippet_length
        self.filter_snippet_length = filter_snippet_length
        self.score_threshold = score_threshold
        self.output_language = output_language
        self.style_prompt = style_prompt
        self.preference_profile = preference_profile
        self.embedding_store = embedding_store

    async def process(
        self,
        articles: list[Article],
        sources: dict[str, SourceConfig],
        period: str = "morning",
        timezone: str = "Asia/Taipei",
        article_scores: dict[str, float] | None = None,
    ) -> ProcessResult:
        """Process articles through tier-based pipeline.

        Splits articles by tier, delegates to filter/summarize processors,
        and assembles a complete DigestContent with URL classification.

        Args:
            articles: All fetched articles.
            sources: Source configs keyed by name.
            period: Digest period ("morning" or "evening").
            timezone: IANA timezone for date formatting.

        Returns:
            ProcessResult with content and URL classification.
        """
        today = now_in_timezone(timezone).strftime("%Y-%m-%d")
        source_names = {a.source_name for a in articles}

        if not articles:
            logger.warning("No articles to process")
            return ProcessResult(
                content=DigestContent(
                    date=today,
                    period=period,
                    sources_processed=0,
                    articles_received=0,
                    articles_included=0,
                ),
                accepted_urls=[],
                rejected_urls=[],
            )

        filter_tier = [a for a in articles if a.source_tier == Tier.FILTER]
        summarize_tier = [a for a in articles if a.source_tier == Tier.SUMMARIZE]

        logger.info(
            "Processing %d articles: %d filter, %d summarize",
            len(articles),
            len(filter_tier),
            len(summarize_tier),
        )

        usage = UsageStats(model=self._llm.model if self._llm else "none")

        # Split filter tier into news and non-news
        news_articles, non_news_articles = filter_news(filter_tier)

        logger.info(
            "Filter tier split: %d news articles, %d non-news articles",
            len(news_articles),
            len(non_news_articles),
        )

        # Cluster news articles
        news_clusters = []
        unclustered_news = []
        if news_articles:
            news_clusters, unclustered_news = await cluster_news(
                news_articles,
                self._llm,
                usage=usage,
                article_scores=article_scores,
                output_language=self.output_language,
                style_prompt=self.style_prompt,
            )

        # Merge unclustered news back into general filter pool
        filter_pool = non_news_articles + unclustered_news

        filtered = await filter_articles(
            filter_pool,
            self._llm,
            usage=usage,
            preference_profile=self.preference_profile,
            embedding_store=self.embedding_store,
            article_scores=article_scores,
            filter_snippet_length=self.filter_snippet_length,
            output_language=self.output_language,
            style_prompt=self.style_prompt,
        )

        # Split summarize tier by score threshold
        high_score_articles, low_score_articles = split_summarize_tier_by_score(
            summarize_tier, article_scores, self.score_threshold
        )

        logger.info(
            "Split summarize tier: %d high-score (AI), %d low-score (attention)",
            len(high_score_articles),
            len(low_score_articles),
        )

        # AI summarization for high-score articles
        summaries = await summarize_articles(
            high_score_articles,
            self._llm,
            usage=usage,
            snippet_length=self.summarize_snippet_length,
            article_scores=article_scores,
            output_language=self.output_language,
            style_prompt=self.style_prompt,
        )

        # Build attention sections for low-score articles
        attention = build_attention_sections(low_score_articles, article_scores)

        total_included = (
            len(filtered)
            + sum(len(s.items) for s in summaries)
            + sum(len(a.items) for a in attention)
            + sum(len(c.items) for c in news_clusters)
        )

        logger.info(
            "API usage: %d calls, %d input + %d output tokens, ~$%.4f",
            usage.api_calls,
            usage.input_tokens,
            usage.output_tokens,
            usage.estimated_cost,
        )

        # Classify URLs: accepted (in digest) vs rejected (filtered out)
        accepted_urls = []
        rejected_urls = []

        # Filter tier: accepted are in filtered items + news clusters, rejected are the rest
        filter_urls = {a.url for a in filter_tier}
        accepted_filter_urls = set()
        for item in filtered:
            accepted_filter_urls.update(item.urls)
        for cluster in news_clusters:
            for item in cluster.items:
                accepted_filter_urls.update(item.urls)
        rejected_filter_urls = filter_urls - accepted_filter_urls

        # Summarize tier: all articles are accepted (all get summarized)
        summarize_urls = {a.url for a in summarize_tier}

        accepted_urls = list(accepted_filter_urls | summarize_urls)
        rejected_urls = list(rejected_filter_urls)

        content = DigestContent(
            date=today,
            period=period,
            sources_processed=len(source_names),
            articles_received=len(articles),
            articles_included=total_included,
            usage=usage,
            news_clusters=news_clusters,
            thematic_summaries=summaries,
            attention_sections=attention,
            filtered_headlines=filtered,
        )

        content = select_digest_articles(content, max_items=self.max_digest_output)

        return ProcessResult(
            content=content,
            accepted_urls=accepted_urls,
            rejected_urls=rejected_urls,
        )
