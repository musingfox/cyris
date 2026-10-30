"""DigestPipeline - stage-based digest processing pipeline."""

import hashlib
import logging
from collections.abc import Awaitable, Callable, Mapping

from cyris.domain.models import (
    NO_LLM_MODEL,
    Article,
    DigestContent,
    DigestSection,
    Lane,
    ProcessResult,
    SourceConfig,
    StoryRecord,
    TopicRecord,
    UsageStats,
)
from cyris.domain.selection import (
    digest_urls,
    layer_by_score,
    order_by_preference,
    preference_moves,
    split_read_lane_by_score,
)
from cyris.domain.topics import stand_in_topics
from cyris.service_layer.cluster_news import cluster_news, filter_news
from cyris.service_layer.filtering import filter_articles
from cyris.service_layer.ports import LLMClient
from cyris.service_layer.schedule import Period
from cyris.service_layer.summarize import (
    build_attention_sections,
    build_follow_sections,
    explain_follow_articles,
    summarize_articles,
)
from cyris.utils.timezone import now_in_timezone

logger = logging.getLogger(__name__)

PreferenceSource = Callable[
    [list[Article], list[TopicRecord]], Awaitable[Mapping[str, float] | None]
]


def _story_id(date: str, period: str, member_urls: list[str]) -> str:
    """Content-derived story id: the same member set yields the same id.

    An enumerate position would be deterministic only per run — a re-run of
    the same window that clusters differently would bind an old id to a new
    story. Hashing the sorted member URLs keys the id to the story itself.
    """
    membership = hashlib.sha1("\n".join(sorted(member_urls)).encode("utf-8")).hexdigest()[:8]
    return f"{date}-{period}-{membership}"


def _stamp_tags(sections: list[DigestSection], article_tags: Mapping[str, list[str]]) -> None:
    """Give each item the tags of its articles, in order, each tag once."""
    for section in sections:
        for item in section.items:
            item.tags = list(
                dict.fromkeys(tag for url in item.urls for tag in article_tags.get(url, []))
            )


