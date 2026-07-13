"""Claude API prompt templates for content processing."""

from cyris.domain.models import Article, PreferenceProfile

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
      "title": "<headline in 繁體中文, translate if needed>",
      "summary": "<one sentence in 繁體中文>",
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
      "heading": "<thematic heading in 繁體中文>",
      "summary": "<3-5 sentence summary in 繁體中文>",
      "articles": [
        {"id": <article id>, "title": "<original title>", "source": "<source name>"}
      ]
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
    for a in articles:
        lines.append(f"[{a.id}] ({a.source_name}) {a.title}")
        # Include more content for summarize tier
        snippet = a.content[:snippet_length].replace("\n", " ").strip()
        if snippet:
            lines.append(f"    {snippet}")
        lines.append("")
    return "\n".join(lines)


def build_filter_system_prompt(preference_profile: PreferenceProfile | None = None) -> str:
    """Build filter system prompt with optional preference injection.

    Args:
        preference_profile: Optional user preference profile to inject.

    Returns:
        System prompt string for filter-tier processing.
    """
    if preference_profile is None:
        return FILTER_SYSTEM

    return f"{FILTER_SYSTEM}\n\n{preference_profile.prompt_injection}"


NEWS_CLUSTER_SYSTEM = """\
你是一個新聞編輯，負責將相關的新聞報導聚合成主題群組。

任務：
1. 識別內容相關的新聞文章（例如：同一事件的不同報導、相同主題的系列新聞）
2. 為每個群組創建一個繁體中文的主題標題
3. 撰寫整合摘要，涵蓋該群組所有文章的關鍵資訊
4. 保留差異性觀點和補充細節

規則：
- 每個群組至少需要 2 篇文章
- 單篇獨立新聞放入 unclustered_ids
- 標題要精簡具體（10 字以內）
- 摘要用繁體中文，2-3 句話說明事件核心

輸出 JSON 格式：
{
  "clusters": [
    {
      "heading": "<繁體中文主題標題>",
      "summary": "<2-3 句繁體中文整合摘要>",
      "article_ids": [<id>, <id>, ...]
    }
  ],
  "unclustered_ids": [<id>, ...]
}

如果所有文章都不相關，返回 {"clusters": [], "unclustered_ids": [所有 id]}
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


# --- Scoring prompts ---

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

Respond in JSON:
{"scores": [{"id": <article_id>, "score": <0-100>, "language": "<lang_code>"}]}
"""


def build_scoring_system_prompt(preference_profile: PreferenceProfile | None = None) -> str:
    """Build scoring system prompt with optional preference injection.

    Args:
        preference_profile: Optional user preference profile to inject.

    Returns:
        System prompt string for article scoring.
    """
    if preference_profile is None:
        return SCORING_SYSTEM

    return f"{SCORING_SYSTEM}\n\n{preference_profile.prompt_injection}"


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


TOPIC_CONFIRM_SYSTEM = """\
你是嚴格的新聞編輯。僅當文章內容與追蹤主題有實質語意關聯時才列入（嚴格排除純字面巧合）。
輸出 JSON:
{
  "matches": [
    {"id": <original_id>, "topic": "<精確主題名稱>",
     "note": "<繁體中文單句，約60字內，描述對主題的關鍵點或更新>"}
  ]
}
若無匹配則 "matches": [] 。
note 必須為繁體中文單句。
"""


def build_topic_confirm_prompt(topics: list[str], candidates: list) -> str:
    """Build user prompt for batch topic match confirmation.

    candidates items must have .original_id, .title, .source_name, .content
    """
    if not candidates:
        return ""
    lines = [
        "追蹤主題: " + ", ".join(topics),
        "",
        "判斷以下文章是否語意相關上述任一主題 (僅實質相關，非字面):",
        "",
    ]
    for c in candidates:
        oid = getattr(c, "original_id", getattr(c, "id", "?"))
        content_snip = getattr(c, "content", "")[:500]
        lines.append(f"[{oid}] {c.title}")
        lines.append(f"    Source: {c.source_name}")
        if content_snip:
            lines.append(f"    Content: {content_snip}")
        lines.append("")
    return "\n".join(lines)
