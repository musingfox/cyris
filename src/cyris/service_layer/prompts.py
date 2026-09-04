"""LLM prompt templates for content processing.

User-facing output language is parameterized via the ``<output_language>``
placeholder (substituted, not str.format, to avoid clashing with the JSON
braces in the templates). An optional reader style prompt is appended the
same way.

The setting is a BCP 47 tag; ``languages.json`` maps it to the wording the model
is actually given. A tag the file does not list is substituted as-is, which is
what keeps a config holding a plain language name working.
"""

import json
from functools import cache
from importlib.resources import files

from cyris.domain.models import Article

DEFAULT_LANGUAGE = "zh-Hant"


@cache
def _language_names() -> dict[str, str]:
    raw = (files(__package__) / "languages.json").read_text(encoding="utf-8")
    return {k: v for k, v in json.loads(raw).items() if not k.startswith("_")}


def language_wording(tag: str) -> str:
    """The prompt wording for a BCP 47 tag, or the tag itself when unlisted."""
    return _language_names().get(tag, tag)


def _finalize_system(
    base: str,
    language: str = DEFAULT_LANGUAGE,
    style_prompt: str = "",
) -> str:
    """Substitute the output language and append the optional style block."""
    out = base.replace("<output_language>", language_wording(language))
    if style_prompt.strip():
        out += f"\n\nReader style — apply to tone and focus:\n{style_prompt.strip()}"
    return out


FILTER_SYSTEM = """\
You are a news editor selecting headlines for a tech-savvy professional reader. \
Most articles are noise — your job is to surface only what matters.

INCLUDE articles with at least one of these qualities:
- Industry-level shifts: M&A, major partnerships, regulatory changes, market disruptions
- Notable product launches from major companies (not minor updates or patches)
- Significant funding rounds, IPOs, or company milestones
- Geopolitical or economic events affecting tech/business
- Substantial analysis or insights that advance understanding of important topics
- Well-researched investigations or original reporting on meaningful subjects

EXCLUDE:
- Incremental updates, bug fixes, minor feature additions
- Opinion pieces, listicles, how-to guides, event recaps
- Follow-up coverage that adds no new information
- Personality profiles, hiring news, internal reorgs

Apply qualitative judgment — select based on merit and substance, not percentage targets.

Respond in JSON:
{
  "selected": [
    {
      "id": <article id>,
      "title": "<headline in <output_language>, translate if needed>",
      "summary": "<one sentence in <output_language>>",
      "source": "<source name>"
    }
  ],
  "rejected_count": <number rejected>
}

If nothing qualifies, return {"selected": [], "rejected_count": N}.
"""

SUMMARIZE_SYSTEM = """\
You are an analyst producing thematic summaries for a bilingual professional reader. \
You receive articles from quality sources, already grouped by topic tag. \
Your job is to synthesize them into a coherent thematic summary.

For each topic group:
1. Identify the key theme connecting the articles
2. Write a structured 3-5 sentence summary:
   - Sentence 1: Core argument or claim
   - Sentences 2-3: Key evidence, data, or supporting points
   - Sentences 4-5: Implications, conclusions, or context
3. Synthesize perspectives across sources: highlight agreement, divergence, \
or complementary viewpoints
4. When sources conflict, attribute specific claims to sources rather than \
stating facts without attribution

Respond in JSON format:
{
  "sections": [
    {
      "heading": "<thematic heading in <output_language>>",
      "summary": "<3-5 sentence summary in <output_language>>",
      "article_ids": [<article id>, ...]
    }
  ]
}
"""


def build_filter_prompt(articles: list[Article], snippet_length: int = 500) -> str:
    """Build user prompt for filter-tier batch processing.

    Args:
        articles: Articles to process.
        snippet_length: Maximum length of content snippet to include.
    """
    lines = []
    for a in articles:
        lines.append(f"[{a.id}] ({a.source_name}) {a.title}")
        # Include first snippet_length chars of content for context
        snippet = a.content[:snippet_length].replace("\n", " ").strip()
        if snippet:
            lines.append(f"    {snippet}")
    return "\n".join(lines)


def build_summarize_prompt(tag: str, articles: list[Article], snippet_length: int = 1000) -> str:
    """Build user prompt for summarize-tier grouped processing.

    Args:
        tag: The topic tag this group shares.
        articles: Articles in this group.
        snippet_length: Maximum length of content snippet to include.
    """
    lines = [f"Topic group: {tag}", ""]
    # Positional index as the prompt id: short ints echo back reliably, unlike
    # the URL-string ids newsletter articles carry.
    for i, a in enumerate(articles):
        lines.append(f"[{i}] ({a.source_name}) {a.title}")
        # Include more content for summarize tier
        snippet = a.content[:snippet_length].replace("\n", " ").strip()
        if snippet:
            lines.append(f"    {snippet}")
        lines.append("")
    return "\n".join(lines)


