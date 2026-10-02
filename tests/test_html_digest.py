"""Tests for HTML digest rendering."""

import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

import pytest
from css_rules import parse_style_block, receipt_fixtures, text_langs

from cyris.adapters.output import html_digest
from cyris.adapters.output.html_digest import PROJECT_URL, HtmlDigestWriter
from cyris.domain.models import (
    NO_LLM_MODEL,
    ArticleState,
    DigestContent,
    DigestItem,
    DigestSection,
    StoredArticle,
    Tier,
    UsageStats,
)

pytestmark = pytest.mark.unit

CREDIT = f'// <a href="{PROJECT_URL}">cyris</a>'


def test_render_with_featured_article(sample_digest_content):
    """C1 Test 1: Featured article renders with DOCTYPE, title, date, styles."""
    # Modify sample to have a featured section with score
    sample_digest_content.featured_articles = [
        DigestSection(
            heading="Featured",
            items=[
                DigestItem(
                    title="GPT-5 Released",
                    summary="OpenAI announced GPT-5 today.",
                    sources=["TechCrunch"],
                    urls=["https://tc.com/gpt5"],
                    score=9.2,
                )
            ],
        )
    ]

    writer = HtmlDigestWriter(Path("/tmp/html-test"))
    html = writer.render(sample_digest_content)

    assert "<!DOCTYPE html>" in html
    assert "GPT-5 Released" in html
    assert "2026-03-16" in html
    assert "<style>" in html


