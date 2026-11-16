"""An issue's own pages and messages, and the pages about the deployment, worded from a catalog.

ADR-0027: the digest page, the raw page, the mail and the Discord message of one issue are
worded in the issue's `output_language`, the current setting when it stored none; the archive
and `/interest` in the current setting. Each surface renders here in every catalog, and a
zh-Hant render, its content Chinese too, holds no English word outside the product's own names.
"""

import json
import re
from datetime import UTC, datetime
from html.parser import HTMLParser

import pytest
from jinja2.utils import htmlsafe_json_dumps
from markupsafe import escape

from cyris.adapters.output.html_digest import HtmlDigestWriter
from cyris.domain.interest_graph import InterestEdge, InterestGraph, InterestNode
from cyris.domain.models import (
    ArticleState,
    DigestContent,
    DigestItem,
    DigestSection,
    Lane,
    StoredArticle,
    UsageStats,
)
from cyris.i18n import translator

pytestmark = pytest.mark.unit

LANGUAGES = ["en", "zh-Hant"]
# Words a Chinese page still says in Latin letters: the product's and its parts' names, the
# command a reader types, and the protocols and settings it names as they are written.
NAMES = {
    "CYRIS",
    "Cyris",
    "cyris",
    "run",
    "Signal",
    "Noise",
    "token",
    "API",
    "LLM",
    "HTTP",
    "RSS",
    "feed",
}


def _content(language: str, **overrides) -> DigestContent:
    """An issue with every layer, its content in `language` as the summarize step writes it."""
    zh = language != "en"

    def word(en: str, chinese: str) -> str:
        return chinese if zh else en

    def item(title: str, url: str, source: str) -> DigestItem:
        return DigestItem(title=title, summary=f"{title}。", sources=[source], urls=[url])

    fields = {
        "date": "2026-01-02",
        "period": "morning",
        "sources_processed": 5,
        "articles_received": 20,
        "articles_included": 10,
        "usage": UsageStats(input_tokens=1000, output_tokens=500, api_calls=3, model="m"),
        "featured_articles": [
            DigestSection(
                heading=word("Top", "頭條"),
                items=[item(word("Featured", "重點報導"), "https://a.test/1", word("A", "甲"))],
            )
        ],
        "news_clusters": [
            DigestSection(
                heading=word("Chips", "晶片"),
                items=[
                    DigestItem(
                        title=word("Chips", "晶片"),
                        summary=word("Chips.", "晶片。"),
                        sources=[word("A", "甲"), word("B", "乙")],
                        urls=["https://n.test/a", "https://n.test/b"],
                    )
                ],
                members=[
                    item(word("Report A", "報告甲"), "https://n.test/a", word("A", "甲")),
                    item(word("Report B", "報告乙"), "https://n.test/b", word("B", "乙")),
                ],
                story_id="2026-01-02-morning-chips",
            ),
            DigestSection(
                heading=word("Lone", "單篇"),
                items=[item(word("Lone report", "單篇報告"), "https://n.test/c", word("A", "甲"))],
            ),
        ],
        "filtered_headlines": [item(word("Headline", "快訊一"), "https://h.test/1", "甲")],
        "tracked_topics": [
            DigestSection(
                heading=word("Tracked", "追蹤主題"),
                items=[item(word("Hit", "命中"), "https://t.test/1", word("C", "丙"))],
            )
        ],
        "triage_pending_count": 5,
        "output_language": language,
    }
    return DigestContent(**(fields | overrides))


def _article(title: str, source: str, state: ArticleState) -> StoredArticle:
    at = datetime(2026, 1, 2, tzinfo=UTC)
    return StoredArticle(
        url=f"https://r.test/{len(title)}-{state}",
        original_id=title,
        title=title,
        content="x",
        published_at=at,
        source_name=source,
        source_lane=Lane.SKIM,
        state=state,
        first_seen_at=at,
        score=0.5,
    )