class DigestPipeline:
    """Stage-based pipeline for digest articles.

    Does no IO beyond LLM calls: everything else is injected by the caller.
    """

    def __init__(
        self,
        llm: LLMClient,
        *,
        summarize_snippet_length: int,
        filter_snippet_length: int,
        summarize_score_threshold: int,
        featured_threshold: int,
        output_language: str,
        style_prompt: str,
        summarize_follow: bool,
    ) -> None:
        self._llm = llm
        self.summarize_snippet_length = summarize_snippet_length
        self.filter_snippet_length = filter_snippet_length
        self.summarize_score_threshold = summarize_score_threshold
        self.featured_threshold = featured_threshold
        self.output_language = output_language
        self.style_prompt = style_prompt
        self.summarize_follow = summarize_follow

    async def process(
        self,
        articles: list[Article],
        sources: dict[str, SourceConfig],
        period: Period = "morning",
        *,
        timezone: str,
        article_scores: dict[str, float] | None = None,
        article_tags: Mapping[str, list[str]] | None = None,
        preference: PreferenceSource | None = None,
        date: str | None = None,
    ) -> ProcessResult:
        """Process articles through lane-based pipeline.

        Splits articles by lane, delegates to filter/summarize processors,
        and assembles a complete DigestContent with URL classification.

        Args:
            articles: All fetched articles.
            sources: Source configs keyed by name.
            period: Digest period ("morning" or "evening").
            timezone: IANA timezone for date formatting.
            article_tags: The tags scoring gave each article this run, by URL; each
                read-lane item carries its articles' tags.
            preference: Scores the articles this issue kept, by URL, given the
                issue's topics, to order the Features, the news clusters and the
                headlines; None keeps the order of the score and the model.
            date: The issue's date, YYYY-MM-DD; None means today in `timezone`.

        Returns:
            ProcessResult with content and URL classification.
        """
        today = date or now_in_timezone(timezone).strftime("%Y-%m-%d")
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
                    output_language=self.output_language,
                ),
                accepted_urls=[],
                rejected_urls=[],
            )

        skim_lane = [a for a in articles if a.source_lane == Lane.SKIM]
        read_lane = [a for a in articles if a.source_lane == Lane.READ]
        follow_lane = [a for a in articles if a.source_lane == Lane.FOLLOW]

        logger.info(
            "Processing %d articles: %d skim, %d read, %d follow",
            len(articles),
            len(skim_lane),
            len(read_lane),
            len(follow_lane),
        )

        usage = UsageStats(
            model=self._llm.model if self._llm else NO_LLM_MODEL,
            fell_back_to_excerpts=self._llm is None,
        )

        # Split skim lane into news and non-news
        news_articles, non_news_articles = filter_news(skim_lane)

        logger.info(
            "Skim lane split: %d news articles, %d non-news articles",
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
                # The cluster step is the skim lane's first pass: what it leaves
                # unclustered goes on to the filter below, read at the same length.
                snippet_length=self.filter_snippet_length,
                output_language=self.output_language,
                style_prompt=self.style_prompt,
            )

        # Merge unclustered news back into general filter pool
        filter_pool = non_news_articles + unclustered_news

        filtered = await filter_articles(
            filter_pool,
            self._llm,
            usage=usage,
            article_scores=article_scores,
            filter_snippet_length=self.filter_snippet_length,
            output_language=self.output_language,
            style_prompt=self.style_prompt,
        )

        # Split read lane by score threshold
        high_score_articles, low_score_articles = split_read_lane_by_score(
            read_lane, article_scores, self.summarize_score_threshold
        )

        logger.info(
            "Split read lane: %d high-score (AI), %d low-score (attention)",
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

        # Follow lane: grouped by source, never scored or filtered; explained only when
        # the reader asked for it.
        explanations = None
        if self.summarize_follow and self._llm is not None and follow_lane:
            explanations = await explain_follow_articles(
                follow_lane,
                self._llm,
                usage=usage,
                snippet_length=self.summarize_snippet_length,
                output_language=self.output_language,
                style_prompt=self.style_prompt,
            )
        follow_sections = build_follow_sections(follow_lane, explanations)

        _stamp_tags([*summaries, *attention], article_tags or {})

        cost = usage.estimated_cost
        logger.info(
            "API usage: %d calls, %d input + %d output tokens, %s",
            usage.api_calls,
            usage.input_tokens,
            usage.output_tokens,
            f"~${cost:.4f}" if cost is not None else f"cost unknown for {usage.model}",
        )

        # Rejected is the filter's verdict: what it kept is shown.
        filter_urls = {a.url for a in skim_lane}
        accepted_filter_urls = set()
        for item in filtered:
            accepted_filter_urls.update(item.urls)
        for cluster in news_clusters:
            for item in cluster.items:
                accepted_filter_urls.update(item.urls)
        rejected_urls = list(filter_urls - accepted_filter_urls)

        url_to_tags = {
            url: section.tags
            for section in news_clusters
            for item in section.items
            for url in item.urls
            if section.tags
        }

        # The section takes the same id the record persists — computed once, so the
        # rendered data-story-id and the D1 row can never drift apart.
        story_records = []
        for section in news_clusters:
            member_urls = [url for item in section.items for url in item.urls]
            section.story_id = _story_id(today, period, member_urls)
            story_records.append(
                StoryRecord(
                    id=section.story_id,
                    heading=section.heading,
                    urls=member_urls,
                )
            )

        content = DigestContent(
            date=today,
            period=period,
            sources_processed=len(source_names),
            articles_received=len(articles),
            articles_included=0,
            usage=usage,
            news_clusters=news_clusters,
            thematic_summaries=summaries,
            attention_sections=attention,
            filtered_headlines=filtered,
            fan_sections=follow_sections,
            output_language=self.output_language,
        )

        laid_out = layer_by_score(content, featured_threshold=self.featured_threshold)
        scores = None
        if preference is not None:
            by_url = {a.url: a for a in articles}
            kept_urls = dict.fromkeys(
                url
                for entry in (
                    *laid_out.filtered_headlines,
                    *(item for section in laid_out.featured_articles[1:] for item in section.items),
                    # Every member, not one per cluster: a cluster is ordered by its
                    # best member's preference, and which that is is not known yet.
                    *(item for section in laid_out.news_clusters for item in section.items),
                )
                for url in entry.urls
            )
            scores = await preference(
                [by_url[u] for u in kept_urls if u in by_url], stand_in_topics(laid_out)
            )
        content = order_by_preference(laid_out, scores)
        moves = None if scores is None else preference_moves(laid_out, content)
        # Accepted means shown in this issue. What the summarizer left out of every
        # section stays pending for the next run in the window.
        accepted_urls = list(digest_urls(content))
        # Counted as the page shows them: a topic of three reports is three articles.
        content.articles_included = len(accepted_urls)

        return ProcessResult(
            content=content,
            accepted_urls=accepted_urls,
            rejected_urls=rejected_urls,
            url_to_tags=url_to_tags,
            story_records=story_records,
            topics=stand_in_topics(content),
            preference_moves=moves,
        )