def test_render_escapes_feed_controlled_fields(sample_digest_content):
    """Feed-supplied titles and URLs must not be able to add attributes or tags.

    The templates are named *.html.j2, so select_autoescape's extension match left
    them unescaped until `default=True` was added.
    """
    from html.parser import HTMLParser

    sample_digest_content.featured_articles = [
        DigestSection(
            heading="Featured",
            items=[
                DigestItem(
                    title="<img src=x onerror=alert(1)>",
                    summary="S",
                    sources=["Src"],
                    # Both quote styles: href is double-quoted, data-urls single-quoted.
                    urls=["https://evil.test/?q=1\" onclick=\"alert(2)&x=' onmouseover='alert(3)"],
                )
            ],
        )
    ]

    html = HtmlDigestWriter(Path("/tmp/html-escape-test")).render(sample_digest_content)

    injected: list[str] = []

    class Collector(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag == "img":
                injected.append("img")
            injected.extend(k for k, _ in attrs if k.startswith("on"))

    Collector().feed(html)
    assert injected == []


def test_render_empty_sections(tmp_path):
    """C1 Test 2: Empty digest renders valid HTML without sections."""
    content = DigestContent(
        date="2026-03-16",
        period="morning",
        sources_processed=0,
        articles_received=0,
        articles_included=0,
        usage=UsageStats(input_tokens=0, output_tokens=0, api_calls=0, model="claude-sonnet-4-6"),
    )

    writer = HtmlDigestWriter(tmp_path)
    html = writer.render(content)

    assert "<!DOCTYPE html>" in html
    assert "<style>" in html
    # Should not contain section headings or content when empty
    for label in SECTION_LABELS:
        assert label not in html


SECTION_LABELS = ("Top story", "Features", "In Focus", "Following", "On the Radar", "The Wire")

EMPTY_SENTENCE = "this run judged the 7 articles it received and kept none"
DEGRADED_NOTICE = '<p class="notice err issue-note">Some or all of this issue is plain excerpts'


def _body(html: str) -> str:
    return html[html.index("<main>") : html.index("</main>")]


def _issue(articles_included: int = 0) -> DigestContent:
    return DigestContent(
        date="2026-03-16",
        period="morning",
        sources_processed=2,
        articles_received=7,
        articles_included=articles_included,
        usage=UsageStats(model=NO_LLM_MODEL),
    )


def test_an_empty_issue_says_why_and_links_all_articles(tmp_path):
    body = _body(HtmlDigestWriter(tmp_path).render(_issue(), raw_page=True))

    assert EMPTY_SENTENCE in body
    assert '<a href="2026-03-16-morning-raw.html">All articles</a>' in body


def test_an_empty_issue_without_a_raw_page_links_nothing(tmp_path):
    body = _body(HtmlDigestWriter(tmp_path).render(_issue(), raw_page=False))

    assert EMPTY_SENTENCE in body
    assert "<a " not in body


def test_an_issue_with_articles_has_no_empty_sentence(tmp_path):
    body = _body(HtmlDigestWriter(tmp_path).render(_issue(articles_included=3), raw_page=True))

    assert "kept none" not in body


@pytest.mark.parametrize("degraded", [True, False])
def test_only_a_degraded_issue_opens_with_the_warn_notice(tmp_path, degraded):
    html = HtmlDigestWriter(tmp_path).render(_issue(articles_included=3), degraded=degraded)

    assert (DEGRADED_NOTICE in _body(html)) is degraded


class _MainText(HTMLParser):
    """Every stripped text node inside <main>, and the heading levels there in order."""

    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []
        self.levels: list[int] = []
        self._inside = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "main":
            self._inside = True
        elif self._inside and re.fullmatch(r"h[1-6]", tag):
            self.levels.append(int(tag[1]))

    def handle_endtag(self, tag: str) -> None:
        if tag == "main":
            self._inside = False

    def handle_data(self, data: str) -> None:
        if self._inside and data.strip():
            self.texts.append(data.strip())


def _main_text(html: str) -> _MainText:
    parser = _MainText()
    parser.feed(html)
    return parser


@pytest.mark.parametrize("label", SECTION_LABELS)
def test_each_section_names_itself_once(label):
    assert _main_text(receipt_fixtures()[1]).texts.count(label) == 1


def test_each_body_section_is_headed_by_its_tag_alone():
    digest = receipt_fixtures()[1]
    assert digest.count('<h2 class="section-tag"><span class="label">') == 5
    assert 'class="id"' not in digest
    assert "section-heading" not in digest


def test_the_heading_outline_skips_no_level():
    levels = _main_text(receipt_fixtures()[1]).levels
    assert levels
    previous = 1  # the masthead's issue title
    for level in levels:
        assert level <= previous + 1, levels
        previous = level
    assert max(levels) <= 4


def test_clusters_are_headed_at_h3_and_radar_items_at_h4():
    digest = receipt_fixtures()[1]
    assert re.search(r'<div class="news-cluster" lang="[^"]*">\s*<h3', digest)
    assert re.search(r'<article class="attention-item">\s*<h4', digest)
    assert "<h5" not in digest


def test_every_body_title_carries_the_title_class_and_block_titles_the_large_one():
    digest = receipt_fixtures()[1]
    main = digest[digest.index("<main>") : digest.index("</main>")]
    h3s = re.findall(r"<h3\b[^>]*>", main)
    h4s = re.findall(r"<h4\b[^>]*>", main)
    # Two featured cards, one cluster, the Following block and the radar block.
    assert h3s == ['<h3 class="item-title lg">'] * 5
    # The Following article and the radar item.
    assert h4s == ['<h4 class="item-title">'] * 2


def test_render_optional_score(tmp_path):
    """C1 Test 3: DigestItem with score=None renders without error."""
    content = DigestContent(
        date="2026-03-16",
        period="morning",
        sources_processed=1,
        articles_received=1,
        articles_included=1,
        usage=UsageStats(
            input_tokens=100, output_tokens=50, api_calls=1, model="claude-sonnet-4-6"
        ),
        featured_articles=[
            DigestSection(
                heading="Featured",
                items=[
                    DigestItem(
                        title="No Score Article",
                        summary="This article has no score.",
                        sources=["Test"],
                        urls=["https://test.com"],
                        score=None,  # Explicitly None
                    )
                ],
            )
        ],
    )

    writer = HtmlDigestWriter(tmp_path)
    html = writer.render(content)

    assert "<!DOCTYPE html>" in html
    assert "No Score Article" in html
    # Should not display score when None
    assert 'class="pill score"' not in html


def test_write_creates_file(tmp_path, sample_digest_content):
    """C2 Test 1: write(dry_run=False) creates file and index."""
    writer = HtmlDigestWriter(tmp_path)
    path = writer.write(sample_digest_content, dry_run=False)

    expected_path = tmp_path / "2026-03-16-morning.html"
    assert path == expected_path
    assert path.exists()

    content = path.read_text()
    assert content.startswith("<!DOCTYPE html>")

    # Index should also exist
    index_path = tmp_path / "index.html"
    assert index_path.exists()


def test_write_puts_the_favicon_beside_the_pages(tmp_path, sample_digest_content):
    HtmlDigestWriter(tmp_path).write(sample_digest_content)

    assert (tmp_path / "favicon.svg").read_bytes() == html_digest.FAVICON.read_bytes()


def test_write_dry_run(tmp_path, sample_digest_content, capsys):
    """C2 Test 2: write(dry_run=True) prints but does not create file."""
    writer = HtmlDigestWriter(tmp_path)
    path = writer.write(sample_digest_content, dry_run=True)

    expected_path = tmp_path / "2026-03-16-morning.html"
    assert path == expected_path
    assert not path.exists()  # File should NOT exist

    # Check stdout was used
    captured = capsys.readouterr()
    assert "<!DOCTYPE html>" in captured.out


def test_render_index_with_digests(tmp_path):
    """C3 Test 1: Index with multiple digest files shows links in desc order."""
    # Create digest files
    (tmp_path / "2026-04-15-morning.html").write_text("<html>test</html>")
    (tmp_path / "2026-04-14-evening.html").write_text("<html>test</html>")
    (tmp_path / "2026-04-14-morning.html").write_text("<html>test</html>")

    writer = HtmlDigestWriter(tmp_path)
    html = writer.render_index([f.name for f in tmp_path.iterdir() if f.is_file()])

    assert "<!DOCTYPE html>" in html
    # One Digest entry per issue
    assert html.count(">Digest</a>") == 3
    # First link should be most recent (2026-04-15-morning.html)
    assert html.index("2026-04-15-morning.html") < html.index("2026-04-14-evening.html")


def test_render_index_empty(tmp_path):
    """C3 Test 2: Empty directory renders valid HTML with no links."""
    writer = HtmlDigestWriter(tmp_path)
    html = writer.render_index([f.name for f in tmp_path.iterdir() if f.is_file()])

    assert "<!DOCTYPE html>" in html
    assert ">Digest</a>" not in html
    assert not re.search(r'href="\d{4}-', html)
    assert "No digests yet" in html
    assert "<code>cyris run</code>" in html
    assert "cyris digest" not in html


def test_render_index_ignores_non_digests(tmp_path):
    """C3 Test 3: Index ignores non-digest files."""
    (tmp_path / "2026-04-15-morning.html").write_text("<html>test</html>")
    (tmp_path / "index.html").write_text("<html>index</html>")
    (tmp_path / "random.txt").write_text("random")
    (tmp_path / "not-a-digest.html").write_text("<html>other</html>")

    writer = HtmlDigestWriter(tmp_path)
    html = writer.render_index([f.name for f in tmp_path.iterdir() if f.is_file()])

    assert "<!DOCTYPE html>" in html
    # Should only have one entry (2026-04-15-morning.html)
    assert html.count(">Digest</a>") == 1
    assert "2026-04-15-morning.html" in html


def _issue_order(html: str, names: list[str]) -> list[str]:
    """The given digest pages in the order the archive first links them."""
    return sorted(names, key=lambda name: html.index(f'href="{name}"'))


def test_a_days_later_issue_is_listed_above_its_earlier_one(tmp_path):
    names = ["2026-04-15-morning.html", "2026-04-15-evening.html", "2026-04-14-evening.html"]

    html = HtmlDigestWriter(tmp_path).render_index(names)

    assert _issue_order(html, names) == [
        "2026-04-15-evening.html",
        "2026-04-15-morning.html",
        "2026-04-14-evening.html",
    ]


def test_the_same_day_order_is_read_from_the_period_order(tmp_path):
    names = ["2026-04-15-morning.html", "2026-04-15-evening.html"]

    html = HtmlDigestWriter(tmp_path).render_index(names, period_order=("evening", "morning"))

    assert _issue_order(html, names) == ["2026-04-15-morning.html", "2026-04-15-evening.html"]


def test_any_number_of_issues_a_day_follows_the_period_order(tmp_path):
    names = ["2026-04-15-dawn.html", "2026-04-15-noon.html", "2026-04-15-dusk.html"]

    html = HtmlDigestWriter(tmp_path).render_index(names, period_order=("dawn", "noon", "dusk"))

    assert _issue_order(html, names) == [
        "2026-04-15-dusk.html",
        "2026-04-15-noon.html",
        "2026-04-15-dawn.html",
    ]


def test_a_label_outside_the_period_order_follows_the_known_ones_of_its_day(tmp_path):
    names = [
        "2026-04-15-alpha.html",
        "2026-04-15-morning.html",
        "2026-04-15-zeta.html",
        "2026-04-15-evening.html",
    ]

    html = HtmlDigestWriter(tmp_path).render_index(names)

    assert _issue_order(html, names) == [
        "2026-04-15-evening.html",
        "2026-04-15-morning.html",
        "2026-04-15-zeta.html",
        "2026-04-15-alpha.html",
    ]


def _tight(html: str) -> str:
    """The markup with the whitespace between tags removed."""
    return re.sub(r">\s+<", "><", html)


def test_a_months_issues_share_one_panel_headed_by_the_month_and_its_count(tmp_path):
    names = [
        "2026-09-02-morning.html",
        "2026-08-31-evening.html",
        "2026-08-31-morning.html",
        "2026-08-30-morning.html",
    ]

    html = _tight(HtmlDigestWriter(tmp_path).render_index(names))

    assert '<span class="data">2026-08</span><span class="label">3 issues</span>' in html


def test_the_newer_month_panel_comes_first(tmp_path):
    names = [
        "2026-08-02-morning.html",
        "2026-08-01-morning.html",
        "2026-07-31-evening.html",
        "2026-07-30-evening.html",
    ]

    html = _tight(HtmlDigestWriter(tmp_path).render_index(names))

    assert html.index('<span class="data">2026-08</span>') < html.index(
        '<span class="data">2026-07</span>'
    )
    assert '<span class="data">2026-07</span><span class="label">2 issues</span>' in html


def test_an_archive_row_holds_date_period_and_the_issues_two_views(tmp_path):
    names = ["2026-08-31-morning.html", "2026-08-31-morning-raw.html", "2026-09-02-morning.html"]

    html = _tight(HtmlDigestWriter(tmp_path).render_index(names))

    assert (
        '<div class="list-row archive-row"><span class="date">2026-08-31</span>'
        '<span class="label">morning</span><span class="actions">'
        '<a class="btn secondary sm" href="2026-08-31-morning.html">Digest</a>'
        '<a class="btn secondary sm" href="2026-08-31-morning-raw.html">All articles</a>'
        "</span></div>"
    ) in html
    assert "digest-list" not in html
    assert "digest-item" not in html


def test_an_empty_archive_has_no_panel(tmp_path):
    html = HtmlDigestWriter(tmp_path).render_index([])

    assert "No digests yet" in html
    assert "<code>cyris run</code>" in html
    assert 'class="panel"' not in html
    assert f"{CREDIT} &middot; 0 issues<" in html


def _content(date: str, period: str, **fields) -> DigestContent:
    return DigestContent(
        date=date,
        period=period,
        sources_processed=1,
        articles_received=1,
        articles_included=fields.pop("articles_included", 1),
        usage=UsageStats(),
        **fields,
    )


def _card(html: str) -> str:
    """The headline card's markup, whitespace between tags removed."""
    match = re.search(r'<article class="front-card">.*?</article>', _tight(html), re.DOTALL)
    assert match, "no headline card"
    return match.group(0)


def _card_line(date: str, period: str) -> str:
    return (
        f'<div class="line"><span class="label latest">Latest</span>'
        f'<span class="date">{date}</span><span class="label">{period}</span></div>'
    )


SAME_DAY = ["2026-04-15-morning.html", "2026-04-15-evening.html", "2026-04-15-evening-raw.html"]


def test_the_newest_issue_is_the_headline_card_not_a_row(tmp_path):
    html = HtmlDigestWriter(tmp_path).render_index(SAME_DAY)

    card = _card(html)
    assert _card_line("2026-04-15", "evening") in card
    assert 'href="2026-04-15-evening-raw.html">All articles</a>' in card
    assert html.count('href="2026-04-15-evening.html"') == 1
    tight = _tight(html)
    assert '<span class="data">2026-04</span><span class="label">1 issue</span>' in tight
    panel = tight[tight.index('<section class="panel">') :]
    assert '<span class="date">2026-04-15</span><span class="label">morning</span>' in panel
    assert 'href="2026-04-15-evening.html"' not in panel


def test_this_runs_issue_is_the_headline_card_when_the_renderer_is_told(tmp_path):
    html = HtmlDigestWriter(tmp_path).render_index(
        SAME_DAY, content=_content("2026-04-15", "morning")
    )

    assert _card_line("2026-04-15", "morning") in _card(html)
    panel = _tight(html)[_tight(html).index('<section class="panel">') :]
    assert '<span class="label">evening</span>' in panel
    assert 'href="2026-04-15-morning.html"' not in panel


def test_a_run_whose_issue_is_not_listed_leaves_the_newest_on_the_card(tmp_path):
    html = HtmlDigestWriter(tmp_path).render_index(
        SAME_DAY, content=_content("2026-04-16", "evening")
    )

    assert _card_line("2026-04-15", "evening") in _card(html)


def test_a_single_issue_is_the_card_with_no_panel_below_it(tmp_path):
    html = HtmlDigestWriter(tmp_path).render_index(["2026-04-15-evening.html"])

    assert _card_line("2026-04-15", "evening") in _card(html)
    assert 'class="panel"' not in html
    assert f"{CREDIT} &middot; 1 issue<" in html


def test_an_empty_archive_has_no_headline_card(tmp_path):
    assert 'class="front-card"' not in HtmlDigestWriter(tmp_path).render_index([])


def test_the_footer_counts_the_card_issue_too(tmp_path):
    names = ["2026-04-15-evening.html", "2026-04-15-morning.html", "2026-04-14-evening.html"]

    html = HtmlDigestWriter(tmp_path).render_index(names)

    assert f"{CREDIT} &middot; 3 issues<" in html


def test_no_archive_link_opens_inside_another(tmp_path):
    names = [*SAME_DAY, "2026-04-14-evening.html", "2026-04-14-evening-raw.html"]

    html = HtmlDigestWriter(tmp_path).render_index(names, content=_content("2026-04-15", "evening"))

    depth = 0
    for tag in re.findall(r"<a\b|</a>", html):
        depth += 1 if tag == "<a" else -1
        assert depth in (0, 1)
    assert depth == 0


def _items(*titles: str, urls: int = 1) -> list[DigestItem]:
    return [
        DigestItem(
            title=title,
            summary="S",
            sources=["Src"],
            urls=[f"https://example.test/{title}/{n}" for n in range(urls)],
        )
        for title in titles
    ]


def _card_for(tmp_path, content: DigestContent | None) -> str:
    """The headline card of an archive holding one 2026-04-15 evening issue."""
    names = ["2026-04-15-evening.html", "2026-04-14-evening.html"]
    return _card(HtmlDigestWriter(tmp_path).render_index(names, content=content))


def _lead_card(html: str) -> str:
    """A digest's lead card, from its opening tag to its close."""
    return html[html.index('<article class="lead-story"') :].split("</article>", 1)[0]


def _lead_card_of(tmp_path, **extra) -> str:
    """The lead card of a digest whose one feature is Lead, linked to https://a.test."""
    lead = DigestItem(title="Lead", summary="s", sources=["S"], urls=["https://a.test"], **extra)
    content = _content(
        "2026-04-15", "evening", featured_articles=[DigestSection(heading="F", items=[lead])]
    )
    return _lead_card(HtmlDigestWriter(tmp_path).render(content))


def test_the_card_carries_the_story_the_digest_leads_with(tmp_path):
    content = _content(
        "2026-04-15",
        "evening",
        featured_articles=[
            DigestSection(heading="F", items=_items("Cloudflare Containers GA", "Second"))
        ],
        thematic_summaries=[DigestSection(heading="T", items=_items("Thematic"))],
    )

    assert '<h2 lang="">Cloudflare Containers GA</h2>' in _card_for(tmp_path, content)
    assert "Cloudflare Containers GA" in _lead_card(HtmlDigestWriter(tmp_path).render(content))


def test_the_top_story_tag_sits_directly_above_the_lead_card():
    digest = receipt_fixtures()[1]
    between = digest.split("Top story</span></div>", 1)[1].split('<article class="lead-story"')[0]
    assert between.strip() == ""


def test_the_lead_score_pill_sits_above_its_title(tmp_path):
    card = _lead_card_of(tmp_path, score=8.5)
    assert '<span class="pill score">8.5</span>' in card
    assert card.index('class="pill score"') < card.index("<h2")
    assert "Score:" not in card
    assert "Source:" not in card


def test_the_lead_links_its_article_once_through_its_title(tmp_path):
    card = _lead_card_of(tmp_path)
    assert card.count('href="https://a.test"') == 1
    title = r'<h2>\s*<a href="https://a.test" target="_blank" rel="noopener">Lead</a>'
    assert re.search(title, card)


def test_without_features_the_card_leads_with_the_first_thematic_story(tmp_path):
    content = _content(
        "2026-04-15",
        "evening",
        thematic_summaries=[DigestSection(heading="T", items=_items("Thematic lead"))],
    )

    assert '<h2 lang="">Thematic lead</h2>' in _card_for(tmp_path, content)


def test_a_run_with_no_lead_story_shows_no_card_title(tmp_path):
    assert "<h2" not in _card_for(tmp_path, _content("2026-04-15", "evening"))


def test_a_lead_story_with_an_empty_title_shows_no_card_title(tmp_path):
    content = _content(
        "2026-04-15",
        "evening",
        featured_articles=[DigestSection(heading="F", items=_items(""))],
    )

    assert "<h2" not in _card_for(tmp_path, content)


def _group(summary: str | None = "Shared summary") -> DigestSection:
    """A two-article summarize group; summary=None is the degraded shape, one excerpt each."""
    members = (("Alpha", "A", "https://a.test/1", 7.0), ("Beta", "B", "https://b.test/2", 8.0))
    return DigestSection(
        heading="Group heading",
        summary=summary,
        items=[
            DigestItem(
                title=title,
                summary=summary or f"{title} excerpt",
                sources=[source],
                urls=[url],
                score=score,
            )
            for title, source, url, score in members
        ],
    )


def _main_of(tmp_path, *featured: DigestSection) -> str:
    html = HtmlDigestWriter(tmp_path).render(
        _content("2026-04-15", "evening", featured_articles=list(featured))
    )
    return html[html.index("<main>") : html.index("</main>")]


def _feature_main(tmp_path, section: DigestSection) -> str:
    """<main> of a digest whose lead is a single article, so `section` lands in Features."""
    return _main_of(tmp_path, DigestSection(heading="L", items=_items("Lead")), section)


def test_a_group_feature_prints_its_summary_once_under_its_heading(tmp_path):
    main = _feature_main(tmp_path, _group())
    assert main.count("Shared summary") == 1
    assert '<h3 class="item-title lg">Group heading</h3>' in main


def test_each_article_in_a_group_keeps_its_own_link_and_vote(tmp_path):
    main = _feature_main(tmp_path, _group())
    for title, url in (("Alpha", "https://a.test/1"), ("Beta", "https://b.test/2")):
        assert re.search(rf'<h4 class="item-title"><a href="{url}"[^>]*>{title}</a></h4>', main)
        assert f"data-urls='[\"{url}\"]'" in main
    assert 'data-urls=\'["https://a.test/1", "https://b.test/2"]\'' not in main


def test_a_degraded_section_keeps_one_card_per_excerpt(tmp_path):
    main = _feature_main(tmp_path, _group(summary=None))
    assert "Alpha excerpt" in main and "Beta excerpt" in main
    assert "Group heading" not in main


def test_a_one_article_group_renders_as_its_article(tmp_path):
    section = DigestSection(heading="Group heading", summary="S", items=_items("Only"))
    main = _feature_main(tmp_path, section)
    assert '<a href="https://example.test/Only/0" target="_blank" rel="noopener">Only</a>' in main
    assert "Group heading" not in main


def test_a_group_lead_lists_its_articles_one_level_below_its_title(tmp_path):
    main = _main_of(tmp_path, _group())
    assert main.count("Shared summary") == 1
    assert re.search(r"<h2>\s*Group heading\s*</h2>", main)
    assert re.search(r'<h3 class="item-title lg"><a href="https://a.test/1"', main)
    assert _main_text(f"<main>{main}</main>").levels[:3] == [2, 3, 3]


def test_a_group_lead_gives_the_card_its_heading(tmp_path):
    content = _content("2026-04-15", "evening", featured_articles=[_group()])
    assert '<h2 lang="">Group heading</h2>' in _card_for(tmp_path, content)


def test_the_card_title_is_escaped(tmp_path):
    content = _content(
        "2026-04-15",
        "evening",
        featured_articles=[DigestSection(heading="F", items=_items("<b>x</b>"))],
    )

    assert '<h2 lang="">&lt;b&gt;x&lt;/b&gt;</h2>' in _card_for(tmp_path, content)


def test_without_content_the_card_has_no_title(tmp_path):
    assert "<h2" not in _card_for(tmp_path, None)


@pytest.mark.parametrize(
    ("included", "shown"), [(22, "22 articles"), (1, "1 article"), (0, "0 articles")]
)
def test_the_card_counts_the_articles_the_digest_included(tmp_path, included, shown):
    content = _content("2026-04-15", "evening", articles_included=included)

    assert f'<span class="data">{shown}</span>' in _card_for(tmp_path, content)


def test_without_content_the_card_has_no_count(tmp_path):
    assert 'class="data"' not in _card_for(tmp_path, None)


def _clusters(*clusters: tuple[str, list[int]]) -> DigestContent:
    """An issue whose news clusters hold items with the given URL counts."""
    return _content(
        "2026-04-15",
        "evening",
        news_clusters=[
            DigestSection(
                heading=heading,
                items=[
                    item
                    for n, urls in enumerate(sizes)
                    for item in _items(f"{heading}{n}", urls=urls)
                ],
            )
            for heading, sizes in clusters
        ],
    )


@pytest.mark.parametrize(
    ("clusters", "line"),
    [
        ((("Small", [2]), ("Big", [5]), ("Mid", [3])), "Big · Mid"),
        ((("Split", [1, 1]), ("Wide", [5])), "Wide · Split"),
        ((("First", [2]), ("Second", [2]), ("Third", [2])), "First · Second"),
        ((("Only", [3]),), "Only"),
    ],
    ids=["largest-two", "members-are-urls", "ties-keep-order", "one-cluster"],
)
def test_the_card_names_the_two_largest_stories(tmp_path, clusters, line):
    assert f'<p class="small" lang="">{line}</p>' in _card_for(tmp_path, _clusters(*clusters))


def test_a_run_without_news_clusters_shows_no_topic_line(tmp_path):
    assert 'class="small"' not in _card_for(tmp_path, _content("2026-04-15", "evening"))


def test_the_card_topics_are_escaped(tmp_path):
    card = _card_for(tmp_path, _clusters(("<script>alert(1)</script>", [1])))

    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in card
    assert "<script>alert(1)" not in card


def _row_classes(html: str) -> list[tuple[str, str]]:
    """(date and period, class attribute) of every archive row, in page order."""
    return [
        (f"{date} {period}", cls)
        for cls, date, period in re.findall(
            r'<div class="(list-row archive-row[^"]*)"><span class="date">([^<]+)</span>'
            r'<span class="label">([^<]+)</span>',
            _tight(html),
        )
    ]


@pytest.mark.parametrize(
    ("labels", "order"), [(("dusk", "noon", "dawn"), ("dawn", "noon", "dusk")), ("cba", "abc")]
)
def test_a_days_later_rows_are_marked_as_the_same_day(tmp_path, labels, order):
    late, mid, early = labels
    names = [
        "2026-04-16-evening.html",
        f"2026-04-15-{late}.html",
        f"2026-04-15-{mid}.html",
        f"2026-04-15-{early}.html",
        "2026-04-14-evening.html",
    ]

    html = HtmlDigestWriter(tmp_path).render_index(names, period_order=order)

    assert _row_classes(html) == [
        (f"2026-04-15 {late}", "list-row archive-row"),
        (f"2026-04-15 {mid}", "list-row archive-row same-day"),
        (f"2026-04-15 {early}", "list-row archive-row same-day"),
        ("2026-04-14 evening", "list-row archive-row"),
    ]


def test_a_row_below_the_card_of_its_own_day_is_not_marked(tmp_path):
    names = ["2026-04-15-morning.html", "2026-04-15-evening.html"]

    html = HtmlDigestWriter(tmp_path).render_index(names)

    assert _card_line("2026-04-15", "evening") in _card(html)
    assert _row_classes(html) == [("2026-04-15 morning", "list-row archive-row")]


def test_a_panels_first_row_is_never_marked(tmp_path):
    names = [
        "2026-05-01-evening.html",
        "2026-05-01-morning.html",
        "2026-04-30-evening.html",
        "2026-04-30-morning.html",
        "2026-03-31-evening.html",
    ]

    html = _tight(HtmlDigestWriter(tmp_path).render_index(names))

    firsts = re.findall(
        r'<section class="panel"><div class="panel-head">.*?</div><div class="([^"]*)"', html
    )
    assert firsts == ["list-row archive-row", "list-row archive-row", "list-row archive-row"]


def _rows(html: str) -> list[str]:
    """Every archive row's markup, whitespace between tags removed."""
    return re.findall(r'<div class="list-row archive-row[^"]*">.*?</div>', _tight(html))


COUNTED = ["2026-09-02-morning.html", "2026-08-31-morning.html"]


@pytest.mark.parametrize(("n", "shown"), [(18, "18 articles"), (1, "1 article")])
def test_a_row_shows_its_recorded_article_count(tmp_path, n, shown):
    html = HtmlDigestWriter(tmp_path).render_index(COUNTED, counts={("2026-08-31", "morning"): n})

    (row,) = _rows(html)
    assert (
        f'<span class="label">morning</span><span class="small">{shown}</span>'
        '<span class="actions">'
    ) in row


def test_a_row_without_a_record_shows_no_count(tmp_path):
    html = HtmlDigestWriter(tmp_path).render_index(COUNTED, counts={})

    assert [row for row in _rows(html) if 'class="small"' in row] == []


def test_a_count_for_an_unlisted_issue_adds_no_row(tmp_path):
    html = HtmlDigestWriter(tmp_path).render_index(COUNTED, counts={("2020-01-01", "morning"): 3})

    assert html.count(">Digest</a>") == len(COUNTED)
    assert "2020-01-01" not in html


def test_the_card_takes_no_count_from_the_recorded_counts(tmp_path):
    html = HtmlDigestWriter(tmp_path).render_index(COUNTED, counts={("2026-09-02", "morning"): 7})

    assert 'class="data"' not in _card(html)


def test_write_index(tmp_path):
    """C4 Test 1: write_index creates index.html with links."""
    # Create one digest
    (tmp_path / "2026-04-15-morning.html").write_text("<html>test</html>")

    writer = HtmlDigestWriter(tmp_path)
    index_path = writer.write_index(tmp_path)

    assert index_path == tmp_path / "index.html"
    assert index_path.exists()

    content = index_path.read_text()
    assert "<!DOCTYPE html>" in content
    assert "2026-04-15-morning.html" in content


def test_the_local_archive_leads_with_this_runs_issue(tmp_path):
    (tmp_path / "2026-04-15-evening.html").write_text("x")
    content = _content(
        "2026-04-16",
        "morning",
        articles_included=5,
        featured_articles=[DigestSection(heading="F", items=_items("Local lead"))],
    )

    HtmlDigestWriter(tmp_path).write(content)

    index = (tmp_path / "index.html").read_text()
    card = _card(index)
    assert _card_line("2026-04-16", "morning") in card
    assert '<h2 lang="">Local lead</h2>' in card
    assert '<span class="data">5 articles</span>' in card
    assert [row for row in _rows(index) if 'class="small"' in row] == []


def _card_of(card: html_digest.HeadlineCard) -> str:
    """The headline card the archive template draws from `card` alone."""
    issue = {"date": "2026-04-16", "period": "morning", "filename": "x.html", "raw_filename": None}
    template = HtmlDigestWriter("unused").env.get_template("index.html.j2")
    return _card(template.render(digests=[issue], latest=issue, card=card, months=[]))


def test_a_card_field_set_to_none_is_left_off():
    card = _card_of(html_digest.HeadlineCard())
    assert "<h2" not in card
    assert 'class="data"' not in card
    assert 'class="small"' not in card


def test_a_card_count_of_zero_still_shows():
    assert '<span class="data">0 articles</span>' in _card_of(html_digest.HeadlineCard(count=0))


def test_the_archive_checks_an_absent_field_one_way():
    source = (Path(html_digest.__file__).parent / "templates" / "index.html.j2").read_text()
    conditions = re.findall(r"{%\s*if\s+((?:card|digest)\b[^%]*?)\s*%}", source)
    assert len(conditions) == 4
    assert [c for c in conditions if not c.endswith(" is not none")] == []


@pytest.mark.parametrize(("n", "said"), [(0, "0 issues"), (1, "1 issue"), (2, "2 issues")])
def test_the_templates_count_in_english_plurals(n, said):
    env = HtmlDigestWriter("unused").env
    assert env.from_string('{{ n | plural("issue") }}').render(n=n) == said


def test_no_template_spells_its_own_plural():
    templates = (Path(html_digest.__file__).parent / "templates").glob("*.j2")
    spelled = re.compile(r"==\s*1\s+else\s+[\"']s[\"']")
    assert [t.name for t in templates if spelled.search(t.read_text())] == []


def test_a_digest_is_named_by_its_date_and_period():
    assert HtmlDigestWriter.digest_filename("2026-04-16", "evening") == "2026-04-16-evening.html"


def test_the_digest_and_its_archive_card_follow_one_file_name(tmp_path, monkeypatch):
    monkeypatch.setattr(
        HtmlDigestWriter, "digest_filename", staticmethod(lambda d, p: f"{d}-{p}-v2.html")
    )
    # A newer issue on disk, so only the card's lookup can put this run on it.
    (tmp_path / "2026-04-17-morning.html").write_text("x")
    content = _content(
        "2026-04-16",
        "morning",
        featured_articles=[DigestSection(heading="F", items=_items("Renamed lead"))],
    )

    path = HtmlDigestWriter(tmp_path).write(content)

    assert path == tmp_path / "2026-04-16-morning-v2.html"
    assert '<h2 lang="">Renamed lead</h2>' in _card((tmp_path / "index.html").read_text())


def test_an_index_written_without_a_run_shows_only_the_newest_issue_on_its_card(tmp_path):
    (tmp_path / "2026-04-15-evening.html").write_text("x")

    card = _card(HtmlDigestWriter(tmp_path).write_index(tmp_path).read_text())

    assert _card_line("2026-04-15", "evening") in card
    assert "<h2" not in card
    assert 'class="data"' not in card


def test_config_html_output_enabled():
    """C5 Test 1: Config with html_output enabled parses correctly."""
    import tempfile
    import tomllib

    toml_content = """
    [html_output]
    enabled = true
    output_dir = "/tmp/digests"
    """

    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write(toml_content)
        f.flush()

        with open(f.name, "rb") as rf:
            raw = tomllib.load(rf)

        from fakes import make_config

        config = make_config(html_output=raw["html_output"]).app

        assert config.html_output.enabled is True
        assert config.html_output.output_dir == "/tmp/digests"

        # Cleanup
        Path(f.name).unlink()


def test_config_html_output_defaults():
    """C5 Test 2: Config without html_output uses defaults."""
    from fakes import make_config

    config = make_config().app

    assert config.html_output.enabled is False
    assert config.html_output.output_dir == "agent-vault/html"


def test_all_sections_render(tmp_path):
    """Integration test: All section types render correctly."""
    content = DigestContent(
        date="2026-04-15",
        period="evening",
        sources_processed=5,
        articles_received=20,
        articles_included=10,
        usage=UsageStats(
            input_tokens=1000, output_tokens=500, api_calls=3, model="claude-sonnet-4-6"
        ),
        featured_articles=[
            DigestSection(
                heading="Top Stories",
                items=[
                    DigestItem(
                        title="Featured Story",
                        summary="Lead story summary.",
                        sources=["Source A"],
                        urls=["https://a.com"],
                        score=9.5,
                    ),
                    DigestItem(
                        title="Second Feature",
                        summary="Secondary feature.",
                        sources=["Source B"],
                        urls=["https://b.com"],
                        score=8.7,
                    ),
                ],
            )
        ],
        news_clusters=[
            DigestSection(
                heading="Tech Industry",
                items=[
                    DigestItem(
                        title="Cluster 1",
                        summary="News cluster summary.",
                        sources=["News A", "News B"],
                        urls=["https://na.com", "https://nb.com"],
                    )
                ],
            )
        ],
        thematic_summaries=[
            DigestSection(
                heading="AI Research",
                description="Latest developments in AI",
                items=[
                    DigestItem(
                        title="Research Paper",
                        summary="Paper summary.",
                        sources=["ArXiv"],
                        urls=["https://arxiv.org/1234"],
                    )
                ],
            )
        ],
        attention_sections=[
            DigestSection(
                heading="Worth Watching",
                items=[
                    DigestItem(
                        title="Attention Item",
                        summary="Brief snippet.",
                        sources=["Blog"],
                        urls=["https://blog.com"],
                    )
                ],
            )
        ],
        filtered_headlines=[
            DigestItem(
                title="Headline 1",
                summary="Brief summary",
                sources=["News"],
                urls=["https://news.com/1"],
            ),
            DigestItem(
                title="Headline 2",
                summary="Another summary",
                sources=["News"],
                urls=["https://news.com/2"],
            ),
        ],
        triage_pending_count=5,
    )

    writer = HtmlDigestWriter(tmp_path)
    html = writer.render(content)

    # Check all sections present
    assert "Featured Story" in html
    assert "Second Feature" in html
    assert "In Focus" in html
    assert "Tech Industry" in html
    # Thematic summaries render inside the Features stream, not as their own section
    assert "Research Paper" in html
    assert "Thematic Summaries" not in html
    assert "On the Radar" in html
    assert "Worth Watching" in html
    assert "The Wire" in html
    assert "Headline 1" in html
    assert "5 awaiting triage" in html


def test_promote_buttons_on_every_section(tmp_path):
    """Every rendered item is votable, and a cluster's vote carries all its articles."""
    item = lambda n: DigestItem(  # noqa: E731
        title=f"Item {n}", summary="s", sources=["Src"], urls=[f"https://x.com/{n}"]
    )
    content = DigestContent(
        date="2026-04-15",
        period="evening",
        sources_processed=1,
        articles_received=1,
        articles_included=1,
        usage=UsageStats(),
        featured_articles=[DigestSection(heading="Top", items=[item(1), item(2)])],
        news_clusters=[
            DigestSection(
                heading="Tech",
                items=[
                    DigestItem(
                        title="Cluster",
                        summary="s",
                        sources=["A", "B"],
                        urls=["https://na.com", "https://nb.com"],
                    )
                ],
            )
        ],
        fan_sections=[DigestSection(heading="Fan", items=[item(3)])],
        thematic_summaries=[DigestSection(heading="Theme", items=[item(4)])],
        attention_sections=[DigestSection(heading="Watch", items=[item(5)])],
        filtered_headlines=[item(6)],
    )

    writer = HtmlDigestWriter(tmp_path)
    html = writer.render(content)

    # lead + featured + cluster + fan + thematic + attention + headline
    assert html.count('class="vote-group"') == 7
    assert 'data-urls=\'["https://na.com", "https://nb.com"]\'' in html
    # The deep-read queue is gone: Obsidian Clipper covers saving, cyris only filters.
    # The wire value is what a published page would send back, so that is what is
    # asserted; the button's old label is a string this codebase no longer holds.
    assert 'data-vote="deep"' not in html
    # Every article in the cluster stays individually openable, and the links stay one
    # flex item of the meta row rather than splitting apart at its gap.
    assert (
        '<span class="src-links"><a href="https://na.com" target="_blank" rel="noopener">A</a>'
        ' · <a href="https://nb.com" target="_blank" rel="noopener">B</a></span>'
    ) in html
    # Two sources is not a mess; folding it would cost a tap for nothing.
    assert "<details" not in html


def _cluster_digest(n_sources: int) -> DigestContent:
    return DigestContent(
        date="2026-04-15",
        period="evening",
        sources_processed=1,
        articles_received=1,
        articles_included=1,
        usage=UsageStats(),
        news_clusters=[
            DigestSection(
                heading="Tech",
                items=[
                    DigestItem(
                        title="Cluster",
                        summary="s",
                        sources=[f"S{i}" for i in range(n_sources)],
                        urls=[f"https://n{i}.com" for i in range(n_sources)],
                    )
                ],
            )
        ],
    )


def test_a_crowded_cluster_folds_its_sources(tmp_path):
    """Five source links wrap into a mess on a phone, so they collapse behind a tap."""
    html = HtmlDigestWriter(tmp_path).render(_cluster_digest(5))

    assert '<details class="src-fold">' in html
    assert '<summary lang="en">5 sources</summary>' in html
    # Folded, not dropped — every link is still in the markup and still reachable.
    for i in range(5):
        assert f'<a href="https://n{i}.com" target="_blank" rel="noopener">S{i}</a>' in html


def test_vote_buttons_use_arrows_not_emoji(tmp_path):
    """Bare emoji ignore `color`, so .done could never tint them to the accent."""
    writer = HtmlDigestWriter(tmp_path)
    html = writer.render(_cluster_digest(2))

    button = '<button class="btn sm secondary promote-btn" data-vote="{}" title="{}">{}</button>'
    assert button.format("up", "More like this", "↑") in html
    assert button.format("down", "Less like this", "↓") in html
    assert "👍" not in html
    assert "👎" not in html


def test_news_cluster_vote_group_carries_story_id(tmp_path):
    """T1: a cluster with a story_id renders it as data-story-id on its vote-group span."""
    content = _cluster_digest(2)
    content.news_clusters[0].story_id = "2026-08-28-morning-0"

    writer = HtmlDigestWriter(tmp_path)
    html = writer.render(content)

    assert 'class="vote-group" data-story-id="2026-08-28-morning-0" data-urls=' in html


def test_news_cluster_without_story_id_renders_unchanged(tmp_path):
    """T2: story_id=None emits no data-story-id; the page is otherwise identical."""
    writer = HtmlDigestWriter(tmp_path)
    html = writer.render(_cluster_digest(2))

    assert "data-story-id" not in html
    # The vote-group span keeps its pre-story shape.
    assert '<span class="vote-group" data-urls=' in html


def test_fan_item_links_to_newsletter_references_but_votes_on_store_url(tmp_path):
    """Newsletter references are reader links; the store URL remains the vote key."""
    item = DigestItem(
        title="Newsletter item",
        summary="s",
        sources=["Newsletter"],
        urls=["newsletter:abc"],
        ref_urls=["https://r1.com/a", "https://r2.com/b"],
    )
    content = DigestContent(
        date="2026-04-15",
        period="evening",
        sources_processed=1,
        articles_received=1,
        articles_included=1,
        usage=UsageStats(),
        fan_sections=[DigestSection(heading="Fan", items=[item])],
    )

    html = HtmlDigestWriter(tmp_path).render(content)

    assert '<a href="https://r1.com/a" target="_blank" rel="noopener">Newsletter item</a>' in html
    assert '<a href="https://r2.com/b" target="_blank" rel="noopener">r2.com</a>' in html
    assert "data-urls='[\"newsletter:abc\"]'" in html
    assert "data-urls='[&#34;https://r1.com/a&#34;" not in html


def test_mixed_cluster_references_render_while_votes_use_store_urls(tmp_path):
    item = DigestItem(
        title="Mixed cluster",
        summary="s",
        sources=["Source A", "Newsletter"],
        urls=["https://store.example/a", "newsletter:202"],
        ref_urls=[
            "https://store.example/a",
            "https://ref.example/one",
            "https://ref.example/two",
        ],
    )
    content = DigestContent(
        date="2026-04-15",
        period="evening",
        sources_processed=2,
        articles_received=2,
        articles_included=2,
        usage=UsageStats(),
        news_clusters=[DigestSection(heading="Mixed", items=[item])],
    )

    html = HtmlDigestWriter(tmp_path).render(content)

    for url in item.ref_urls:
        assert f'<a href="{url}" target="_blank" rel="noopener">' in html
    assert 'data-urls=\'["https://store.example/a", "newsletter:202"]\'' in html


def test_single_newsletter_article_does_not_render_body_links_as_sources(tmp_path):
    """A newsletter's body links do not turn its one article into many sources."""
    item = DigestItem(
        title="Newsletter item",
        summary="s",
        sources=["Newsletter"],
        urls=["newsletter:abc"],
        ref_urls=["https://article.example/story", "https://footer.example/privacy"],
    )
    content = DigestContent(
        date="2026-04-15",
        period="evening",
        sources_processed=1,
        articles_received=1,
        articles_included=1,
        usage=UsageStats(),
        featured_articles=[DigestSection(heading="Features", items=[item, item])],
    )

    html = HtmlDigestWriter(tmp_path).render(content)

    assert "2 sources" not in html
    assert "footer.example" not in html
    assert html.count('<span class="source-tag">Newsletter</span>') == 2
    assert html.count("data-urls='[\"newsletter:abc\"]'") == 2


def test_fan_item_without_references_keeps_its_store_url(tmp_path):
    """Non-newsletter fan items retain their existing reader link."""
    item = DigestItem(
        title="Mailchimp item",
        summary="s",
        sources=["Newsletter"],
        urls=["https://mailchi.mp/x"],
        ref_urls=[],
    )
    content = DigestContent(
        date="2026-04-15",
        period="evening",
        sources_processed=1,
        articles_received=1,
        articles_included=1,
        usage=UsageStats(),
        fan_sections=[DigestSection(heading="Fan", items=[item])],
    )

    html = HtmlDigestWriter(tmp_path).render(content)

    assert (
        '<a href="https://mailchi.mp/x" target="_blank" rel="noopener">Mailchimp item</a>' in html
    )


def test_news_cluster_without_references_keeps_existing_source_markup(tmp_path):
    """RSS cluster source links keep their existing source labels and order."""
    html = HtmlDigestWriter(tmp_path).render(_cluster_digest(2))

    assert (
        '<a href="https://n0.com" target="_blank" rel="noopener">S0</a> · '
        '<a href="https://n1.com" target="_blank" rel="noopener">S1</a>'
    ) in html


def test_fan_item_with_one_reference_keeps_existing_source_markup(tmp_path):
    """A single original link changes the destination, not the source label."""
    item = DigestItem(
        title="Newsletter item",
        summary="s",
        sources=["Newsletter"],
        urls=["newsletter:abc"],
        ref_urls=["https://r1.com/a"],
    )
    content = DigestContent(
        date="2026-04-15",
        period="evening",
        sources_processed=1,
        articles_received=1,
        articles_included=1,
        usage=UsageStats(),
        fan_sections=[DigestSection(heading="Fan", items=[item])],
    )

    html = HtmlDigestWriter(tmp_path).render(content)

    assert '<span class="source-tag">Newsletter</span>' in html
    assert '<a href="https://r1.com/a" target="_blank" rel="noopener">r1.com</a>' not in html


def test_synthetic_store_url_never_becomes_an_href(sample_digest_content, tmp_path):
    """A newsletter:<id> store URL is a dead link: render the title and source unlinked."""
    sample_digest_content.featured_articles = [
        DigestSection(
            heading="Featured",
            items=[
                DigestItem(
                    title="曼報本期",
                    summary="內文",
                    sources=["曼報"],
                    urls=["newsletter:deadbeef"],
                )
            ],
        )
    ]

    html = HtmlDigestWriter(tmp_path).render(sample_digest_content)

    assert "曼報本期" in html
    assert "newsletter:deadbeef" not in html.replace("data-urls='[\"newsletter:deadbeef\"]'", "")


def _stored(title, source, state=ArticleState.PENDING, score=None):
    now = datetime.now(UTC)
    return StoredArticle(
        url=f"https://example.com/{abs(hash(title))}",
        original_id=abs(hash(title)),
        title=title,
        content="",
        published_at=now,
        source_name=source,
        source_tier=Tier.FILTER,
        state=state,
        first_seen_at=now,
        score=score,
    )


def test_write_raw_groups_by_source_and_escapes(tmp_path):
    """Raw page groups articles per source and escapes feed-controlled titles."""
    writer = HtmlDigestWriter(tmp_path)
    articles = [
        _stored("Kept", "Src A", state=ArticleState.ACCEPTED, score=0.9),
        _stored("Unscored", "Src A"),
        _stored("<script>alert(1)</script> & co", "Src B", state=ArticleState.REJECTED),
    ]

    path = writer.write_raw("2026-08-20", "morning", articles)
    html = path.read_text(encoding="utf-8")

    assert path == tmp_path / "2026-08-20-morning-raw.html"
    assert "Src A" in html and "Src B" in html
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; co" in html
    # Scored article precedes the unscored one within its source group
    assert html.index("Kept") < html.index("Unscored")
    assert "3 articles" in html
    assert 'href="2026-08-20-morning.html"' in html


def test_index_skips_raw_pages(tmp_path):
    """A raw companion is an entry on its issue's row, not an issue of its own."""
    writer = HtmlDigestWriter(tmp_path)
    (tmp_path / "2026-08-20-morning.html").write_text("x")
    (tmp_path / "2026-08-20-morning-raw.html").write_text("x")

    index = writer.render_index([f.name for f in tmp_path.iterdir() if f.is_file()])

    assert index.count(">Digest</a>") == 1
    assert index.count(">All articles</a>") == 1
    assert 'href="2026-08-20-morning.html">Digest</a>' in index
    assert 'href="2026-08-20-morning-raw.html">All articles</a>' in index


def test_an_issue_without_a_raw_page_offers_only_its_digest(tmp_path):
    """No dead link: All articles appears only when the raw page exists."""
    index = HtmlDigestWriter(tmp_path).render_index(["2026-08-20-morning.html"])

    assert 'href="2026-08-20-morning.html">Digest</a>' in index
    assert ">All articles</a>" not in index


def test_the_archive_links_the_raw_page_under_the_name_write_raw_gave_it(tmp_path):
    writer = HtmlDigestWriter(tmp_path)
    raw = writer.write_raw("2026-08-20", "morning", [_stored("Article", "Src")])

    index = writer.render_index(["2026-08-20-morning.html", raw.name])

    assert f'href="{raw.name}">All articles</a>' in index


def test_write_raw_renders_vote_buttons(tmp_path):
    """Rejected articles get up/down buttons so the raw page can pull them back."""
    writer = HtmlDigestWriter(tmp_path)
    articles = [_stored("Dropped", "Src A", state=ArticleState.REJECTED)]

    html = writer.write_raw("2026-08-20", "morning", articles).read_text(encoding="utf-8")

    assert html.count('class="vote-group"') == 1
    assert 'data-vote="up"' in html and 'data-vote="down"' in html
    # The vote goes to the app Worker, which holds the token
    assert '"/api/vote"' in html or "'/api/vote'" in html


def test_no_template_reaches_for_a_credential():
    """The private-votes invariant, checked where it can still break.

    It used to be checked by handing the writer a token and grepping the output
    for it. That stopped meaning anything once the writer stopped taking one —
    a renderer holding no credential cannot leak one, so the assertion could
    only pass. What can regress is a template reaching for a value again, so
    the templates are what this reads.
    """
    templates = sorted((Path(html_digest.__file__).parent / "templates").glob("*.j2"))
    assert templates, "no templates found — this test would pass on an empty glob"

    forbidden = ("promote_token", "bearer", "authorization", "api_key", "secret")
    offenders = [
        f"{path.name}: {word}"
        for path in templates
        for word in forbidden
        if word in path.read_text().lower()
    ]

    assert offenders == []


def test_the_published_page_votes_through_the_app_worker(tmp_path):
    """Buttons on the page, and the only route they may call."""
    writer = HtmlDigestWriter(tmp_path)
    content = DigestContent(
        date="2026-04-15",
        period="evening",
        sources_processed=1,
        articles_received=1,
        articles_included=1,
        usage=UsageStats(),
        featured_articles=[
            DigestSection(
                heading="Featured",
                items=[
                    DigestItem(
                        title="Article",
                        summary="Summary",
                        sources=["Src"],
                        urls=["https://example.com/1"],
                    )
                ],
            )
        ],
    )

    digest_html = writer.render(content)
    raw_html = writer.render_raw("2026-04-15", "evening", [_stored("Article", "Src")])

    # Present on both surfaces, hidden until the capability probe answers
    assert 'class="vote-group"' in digest_html
    assert 'class="vote-group"' in raw_html
    assert '"/api/vote"' in digest_html or "'/api/vote'" in digest_html
    assert '"/api/vote"' in raw_html or "'/api/vote'" in raw_html


def test_vote_buttons_hidden_by_default_shown_by_capability_probe(tmp_path):
    """Vote buttons are hidden by default and shown by capability probe (Access check)."""
    writer = HtmlDigestWriter(tmp_path)
    content = DigestContent(
        date="2026-04-15",
        period="evening",
        sources_processed=1,
        articles_received=1,
        articles_included=1,
        usage=UsageStats(),
        featured_articles=[
            DigestSection(
                heading="Featured",
                items=[
                    DigestItem(
                        title="Article",
                        summary="Summary",
                        sources=["Src"],
                        urls=["https://example.com/1"],
                    )
                ],
            )
        ],
    )

    html = writer.render(content)

    # Vote groups should have display: none by default
    assert ".vote-group { display: none;" in html
    # Capability probe should exist and use GET with redirect: manual
    assert "fetch('/api/vote'" in html or 'fetch("/api/vote"' in html
    assert "method: 'GET'" in html or 'method: "GET"' in html
    assert "redirect: 'manual'" in html or 'redirect: "manual"' in html
    # Probe must parse JSON and check authorized === true, not just 2xx status
    assert "await resp.json()" in html or "await resp.json ()" in html
    assert "data.authorized !== true" in html or "data.authorized === true" in html
    # Probe should set display: inline-flex when authorized
    assert "style.display = 'inline-flex'" in html or 'style.display = "inline-flex"' in html


def test_settings_link_hidden_by_default_shown_by_capability_probe(tmp_path):
    """Settings link is hidden by default and shown by same capability probe as votes."""
    writer = HtmlDigestWriter(tmp_path)
    content = DigestContent(
        date="2026-04-15",
        period="evening",
        sources_processed=1,
        articles_received=1,
        articles_included=1,
        usage=UsageStats(),
    )

    html = writer.render(content)

    # Settings link should be present but hidden by default
    assert '<a class="label settings-link" href="/settings">Settings' in html
    assert ".settings-link { display: none;" in html
    # Capability probe must parse JSON and check authorized, not just 2xx
    assert "await resp.json()" in html or "await resp.json ()" in html
    assert "data.authorized !== true" in html or "data.authorized === true" in html
    # Probe should show it when authorized
    assert "'.settings-link'" in html or '".settings-link"' in html
    assert "style.display = 'inline'" in html or 'style.display = "inline"' in html


def test_the_archive_hides_settings_behind_the_same_probe(tmp_path):
    """The archive gates Settings on the /api/vote probe, and carries no vote code.

    A visitor with no session is the bypass path: the archive has to treat it as
    unauthorized like digest and raw do, and has nothing it could vote with.
    """
    html = HtmlDigestWriter(tmp_path).render_index([])

    assert ".settings-link { display: none;" in html
    assert "fetch('/api/vote'" in html
    assert "redirect: 'manual'" in html
    assert "if (!resp.ok) return;" in html
    assert "data.authorized !== true" in html
    assert "document.querySelectorAll('.settings-link')" in html
    assert "method: 'POST'" not in html
    assert "DIGEST_DATE" not in html


def test_digest_and_raw_probe_once_and_keep_their_votes(tmp_path):
    pages = _issue_pages(tmp_path)

    for html in (pages["digest"], pages["raw"]):
        assert html.count("redirect: 'manual'") == 1
        assert "DIGEST_DATE" in html


@pytest.mark.parametrize("page", ["digest", "raw"])
def test_every_vote_goes_through_the_one_cast_vote_path(tmp_path, page):
    html = _issue_pages(tmp_path)[page]

    assert html.count("async function castVote(group, vote)") == 1
    assert html.count("method: 'POST'") == 1


REVEAL_GATED = "document.querySelectorAll('.gated').forEach((g) => g.hidden = false);"
AUTHORIZED_CHECK = "data.authorized !== true"


def _gated_reveal_problems(script: str) -> list[str]:
    """A gated control may be revealed only after the probe said authorized."""
    if REVEAL_GATED not in script:
        return ["the probe reveals no gated control"]
    if script.index(REVEAL_GATED) < script.index(AUTHORIZED_CHECK):
        return ["gated controls are revealed before the authorization check"]
    return []


def test_the_probe_reveals_gated_controls_only_once_authorized(tmp_path):
    script = HtmlDigestWriter(tmp_path).env.get_template("_probe_script.html.j2").render()

    assert _gated_reveal_problems(script) == []
    early = REVEAL_GATED + script.replace(REVEAL_GATED, "")
    assert _gated_reveal_problems(early) == [
        "gated controls are revealed before the authorization check"
    ]


RAW_VIEWS = (
    '<div class="seg gated" role="group" aria-label="Article view" id="raw-views" hidden>'
    '<button type="button" data-raw-view="list" aria-pressed="true">List</button>'
    '<button type="button" data-raw-view="triage" aria-pressed="false">Triage</button></div>'
)


def test_the_raw_view_switch_sits_hidden_at_the_end_of_the_page_head(tmp_path):
    raw = _issue_pages(tmp_path)["raw"]

    assert raw.count(RAW_VIEWS) == 1
    assert raw.index('class="page-head"') < raw.index(RAW_VIEWS)
    assert raw.index(RAW_VIEWS) < raw.index('<section class="panel">')
    assert parse_style_block(raw)["[hidden]"] == {"display: none !important"}


def test_the_raw_page_holds_both_views_before_its_footer(tmp_path):
    raw = _issue_pages(tmp_path)["raw"]

    assert '<div class="deck-wrap" id="raw-triage" hidden>' in raw
    assert (
        raw.index('id="raw-groups"')
        < raw.index('id="raw-triage"')
        < raw.index(f'<div class="footer">{CREDIT}</div>')
    )
    assert raw.index('id="raw-groups"') < raw.index('<section class="panel">')


TRIAGE_MARKUP = (
    '<span class="label" id="t-remaining"></span>',
    '<article class="card" id="t-card"><span class="label" id="t-source"></span>'
    '<h2 id="t-title"></h2></article>',
    '<div class="deck-actions" id="t-actions">'
    '<button class="btn danger" type="button" id="t-down" data-dir="down">Down</button>'
    '<button class="btn primary" type="button" id="t-up" data-dir="up">Up</button></div>',
    '<p class="small" id="t-hint">Swipe left for down, right for up.'
    " Tap the card to open the article in a new tab.</p>",
)


def test_the_triage_view_holds_one_card_and_its_two_buttons(tmp_path):
    raw = _issue_pages(tmp_path)["raw"]
    triage = raw[raw.index('id="raw-triage"') : raw.index('<div class="footer">')]

    for markup in TRIAGE_MARKUP:
        assert markup in triage
    assert re.findall(r"<button\b[^>]*data-vote", triage) == []


VOTE_FAILED = (
    '<p class="notice err" id="t-error" role="alert" hidden>The vote did not go through,'
    " so this card stays. Check your connection, then try again.</p>"
)


def test_a_failed_triage_vote_is_explained_under_its_buttons(tmp_path):
    raw = _issue_pages(tmp_path)["raw"]

    assert raw.count(VOTE_FAILED) == 1
    assert raw.index('id="t-actions"') < raw.index(VOTE_FAILED) < raw.index('id="t-hint"')


def test_the_raw_page_fetches_only_to_vote_and_to_probe(tmp_path):
    assert _issue_pages(tmp_path)["raw"].count("fetch(") == 2


def test_the_issue_bar_switch_is_never_gated(tmp_path):
    raw = _issue_pages(tmp_path)["raw"]

    assert '<nav class="seg" aria-label="Issue views">' in raw


def _issue_pages(tmp_path) -> dict[str, str]:
    """Archive, digest and raw for one 2026-04-15 evening issue."""
    writer = HtmlDigestWriter(tmp_path)
    content = DigestContent(
        date="2026-04-15",
        period="evening",
        sources_processed=1,
        articles_received=1,
        articles_included=1,
        usage=UsageStats(),
    )
    return {
        "index": writer.render_index(["2026-04-15-evening.html", "2026-04-15-evening-raw.html"]),
        "digest": writer.render(content, raw_page=True),
        "raw": writer.render_raw("2026-04-15", "evening", [_stored("Article", "Src")]),
    }


def _site_bar(html: str) -> str:
    match = re.search(r'<header class="site-bar">.*?</header>', html, re.DOTALL)
    assert match, "no site bar"
    return match.group(0)


@pytest.mark.parametrize("page", ["index", "digest", "raw"])
def test_every_page_opens_with_the_site_bar(tmp_path, page):
    html = _issue_pages(tmp_path)[page]

    assert html.count('<header class="site-bar">') == 1
    assert html.index('<header class="site-bar">') < html.index('class="container"')
    bar = _site_bar(html)
    assert 'aria-current="page">Archive</a>' in bar
    assert '<a class="label settings-link" href="/settings">Settings</a>' in bar
    assert 'class="brand-name">CYRIS<' in bar


# The partial's output when no page names itself current, line for line.
SITE_BAR_ON_THE_ARCHIVE = "\n".join(
    [
        '    <header class="site-bar">',
        '        <div class="site-bar-inner">',
        '            <a class="brand" href="index.html"><span class="brand-mark"></span>'
        '<span class="brand-name">CYRIS</span></a>',
        '            <nav class="site-nav" aria-label="Site">',
        '                <a class="label" href="index.html" aria-current="page">Archive</a>',
        '                <a class="label settings-link" href="/settings">Settings</a>',
        '                <a class="label signin-link" href="/login" hidden>Sign in</a>',
        "            </nav>",
        "        </div>",
        "    </header>",
    ]
)


def test_the_site_bar_marks_the_archive_unless_told_otherwise(tmp_path):
    bar = HtmlDigestWriter(tmp_path).env.get_template("_site_bar.html.j2").render()

    assert bar == SITE_BAR_ON_THE_ARCHIVE


def test_the_site_bar_can_mark_settings_as_the_current_page(tmp_path):
    env = HtmlDigestWriter(tmp_path).env
    wrapper = '{% with current="settings" %}{% include "_site_bar.html.j2" %}{% endwith %}'

    bar = env.from_string(wrapper).render()

    assert '<a class="label settings-link" href="/settings" aria-current="page">Settings</a>' in bar
    assert bar.count("aria-current") == 1


def test_settings_links_the_archive_only_once_the_probe_says_signed_in(tmp_path):
    """A local `cyris triage-ui` serves no archive, so /settings must not link a 404.

    Behind the app Worker the /api/vote probe answers authorized and restores both.
    """
    env = HtmlDigestWriter(tmp_path).env
    wrapper = '{% with current="settings" %}{% include "_site_bar.html.j2" %}{% endwith %}'

    bar = env.from_string(wrapper).render()

    assert '<a class="label gated" href="index.html" hidden>Archive</a>' in bar
    assert '<a class="brand" data-gated-href="index.html">' in bar
    assert ' href="index.html"><span class="brand-mark">' not in bar
    probe = env.get_template("_probe_script.html.j2").render()
    assert "document.querySelectorAll('.gated').forEach((g) => g.hidden = false);" in probe
    assert "a.href = a.dataset.gatedHref" in probe


def test_the_probe_offers_sign_in_only_on_the_workers_own_refusal(tmp_path):
    """Sign in links /login with this page as next; pages.dev and Access redirects get none."""
    html = HtmlDigestWriter(tmp_path).render_index([])

    assert '<a class="label signin-link" href="/login" hidden>Sign in</a>' in html
    assert "if (resp.status === 401) {" in html
    assert "if (refused.authorized !== false) return;" in html
    assert "encodeURIComponent(location.pathname + location.search)" in html
    assert "a.href = '/login?next=' + next;" in html


@pytest.mark.parametrize("page", ["index", "digest", "raw"])
def test_the_site_bar_has_no_triage(tmp_path, page):
    """The deck is retiring (spec section 5), and /triage is a 404 in production."""
    bar = _site_bar(_issue_pages(tmp_path)[page])

    assert "Triage" not in bar
    assert "/triage" not in bar


def test_the_old_masthead_brand_and_meta_strip_are_gone(tmp_path):
    pages = _issue_pages(tmp_path)

    assert 'class="mast-row"' not in pages["digest"]
    assert 'class="meta-strip"' not in pages["digest"]
    assert ".meta-strip" not in parse_style_block(pages["digest"])
    for html in pages.values():
        assert "<strong>CYRIS</strong> // " not in html


def test_the_digest_switches_to_its_raw_page_from_the_issue_bar(tmp_path):
    digest = _issue_pages(tmp_path)["digest"]

    assert '<span class="data">2026-04-15</span><span class="label">evening</span>' in digest
    assert '<a href="2026-04-15-evening.html" aria-current="page">Digest</a>' in digest
    assert '<a href="2026-04-15-evening-raw.html">All articles</a>' in digest


def test_a_digest_without_a_raw_page_offers_no_all_articles(tmp_path):
    """The raw page is only emitted when the run collected something; no dead link."""
    content = DigestContent(
        date="2026-04-15",
        period="evening",
        sources_processed=1,
        articles_received=1,
        articles_included=1,
        usage=UsageStats(),
    )

    digest = HtmlDigestWriter(tmp_path).render(content)

    assert '<a href="2026-04-15-evening.html" aria-current="page">Digest</a>' in digest
    assert ">All articles</a>" not in digest
    assert "-raw.html" not in digest


def test_the_raw_page_switches_back_to_its_digest_from_the_issue_bar(tmp_path):
    raw = _issue_pages(tmp_path)["raw"]

    assert '<span class="data">2026-04-15</span><span class="label">evening</span>' in raw
    assert '<a href="2026-04-15-evening.html">Digest</a>' in raw
    assert '<a href="2026-04-15-evening-raw.html" aria-current="page">All articles</a>' in raw


def test_the_issue_bar_sits_under_the_site_bar_on_issue_pages_only(tmp_path):
    pages = _issue_pages(tmp_path)

    for page in ("digest", "raw"):
        html = pages[page]
        assert (
            html.index('class="site-bar"')
            < html.index('class="issue-bar"')
            < html.index('class="container"')
        ), page
    assert 'class="issue-bar"' not in pages["index"]


@pytest.mark.parametrize("page", ["digest", "raw"])
def test_an_issue_links_only_to_its_own_two_views(tmp_path, page):
    """No previous or next issue (spec section 5): the reader reads today's issue."""
    html = _issue_pages(tmp_path)[page]

    dated = set(re.findall(r'href="(\d{4}-\d{2}-\d{2}-[^"]*\.html)"', html))
    assert dated == {"2026-04-15-evening.html", "2026-04-15-evening-raw.html"}


def test_the_raw_subtitle_leaves_date_and_period_to_the_issue_bar(tmp_path):
    articles = [_stored(f"Article {n}", "Src") for n in range(3)]
    raw = HtmlDigestWriter(tmp_path).render_raw("2026-04-15", "evening", articles)

    assert "3 articles" in raw
    assert "· EVENING ·" not in raw


@pytest.mark.parametrize(
    ("articles", "label"),
    [
        (
            [("A", "Src A"), ("B", "Src A"), ("C", "Src B")],
            '<span class="label">All articles · 3 articles · 2 sources</span>',
        ),
        ([("A", "Src A")], '<span class="label">All articles · 1 article · 1 source</span>'),
        ([], '<span class="label">All articles · 0 articles · 0 sources</span>'),
    ],
)
def test_the_raw_page_head_counts_articles_and_sources(tmp_path, articles, label):
    stored = [_stored(title, source) for title, source in articles]
    raw = HtmlDigestWriter(tmp_path).render_raw("2026-04-15", "evening", stored)

    assert label in raw


def test_the_raw_page_opens_with_the_spec_page_head(tmp_path):
    raw = _issue_pages(tmp_path)["raw"]

    assert '<h1 class="display">What this run <em>judged</em></h1>' in raw
    assert (
        '<p class="small">Everything the run decided on, plus what is still pending.'
        " The digest shows a selection of it.</p>"
    ) in raw
    assert 'class="masthead"' not in raw
    assert 'class="subtitle"' not in raw


ARCHIVE_PAGE_HEAD = (
    '<span class="label">Archive</span>',
    '<h1 class="display">Every <em>issue</em></h1>',
    '<p class="small">Cyris collects articles from RSS feeds and newsletters and selects the'
    " ones worth reading into a digest. Newest first. Each issue has the digest and the full"
    " list of articles it judged.</p>",
)


def test_the_archive_opens_with_the_spec_page_head(tmp_path):
    html = HtmlDigestWriter(tmp_path).render_index([])

    head = html[html.index('class="container"') :]
    assert head.index('<div class="page-head">') < head.index(ARCHIVE_PAGE_HEAD[0])
    positions = [head.index(part) for part in ARCHIVE_PAGE_HEAD]
    assert positions == sorted(positions)


def test_the_archive_masthead_is_gone(tmp_path):
    html = HtmlDigestWriter(tmp_path).render_index(["2026-04-15-evening.html"])

    assert 'class="masthead"' not in html
    assert 'class="subtitle"' not in html


def _footer(html: str) -> str:
    """Everything from the footer to the page's scripts."""
    return html[html.index('class="footer"') :].split("<script", 1)[0]


@pytest.mark.parametrize("page", ["index", "digest", "raw"])
def test_a_footer_links_only_to_the_project(tmp_path, page):
    html = _issue_pages(tmp_path)[page]

    assert re.findall(r'<a href="([^"]*)"', _footer(html)) == [PROJECT_URL]
    assert _footer(html).count("<a") == 1
    rules = parse_style_block(html)
    assert ".footer a" not in rules
    assert ".footer a:hover" not in rules


def test_the_footers_say_only_what_made_the_page(tmp_path):
    pages = _issue_pages(tmp_path)

    digest_footer = _footer(pages["digest"])
    assert f'// Generated by <a href="{PROJECT_URL}">Cyris</a>' in digest_footer
    assert "tokens" in digest_footer and "api calls" in digest_footer
    assert f'<div class="footer">{CREDIT}</div>' in pages["raw"]


def test_the_archive_footer_counts_its_issues(tmp_path):
    writer = HtmlDigestWriter(tmp_path)
    two = writer.render_index(["2026-04-15-evening.html", "2026-04-15-morning.html"])
    one = writer.render_index(["2026-04-15-evening.html"])

    assert f"{CREDIT} &middot; 2 issues<" in two
    assert f"{CREDIT} &middot; 1 issue<" in one
    assert "local-first" not in two


@pytest.mark.parametrize("tag", ["en", "ja", "zh-Hant", "pt-BR", "zh-Hant-TW"])
def test_a_bcp47_tag_is_its_own_lang(tag):
    assert html_digest._html_lang(tag) == tag


@pytest.mark.parametrize(
    "value", ["Traditional Chinese", "繁體中文", "English", "Deutsch", "auto", ""]
)
def test_anything_else_is_htmls_unknown_language(value):
    assert html_digest._html_lang(value) == ""


def _every_section(output_language: str) -> DigestContent:
    def item(title: str) -> DigestItem:
        return DigestItem(
            title=title,
            summary=f"{title} summary.",
            sources=["Src"],
            urls=[f"https://x.test/{title}"],
        )

    cluster = DigestItem(
        title="Cluster",
        summary="Cluster summary.",
        sources=["A", "B", "C"],
        urls=["https://a.test/1", "https://b.test/1", "https://c.test/1"],
    )
    return DigestContent(
        date="2026-04-15",
        period="morning",
        sources_processed=1,
        articles_received=6,
        articles_included=6,
        output_language=output_language,
        featured_articles=[DigestSection(heading="Features", items=[item("Lead"), item("Second")])],
        news_clusters=[DigestSection(heading="Cluster heading", items=[cluster])],
        fan_sections=[DigestSection(heading="Fan heading", items=[item("Fan")])],
        attention_sections=[
            DigestSection(heading="Radar heading", description="Why.", items=[item("Radar")])
        ],
        filtered_headlines=[item("Wire")],
    )


WRITTEN_TEXT = [
    "Lead",
    "Lead summary.",
    "Second",
    "Second summary.",
    "Cluster heading",
    "Cluster summary.",
    "Wire",
]
PASSED_THROUGH_TEXT = [
    "Fan heading",
    "Fan",
    "Fan summary.",
    "Radar heading",
    "Why.",
    "Radar",
    "Radar summary.",
]
DIGEST_CHROME_TEXT = [
    "Top story",
    "Features",
    "In Focus",
    "Following",
    "On the Radar",
    "The Wire",
    "Sources",
    "Included",
    "3 sources",
]


def _langs_of(langs: dict[str, str | None], texts: list[str]) -> dict[str, str | None]:
    return {text: langs[text] for text in texts}


def test_the_digest_chrome_is_english_and_its_written_content_carries_the_output_language(
    tmp_path,
):
    html = HtmlDigestWriter(tmp_path).render(_every_section("ja"))
    langs = text_langs(html)

    assert '<html lang="en">' in html
    assert _langs_of(langs, DIGEST_CHROME_TEXT) == dict.fromkeys(DIGEST_CHROME_TEXT, "en")
    assert _langs_of(langs, WRITTEN_TEXT) == dict.fromkeys(WRITTEN_TEXT, "ja")


def test_untranslated_sections_leave_their_language_unknown(tmp_path):
    langs = text_langs(HtmlDigestWriter(tmp_path).render(_every_section("ja")))

    assert _langs_of(langs, PASSED_THROUGH_TEXT) == dict.fromkeys(PASSED_THROUGH_TEXT, "")


def test_every_vote_group_says_its_titles_are_english(tmp_path):
    html = HtmlDigestWriter(tmp_path).render(_every_section("ja"))
    groups = re.findall(r'<span class="vote-group"[^>]*>', html)

    assert len(groups) == 6
    assert all(' lang="en"' in group for group in groups)


def test_a_plain_language_name_leaves_the_content_language_unknown(tmp_path):
    html = HtmlDigestWriter(tmp_path).render(_every_section("Traditional Chinese"))
    langs = text_langs(html)

    assert "Traditional Chinese" not in html
    assert _langs_of(langs, WRITTEN_TEXT) == dict.fromkeys(WRITTEN_TEXT, "")
    assert langs["Top story"] == "en"


def test_the_archive_card_carries_its_issues_language(tmp_path):
    html = HtmlDigestWriter(tmp_path).render_index(
        ["2026-04-15-morning.html", "2026-04-14-morning.html"], content=_every_section("ko")
    )
    langs = text_langs(html)

    assert '<html lang="en">' in html
    assert langs["Lead"] == "ko"
    assert langs["Cluster heading"] == "ko"
    assert langs["Latest"] == langs["2026-04-14"] == "en"


def test_a_raw_title_has_no_reliable_language_so_it_is_unknown(tmp_path):
    scored = _stored("中文標題", "Src").model_copy(update={"language": "zh"})
    unscored = _stored("Unscored title", "Src")
    html = HtmlDigestWriter(tmp_path).render_raw("2026-04-15", "morning", [scored, unscored])
    langs = text_langs(html)

    assert '<html lang="en">' in html
    assert langs["中文標題"] == langs["Unscored title"] == ""
    assert langs["Src"] == "en"
    assert "title.lang = next.querySelector('a').lang;" in html