class _Words(HTMLParser):
    """What a reader reads or hears: text outside scripts and styles, and the worded attributes."""

    WORDED = {"title", "aria-label", "alt"}

    def __init__(self) -> None:
        super().__init__()
        self.runs: list[str] = []
        self.inside: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:
        values = dict(attrs)
        self.runs += [value for name, value in attrs if name in self.WORDED and value]
        if tag == "meta" and values.get("name") == "description":
            self.runs.append(values["content"])
        if words := values.get("data-layer-words"):
            self.runs += [re.sub(r"<[^>]+>", "", v) for v in json.loads(words).values()]
        if tag in ("script", "style"):
            self.inside.append(tag)

    def handle_endtag(self, tag) -> None:
        if self.inside and tag == self.inside[-1]:
            self.inside.pop()

    def handle_data(self, data) -> None:
        if not self.inside and data.strip():
            self.runs.append(data.strip())


def _english_words(html: str) -> set[str]:
    """Each Latin-letter word a reader meets on the page that is not one of `NAMES`.

    An ISO 8601 time's `T` is data, not a word.
    """
    parser = _Words()
    parser.feed(html)
    runs = [re.sub(r"(?<=\d)T(?=\d)", " ", run) for run in parser.runs]
    return {w for run in runs for w in re.findall(r"[A-Za-z]+", run)} - NAMES


def _html_lang(html: str) -> str:
    return re.search(r'<html lang="([^"]*)">', html)[1]


def _shows(html: str, text: str) -> bool:
    """`text` is on the page as text, in an attribute, or as a script's JSON string."""
    return str(escape(text)) in html or htmlsafe_json_dumps(text) in html


def _digest(language: str, tmp_path, **overrides) -> str:
    return HtmlDigestWriter(tmp_path).render(_content(language, **overrides), raw_page=True)


# ---- the digest page --------------------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_digest_page_is_worded_in_its_issues_language(language, tmp_path) -> None:
    t = translator(language)

    html = _digest(language, tmp_path, triage_pending_count=5)

    assert _html_lang(html) == language
    for text in [
        t("site.archive"),
        t("site.sign_in"),
        t("issue.digest"),
        t("issue.all_articles"),
        t("issue_period.morning"),
        t("digest.stat_sources"),
        t("digest.stat_received"),
        t("digest.stat_included"),
        t("digest.stat_triage"),
        t("digest.awaiting_triage", count=5),
        t("layer.tracking"),
        t("layer.stories"),
        t("layer.topics"),
        t("layer.headlines"),
        t("digest.close"),
        t("digest.topic_articles", count=2),
        t("digest.pull_to_close"),
        t("vote.more"),
        t("vote.less"),
        t("vote.signed_out"),
        t("vote.unanswered"),
        t(
            "digest.description",
            date="2026-01-02",
            period=t("issue_period.morning"),
            included=10,
            received=20,
        ),
    ]:
        assert _shows(html, text), text


def test_the_digest_pages_description_names_the_period_not_its_identifier(tmp_path) -> None:
    html = _digest("zh-Hant", tmp_path)

    description = re.search(r'<meta name="description" content="([^"]*)">', html)[1]
    assert "早報" in description
    assert "morning" not in description


def test_the_digest_pages_footer_links_the_project_inside_its_sentence(tmp_path) -> None:
    html = _digest("zh-Hant", tmp_path)

    assert (
        '由 <a href="https://cyris.musingfox.com/">Cyris</a> 產生 · 1,500 個 token · 3 次 API 呼叫'
        in html
    )


def test_a_zh_hant_digest_page_says_nothing_in_english(tmp_path) -> None:
    assert _english_words(_digest("zh-Hant", tmp_path)) == set()


def test_the_english_check_finds_the_chrome_of_an_english_page(tmp_path) -> None:
    assert {"Stories", "Topics", "Close", "Archive"} <= _english_words(_digest("en", tmp_path))


def test_each_layer_carries_its_words_for_the_layer_script(tmp_path) -> None:
    html = _digest("zh-Hant", tmp_path)

    words = [json.loads(w) for w in re.findall(r"data-layer-words='([^']*)'", html)]
    assert [w["count"] for w in words] == ["1 篇報導", "1 個主題", "2 則快訊"]
    assert words[1]["skip"] == '<span class="skip-words">跳到 </span>1 個主題 ↓'
    assert words[0]["back"] == "回到報導"


