"""CSS rule receipts and deterministic render fixtures for digest templates."""

import re
from datetime import UTC, datetime
from pathlib import Path

from cyris.adapters.output.html_digest import HtmlDigestWriter
from cyris.domain.models import (
    ArticleState,
    DigestContent,
    DigestItem,
    DigestSection,
    StoredArticle,
    Tier,
    UsageStats,
)

_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_WHITESPACE = re.compile(r"\s+")

UI_SPEC = Path(__file__).resolve().parents[1] / "docs" / "design" / "ui-language.md"


RuleDeclarations = set[str] | list[str]


def _normalized_declaration(declaration: str) -> str:
    """Normalize a declaration without changing declaration ordering."""
    property_name, value = declaration.split(":", 1)
    return f"{property_name.strip()}: {_WHITESPACE.sub(' ', value).strip()}"


def _split_declarations(css: str) -> list[str]:
    """Split declarations while respecting strings and parenthesized values."""
    declarations: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    escaped = False
    for index, character in enumerate(css):
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
        elif character in {"'", '"'}:
            quote = character
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        elif character == ";" and depth == 0:
            if declaration := css[start:index].strip():
                declarations.append(_normalized_declaration(declaration))
            start = index + 1
    if declaration := css[start:].strip():
        declarations.append(_normalized_declaration(declaration))
    return declarations


def _matching_brace(css: str, opening: int) -> int:
    """Find an opening brace's mate, accounting for quoted CSS strings."""
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(opening, len(css)):
        character = css[index]
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return index
    raise ValueError("unclosed CSS rule")


def _parse_rules(css: str, prefixes: tuple[str, ...], rules: dict[str, RuleDeclarations]) -> None:
    position = 0
    while position < len(css):
        while position < len(css) and css[position].isspace():
            position += 1
        if position == len(css):
            return
        opening = css.find("{", position)
        if opening == -1:
            return
        selector = css[position:opening].strip()
        closing = _matching_brace(css, opening)
        contents = css[opening + 1 : closing]
        if selector.startswith("@"):
            _parse_rules(contents, (*prefixes, selector), rules)
        elif selector:
            key = " | ".join((*prefixes, selector))
            declarations = _split_declarations(contents)
            existing = rules.get(key)
            if existing is None:
                ordered = selector in {"body", ":root"} and not prefixes
                rules[key] = declarations if ordered else set(declarations)
            elif isinstance(existing, list):
                existing.extend(declarations)
            else:
                existing.update(declarations)
        position = closing + 1


def parse_style_block(html: str) -> dict[str, RuleDeclarations]:
    """Return normalized CSS rules from every inline ``<style>`` block in HTML.

    Rules under at-rules retain their complete ancestor path so changes to a media
    query or keyframe scope cannot masquerade as a top-level rule change.
    ``body`` alone preserves declaration order because its background shorthand
    must precede its background longhands. A selector declared more than once at
    the same path merges rather than overwrites, so no block can be dropped from
    the receipt by a later one repeating its selector.
    """
    rules: dict[str, RuleDeclarations] = {}
    for style in re.findall(r"<style\b[^>]*>(.*?)</style\s*>", html, re.DOTALL | re.IGNORECASE):
        _parse_rules(_COMMENT.sub("", style), (), rules)
    return rules


def _rules(source: str) -> dict[str, RuleDeclarations]:
    """Parse an HTML page's style blocks, or bare CSS when there are none."""
    return parse_style_block(source if "<style" in source else f"<style>{source}</style>")


def spec_token_fence(markdown: str) -> str:
    """Return the first ``css`` fence after the spec's ``## 2.`` heading."""
    section = re.search(r"^## 2\.", markdown, re.MULTILINE)
    if section is None:
        raise ValueError("the spec has no '## 2.' heading")
    fence = re.search(r"^```css\n(.*?)^```", markdown[section.end() :], re.MULTILINE | re.DOTALL)
    if fence is None:
        raise ValueError("the spec's section 2 has no css fence")
    return fence.group(1)


def root_declarations(css_source: str) -> list[str]:
    """Return ordered ``:root`` declarations from CSS or an HTML style block."""
    declarations = _rules(css_source)[":root"]
    assert isinstance(declarations, list)
    return declarations


def _item(title: str, url: str, source: str) -> DigestItem:
    return DigestItem(title=title, summary=f"{title} summary.", sources=[source], urls=[url])


def _raw_article(
    title: str, source: str, state: ArticleState, score: float | None
) -> StoredArticle:
    timestamp = datetime(2026, 1, 2, tzinfo=UTC)
    return StoredArticle(
        url=f"https://example.test/{title.lower().replace(' ', '-')}",
        original_id=title,
        title=title,
        content="Receipt fixture article.",
        published_at=timestamp,
        source_name=source,
        source_tier=Tier.FILTER,
        state=state,
        first_seen_at=timestamp,
        score=score,
    )


def receipt_fixtures() -> tuple[str, str, str]:
    """Render fixed index, complete digest, and raw-page receipt inputs.

    The digest mirrors ``test_all_sections_render`` so each digest section has a
    reachable fixture. Fixed dates and records keep snapshots stable across runs.
    """
    writer = HtmlDigestWriter("/tmp/css-receipt")
    index_html = writer.render_index(["2026-01-02-morning.html", "2026-01-01-evening.html"])
    content = DigestContent(
        date="2026-01-02",
        period="morning",
        sources_processed=5,
        articles_received=20,
        articles_included=10,
        usage=UsageStats(input_tokens=1000, output_tokens=500, api_calls=3, model="receipt"),
        featured_articles=[
            DigestSection(
                heading="Top Stories",
                items=[
                    _item("Featured Story", "https://featured.test/story", "Source A"),
                    _item("Second Feature", "https://featured.test/second", "Source B"),
                ],
            )
        ],
        news_clusters=[
            DigestSection(
                heading="Tech Industry",
                items=[_item("Cluster", "https://news.test/cluster", "News A")],
            )
        ],
        thematic_summaries=[
            DigestSection(
                heading="AI Research",
                description="Latest developments in AI",
                items=[_item("Research Paper", "https://arxiv.org/1234", "ArXiv")],
            )
        ],
        attention_sections=[
            DigestSection(
                heading="Worth Watching",
                items=[_item("Attention Item", "https://blog.test/item", "Blog")],
            )
        ],
        filtered_headlines=[
            _item("Headline 1", "https://news.test/one", "News"),
            _item("Headline 2", "https://news.test/two", "News"),
        ],
        fan_sections=[
            DigestSection(heading="Fan", items=[_item("Fan Item", "https://fan.test/item", "Fan")])
        ],
        triage_pending_count=5,
    )
    digest_html = writer.render(content)
    raw_html = writer.render_raw(
        "2026-01-02",
        "morning",
        [
            _raw_article("Accepted Article", "Source A", ArticleState.ACCEPTED, 0.9),
            _raw_article("Pending Article", "Source A", ArticleState.PENDING, None),
            _raw_article("Rejected Article", "Source B", ArticleState.REJECTED, 0.2),
        ],
    )
    return index_html, digest_html, raw_html