def build_filter_system_prompt(
    language: str = DEFAULT_LANGUAGE,
    style_prompt: str = "",
) -> str:
    """Build filter system prompt with output language and style."""
    return _finalize_system(FILTER_SYSTEM, language, style_prompt)


def build_summarize_system_prompt(
    language: str = DEFAULT_LANGUAGE,
    style_prompt: str = "",
) -> str:
    """Build summarize system prompt with output language and style."""
    return _finalize_system(SUMMARIZE_SYSTEM, language, style_prompt)


NEWS_CLUSTER_SYSTEM = """\
You are a news editor grouping related news reports into topic clusters.

Task:
1. Identify content-related news articles (e.g. different reports on the same event,
   a series on the same topic)
2. Create a concise, specific topic heading for each cluster
3. Assign concise topic tags to each cluster
4. Write an integrated summary covering the key information across the cluster's articles
5. Preserve divergent viewpoints and complementary details

Rules:
- Each cluster needs at least 2 articles
- Standalone single news items go into unclustered_ids
- Keep headings concise and specific
- Write every heading and summary in <output_language>; 2-3 sentences per summary

Respond in JSON:
{
  "clusters": [
    {
      "heading": "<heading in <output_language>>",
      "summary": "<2-3 sentence summary in <output_language>>",
      "article_ids": [<id>, <id>, ...],
      "tags": ["<topic tag>", ...]
    }
  ],
  "unclustered_ids": [<id>, ...]
}

If no articles are related, return {"clusters": [], "unclustered_ids": [all ids]}.
"""


def build_news_cluster_prompt(articles: list[Article]) -> str:
    """Build user prompt for news clustering.

    Args:
        articles: News articles to cluster.

    Returns:
        Formatted prompt string with article summaries.
    """
    lines = []
    for a in articles:
        lines.append(f"[{a.id}] ({a.source_name}) {a.title}")
        # Include first 500 chars of content for clustering context
        snippet = a.content[:500].replace("\n", " ").strip()
        if snippet:
            lines.append(f"    {snippet}")
        lines.append("")
    return "\n".join(lines)


def build_news_cluster_system_prompt(
    language: str = DEFAULT_LANGUAGE, style_prompt: str = ""
) -> str:
    """Build news-cluster system prompt with output language and style injection."""
    return _finalize_system(NEWS_CLUSTER_SYSTEM, language, style_prompt)


# --- Scoring prompts (no natural-language output; language-agnostic) ---

SCORING_SYSTEM = """\
You are an article relevance evaluator. Score each article on a 0-100 scale based on \
personal relevance to the reader's interests.

Scoring criteria (in priority order):
1. Topic relevance: Does this article cover topics the reader cares about?
2. Information novelty: Does it contain new information, insights, or perspectives?
3. Article quality: Is it well-researched, substantive, and actionable?

Calibration anchors:
- 90-100: Highly relevant. Directly addresses reader's core interests with novel insights.
- 70-89: Relevant. Covers topics of interest with useful information or analysis.
- 50-69: Moderately relevant. Related to reader's interests but limited novelty or depth.
- 30-49: Low relevance. Tangentially related or generic coverage of peripherally interesting topics.
- 0-29: Not relevant. Off-topic or extremely generic content with no clear connection to interests.

For each article, also detect the primary language: "zh" for Chinese, "en" for English, \
or the appropriate ISO 639-1 code for other languages.
For each article, also assign concise topic tags.

Respond in JSON:
{"scores": [{"id": <article_id>, "score": <0-100>, "language": "<lang_code>", \
"tags": ["<topic tag>", ...]}]}
"""


def build_scoring_system_prompt() -> str:
    """Build the scoring system prompt."""
    return SCORING_SYSTEM


def build_scoring_prompt(articles: list, snippet_length: int = 1000) -> str:
    """Build user prompt for batch article scoring.

    Args:
        articles: List of StoredArticle objects to score.
        snippet_length: Maximum length of content snippet to include.

    Returns:
        Formatted prompt with article IDs, titles, and content snippets.
    """
    if not articles:
        return ""

    lines = ["Score the following articles:\n"]
    for article in articles:
        content_snippet = article.content[:snippet_length] if article.content else ""
        lines.append(f"[{article.original_id}] {article.title}")
        lines.append(f"    Source: {article.source_name}")
        if content_snippet:
            lines.append(f"    Content: {content_snippet}")
        lines.append("")

    return "\n".join(lines)