@pytest.mark.parametrize("language", LANGUAGES)
def test_an_empty_issue_says_so_and_links_its_raw_page_inside_the_sentence(
    language, tmp_path
) -> None:
    t = translator(language)
    content = _content(
        language,
        articles_included=0,
        featured_articles=[],
        news_clusters=[],
        filtered_headlines=[],
        tracked_topics=[],
    )

    html = HtmlDigestWriter(tmp_path).render(content, raw_page=True)
    alone = HtmlDigestWriter(tmp_path).render(content, raw_page=False)

    link = {"link": ("a", {"href": "2026-01-02-morning-raw.html"})}
    assert t("issue.empty_raw", received=20, elements=link) in html
    assert str(escape(t("issue.empty", received=20))) in alone


@pytest.mark.parametrize("language", LANGUAGES)
def test_a_degraded_issue_is_noted_in_its_language(language, tmp_path) -> None:
    html = HtmlDigestWriter(tmp_path).render(_content(language), degraded=True)

    assert str(escape(translator(language)("issue.degraded"))) in html


def test_an_issue_stored_with_no_language_is_worded_in_the_current_setting(tmp_path) -> None:
    html = HtmlDigestWriter(tmp_path, language="zh-Hant").render(_content("en", output_language=""))

    assert _html_lang(html) == "zh-Hant"
    assert "報導" in html


def test_a_rerender_under_another_setting_keeps_the_issues_language(tmp_path) -> None:
    html = HtmlDigestWriter(tmp_path, language="en").render(_content("zh-Hant"))

    assert _html_lang(html) == "zh-Hant"
    assert _english_words(html) == set()


# ---- the raw page ----------------------------------------------------------------------


def _raw(language: str, tmp_path, current: str = "en") -> str:
    zh = language != "en"
    articles = [
        _article("收錄的文章" if zh else "Kept", "來源甲" if zh else "A", ArticleState.ACCEPTED),
        _article("待定的文章" if zh else "Held", "來源甲" if zh else "A", ArticleState.PENDING),
        _article("略過的文章" if zh else "Cut", "來源乙" if zh else "B", ArticleState.REJECTED),
    ]
    return HtmlDigestWriter(tmp_path, language=current).render_raw(
        "2026-01-02", "evening", articles, language
    )


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_raw_page_is_worded_in_its_issues_language(language, tmp_path) -> None:
    t = translator(language)

    html = _raw(language, tmp_path)

    assert _html_lang(html) == language
    for text in [
        t("raw.title"),
        t("raw.description", date="2026-01-02", period=t("issue_period.evening")),
        t("raw.label", articles=3, sources=2),
        t("raw.intro"),
        t("raw.views"),
        t("raw.view_list"),
        t("raw.view_triage"),
        t("raw.up"),
        t("raw.down"),
        t("raw.hint"),
        t("article_state.accepted"),
        t("article_state.pending"),
        t("article_state.rejected"),
        t("vote.more"),
        t("raw.remaining", count="%COUNT%"),
        t("vote.refused", status="%STATUS%"),
    ]:
        assert _shows(html, text), text
    assert t("raw.heading", elements={"em": ("em", {})}) in html


def test_a_zh_hant_raw_page_says_nothing_in_english(tmp_path) -> None:
    assert _english_words(_raw("zh-Hant", tmp_path)) == set()


def test_a_raw_page_stored_with_no_language_is_worded_in_the_current_setting(tmp_path) -> None:
    assert _html_lang(_raw("", tmp_path, current="zh-Hant")) == "zh-Hant"


@pytest.mark.parametrize(
    ("articles", "sources", "said"),
    [
        (1, 1, "All articles · 1 article · 1 source"),
        (3, 2, "All articles · 3 articles · 2 sources"),
    ],
)
def test_the_raw_pages_label_counts_each_noun_by_its_own_plural(articles, sources, said) -> None:
    assert translator("en")("raw.label", articles=articles, sources=sources) == said


# ---- the archive -----------------------------------------------------------------------


def _index(current: str, tmp_path, content: DigestContent | None = None) -> str:
    names = ["2026-01-02-morning.html", "2026-01-02-morning-raw.html", "2026-01-01-evening.html"]
    return HtmlDigestWriter(tmp_path, language=current).render_index(
        names, content=content, counts={("2026-01-01", "evening"): 4}
    )


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_archive_is_worded_in_the_current_setting(language, tmp_path) -> None:
    t = translator(language)

    html = _index(language, tmp_path, _content(language))

    assert _html_lang(html) == language
    for text in [
        t("index.description"),
        t("index.intro"),
        t("index.latest"),
        t("index.articles", count=10),
        t("index.articles", count=4),
        t("index.issues", count=1),
        t("index.issues", count=2),
        t("issue.digest"),
        t("issue.all_articles"),
        t("issue_period.evening"),
    ]:
        assert _shows(html, text), text
    assert t("index.heading", elements={"em": ("em", {})}) in html
    assert t("index.interest", elements={"link": ("a", {"href": "/interest"})}) in html


def test_the_archive_follows_the_setting_not_the_issue_it_leads_with(tmp_path) -> None:
    html = _index("en", tmp_path, _content("zh-Hant"))

    assert _html_lang(html) == "en"
    assert "Latest" in html


def test_a_zh_hant_archive_says_nothing_in_english(tmp_path) -> None:
    assert _english_words(_index("zh-Hant", tmp_path, _content("zh-Hant"))) == set()


@pytest.mark.parametrize("language", LANGUAGES)
def test_an_empty_archive_names_the_command_inside_its_sentence(language, tmp_path) -> None:
    html = HtmlDigestWriter(tmp_path, language=language).render_index([])

    code = {"code": ("code", {"class": "data"})}
    assert translator(language)("index.empty", elements=code) in html


# ---- the interest map ------------------------------------------------------------------


def _interest(language: str, tmp_path, *, lit: float = 1.0, holds: bool = True) -> str:
    nodes = [InterestNode("代理", 2, 0, "up", lit), InterestNode("晶片", 0, 2, "down", 0.0)]
    graph = InterestGraph(nodes=nodes, edges=[InterestEdge("代理", "晶片")])
    return HtmlDigestWriter(tmp_path, language=language).render_interest(
        graph, holds_active=holds, generated_at="2026-10-17T08:00:00+00:00", clock_days=7
    )


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_interest_map_is_worded_in_the_current_setting(language, tmp_path) -> None:
    t = translator(language)

    html = _interest(language, tmp_path)

    assert _html_lang(html) == language
    for text in [
        t("interest.map"),
        t("interest.description"),
        t("interest.label"),
        t("interest.intro"),
        t("interest.legend"),
        t("interest.tags"),
        t("interest.col_tag"),
        t("interest.col_up"),
        t("interest.col_down"),
        t("interest.col_polarity"),
        t("interest.col_days_left"),
        t("polarity.up"),
        t("polarity.down"),
        t("interest.as_of", time="2026-10-17T08:00:00+00:00"),
    ]:
        assert _shows(html, text), text
    assert t("interest.heading", elements={"em": ("em", {})}) in html


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_interest_maps_notes_and_empty_state_are_worded(language, tmp_path) -> None:
    t = translator(language)
    code = {"code": ("code", {"class": "data"})}
    empty = HtmlDigestWriter(tmp_path, language=language).render_interest(
        InterestGraph(nodes=[], edges=[]), holds_active=False, generated_at="", clock_days=7
    )

    assert str(escape(t("interest.holds_elsewhere"))) in _interest(language, tmp_path, lit=0.0)
    assert str(escape(t("interest.no_holds"))) in _interest(
        language, tmp_path, lit=0.0, holds=False
    )
    assert t("interest.empty", elements=code) in empty


def test_a_zh_hant_interest_map_says_nothing_in_english(tmp_path) -> None:
    assert _english_words(_interest("zh-Hant", tmp_path)) == set()
