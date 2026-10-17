"""The digest as an email body: one column, two palettes, nothing a mail client drops."""

import json
import re
from pathlib import Path

import pytest
from css_rules import text_langs

from cyris.adapters.output.email_digest import (
    LIGHT_PALETTE,
    PALETTE_FILE,
    render_digest_email,
    tokens,
)
from cyris.adapters.output.html_digest import PROJECT_URL
from cyris.domain.models import (
    NO_LLM_MODEL,
    DigestContent,
    DigestItem,
    DigestSection,
    UsageStats,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]
DIGEST_URL = "https://digest.example.org/2026-09-27-morning"
GROUNDS = ["bg", "bg-elev", "surface", "surface-2"]
SPACING_SCALE = {"4px", "8px", "12px", "16px", "20px", "24px", "32px", "48px", "64px", "80px"}


def _item(title: str, url: str, source: str, score: float | None = None) -> DigestItem:
    return DigestItem(
        title=title, summary=f"{title} summary.", sources=[source], urls=[url], score=score
    )


def _full() -> DigestContent:
    return DigestContent(
        date="2026-09-27",
        period="morning",
        sources_processed=12,
        articles_received=300,
        articles_included=15,
        usage=UsageStats(input_tokens=1000, output_tokens=500, api_calls=3, model="m"),
        featured_articles=[
            DigestSection(
                heading="Lead group",
                items=[
                    _item("Lead Story", "https://lead.test/a", "Source A", score=91),
                    _item("Second Feature", "https://feature.test/b", "Source B", score=80),
                ],
            )
        ],
        news_clusters=[
            DigestSection(
                heading="A Cluster",
                items=[
                    DigestItem(
                        title="c",
                        summary="Cluster summary.",
                        sources=["News A", "News B"],
                        urls=["https://news.test/1", "https://news.test/2"],
                    )
                ],
            )
        ],
        fan_sections=[
            DigestSection(heading="A Group", items=[_item("Fan Item", "https://fan.test/1", "Fan")])
        ],
        attention_sections=[
            DigestSection(
                heading="Watching",
                description="Why these.",
                items=[_item("Radar Item", "https://radar.test/1", "Blog")],
            )
        ],
        filtered_headlines=[
            _item("Wire One", "https://wire.test/1", "Wire"),
            _item("No Link", "newsletter:abc", "Letter"),
        ],
    )


def _style(html: str) -> str:
    return "".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S))


class TestWhatAMailClientKeeps:
    def test_no_script_no_css_variable_and_both_color_scheme_tags(self):
        html = render_digest_email(_full(), DIGEST_URL, raw_page=True)

        assert "<script" not in html
        assert "var(" not in html
        assert '<meta name="color-scheme" content="light dark">' in html
        assert '<meta name="supported-color-schemes" content="light dark">' in html

    def test_the_style_carries_the_font_stacks_unescaped(self):
        style = _style(render_digest_email(_full(), DIGEST_URL))

        assert "&#39;" not in style
        assert f"font-family: {tokens()['font-serif']};" in style

    def test_every_link_is_absolute(self):
        html = render_digest_email(_full(), DIGEST_URL, raw_page=True)

        hrefs = re.findall(r'href="([^"]*)"', html)
        assert hrefs
        assert [h for h in hrefs if not h.startswith(("https://", "http://"))] == []

    def test_a_synthetic_url_is_never_a_link(self):
        html = render_digest_email(_full(), DIGEST_URL)

        assert "newsletter:abc" not in html
        assert "No Link" in html

    def test_feed_text_is_escaped(self):
        content = _full()
        content.filtered_headlines[0].title = "<script>alert(1)</script>"

        html = render_digest_email(content, DIGEST_URL)

        assert "<script>alert" not in html
        assert "&lt;script&gt;" in html


MARKER = re.compile(r'<h2 class="section-tag"><span class="rule"></span>([^<]*)</h2>')


def _placed(area_order: list[str], tech: str | None = "Tech") -> DigestContent:
    def item(title: str, area: str | None) -> DigestItem:
        url = f"https://{title.lower().replace(' ', '-')}.test/"
        return _item(title, url, "Src").model_copy(update={"area": area})

    return DigestContent(
        date="2026-09-27",
        period="morning",
        sources_processed=3,
        articles_received=9,
        articles_included=4,
        usage=UsageStats(input_tokens=1, output_tokens=1, api_calls=1, model="m"),
        area_order=area_order,
        featured_articles=[
            DigestSection(heading="Lead", items=[item("Lead Story", None)]),
            DigestSection(heading="Feature", items=[item("Tech Feature", tech)]),
        ],
        fan_sections=[DigestSection(heading="Fans", items=[item("Health Fan", "Health")])],
        attention_sections=[DigestSection(heading="Watch", items=[item("Gone Radar", "Retired")])],
        filtered_headlines=[item("Tech Wire", tech)],
    )


class TestSections:
    def test_each_area_is_a_marker_in_its_stored_order_after_the_top_story(self):
        html = render_digest_email(_placed(["Tech", "Health"]), DIGEST_URL)

        assert MARKER.findall(html) == ["Top story", "Tech", "Health", "Other"]
        assert html.index(">Lead Story<") < html.index(">Tech Feature<")
        assert html.index(">Tech Feature<") < html.index(">Tech Wire<") < html.index(">Health Fan<")
        assert html.index(">Health Fan<") < html.index(">Gone Radar<")

    def test_an_area_lists_its_cards_then_follows_then_clusters_then_rows(self):
        def tech(title: str) -> DigestItem:
            url = f"https://{title.lower().replace(' ', '-')}.test/"
            return _item(title, url, "Src").model_copy(update={"area": "Tech"})

        content = _placed(["Tech"])
        content.fan_sections = [DigestSection(heading="Fans", items=[tech("Tech Fan")])]
        content.news_clusters = [
            DigestSection(
                heading="Cluster Heading", items=[tech("Cluster One"), tech("Cluster Two")]
            )
        ]
        content.filtered_headlines = [tech("Tech Wire")]

        html = render_digest_email(content, DIGEST_URL)

        positions = [
            html.index(">Tech Feature<"),
            html.index(">Tech Fan<"),
            html.index(">Cluster Heading<"),
            html.index(">Tech Wire<"),
        ]
        assert positions == sorted(positions)

    def test_an_issue_with_no_area_order_has_no_area_marker_and_every_entry(self):
        html = render_digest_email(_placed([], tech=None), DIGEST_URL)

        assert MARKER.findall(html) == ["Top story"]
        for title in ["Tech Feature", "Tech Wire", "Health Fan", "Gone Radar"]:
            assert f">{title}<" in html

    def test_an_unplaced_entry_closes_the_issue_under_other(self):
        html = render_digest_email(_placed(["Tech", "Health"]), DIGEST_URL)

        assert MARKER.findall(html)[-1] == "Other"

    def test_an_area_name_is_escaped(self):
        html = render_digest_email(_placed(["<b>Tech</b>"], tech="<b>Tech</b>"), DIGEST_URL)

        assert "<b>Tech</b>" not in html
        assert "&lt;b&gt;Tech&lt;/b&gt;" in html

    def test_an_empty_issue_has_no_section_marker(self):
        html = render_digest_email(_issue(), DIGEST_URL)

        assert MARKER.findall(html) == []
        assert EMPTY_SENTENCE in html

    def test_tracked_topics_are_not_in_the_mail(self):
        content = _placed(["Tech"]).model_copy(
            update={
                "tracked_topics": [
                    DigestSection(
                        heading="Watched Topic", items=[_item("Topic Item", "https://t.test/", "T")]
                    )
                ]
            }
        )
        html = render_digest_email(content, DIGEST_URL)

        assert "Tracking" not in html
        assert "Watched Topic" not in html

    def test_the_lead_carries_its_score_and_the_article_links(self):
        html = render_digest_email(_full(), DIGEST_URL)

        assert "91.0" in html
        for url in ["https://lead.test/a", "https://news.test/2", "https://wire.test/1"]:
            assert f'href="{url}"' in html

    def test_a_group_prints_its_summary_once_and_links_each_article(self):
        content = _full()
        group = content.featured_articles[0]
        for item in group.items:
            item.summary = "Shared summary."
        content.featured_articles = [group.model_copy(update={"summary": "Shared summary."})]

        html = render_digest_email(content, DIGEST_URL)

        assert html.count("Shared summary.") == 1
        assert "91.0" in html and "80.0" in html
        assert ">Lead group</h3>" in html
        for title, url in [
            ("Lead Story", "https://lead.test/a"),
            ("Second Feature", "https://feature.test/b"),
        ]:
            assert f'<a href="{url}">{title}</a>' in html

    def test_a_wire_row_carries_its_summary_and_one_without_is_its_title_alone(self):
        content = _full()
        content.filtered_headlines[1].summary = ""

        html = render_digest_email(content, DIGEST_URL)
        wire = html.split('<ol class="wire"', 1)[1].split("</ol>", 1)[0]

        title = '<a href="https://wire.test/1">Wire One</a>'
        assert f'{title}<p class="summary">Wire One summary.</p></li>' in wire
        assert '<li lang=""><span class="idx">003</span>No Link</li>' in wire


def _entry(title: str, summary: str, area: str | None = None) -> DigestItem:
    return DigestItem(
        title=title,
        summary=summary,
        sources=["Src"],
        urls=[f"https://{title.lower().replace(' ', '-')}.test/"],
        area=area,
    )


def _top_story_block(html: str) -> str:
    start = html.index('<h2 class="section-tag"><span class="rule"></span>Top story</h2>')
    return html[start : html.index("</section>", start)]


class TestAreaSkeletonLayout:
    def test_the_top_story_block_holds_every_story_of_the_headline(self):
        headline = DigestSection(
            heading="Top",
            summary=None,
            items=[_entry(title, f"{title} summary.") for title in ("X1", "X2", "X3")],
        )
        content = DigestContent(
            date="2026-09-27",
            period="morning",
            sources_processed=1,
            articles_received=4,
            articles_included=4,
            usage=UsageStats(),
            featured_articles=[
                headline,
                DigestSection(heading="F", items=[_entry("Tech Feature", "F.", area="Tech")]),
            ],
            area_order=["Tech"],
        )

        html = render_digest_email(content, DIGEST_URL)
        block = _top_story_block(html)

        for title in ("X1", "X2", "X3"):
            assert f">{title}<" in block
        assert MARKER.findall(block) == ["Top story"]
        assert MARKER.findall(html) == ["Top story", "Tech"]

    def test_a_radar_row_is_untranslated_and_a_headline_row_takes_the_output_language(self):
        content = DigestContent(
            date="2026-09-27",
            period="morning",
            sources_processed=1,
            articles_received=2,
            articles_included=2,
            usage=UsageStats(),
            output_language="en",
            area_order=["Tech"],
            attention_sections=[
                DigestSection(heading="Radar", items=[_entry("Radar Row", "R.", area="Tech")])
            ],
            filtered_headlines=[_entry("Wire Row", "W.", area="Tech")],
        )

        html = render_digest_email(content, DIGEST_URL)

        assert re.findall(r'<li lang="([^"]*)"', html) == ["", "en"]


class TestLanguage:
    CONTENT = [
        "Lead Story summary.",
        "A Cluster",
        "Cluster summary.",
        "Wire One",
        "Wire One summary.",
    ]
    PASSED_THROUGH = ["Fan Item"]
    TITLES = ["Lead Story", "Second Feature"]
    CHROME = ["Top story"]

    def test_the_chrome_is_english_and_the_content_carries_the_output_language(self):
        content = _full().model_copy(update={"output_language": "ja"})
        html = render_digest_email(content, DIGEST_URL)
        langs = text_langs(html)

        assert '<html lang="en">' in html
        assert {text: langs[text] for text in self.CHROME} == dict.fromkeys(self.CHROME, "en")
        assert {text: langs[text] for text in self.CONTENT} == dict.fromkeys(self.CONTENT, "ja")

    def test_an_articles_own_title_leaves_its_language_unknown_beside_its_summary(self):
        content = _full().model_copy(update={"output_language": "ja"})
        langs = text_langs(render_digest_email(content, DIGEST_URL))

        assert {text: langs[text] for text in self.TITLES} == dict.fromkeys(self.TITLES, "")

    def test_a_grouped_top_story_keeps_its_heading_and_leaves_its_titles_unknown(self):
        content = _full().model_copy(update={"output_language": "ja"})
        group = content.featured_articles[0]
        content.featured_articles = [group.model_copy(update={"summary": "Shared summary."})]
        langs = text_langs(render_digest_email(content, DIGEST_URL))

        written = ["Lead group", "Shared summary."]
        assert {text: langs[text] for text in written} == dict.fromkeys(written, "ja")
        assert {text: langs[text] for text in self.TITLES} == dict.fromkeys(self.TITLES, "")

    @staticmethod
    def _passed_through(*titles: str) -> DigestContent:
        content = _full().model_copy(update={"output_language": "ja"})
        for item in (*content.featured_articles[0].items, *content.filtered_headlines):
            item.passed_through = item.title in titles
        return content

    def test_an_excerpt_fallback_leaves_its_language_unknown_beside_written_text(self):
        content = self._passed_through("Second Feature", "Wire One")
        langs = text_langs(render_digest_email(content, DIGEST_URL))

        fallback = ["Second Feature", "Second Feature summary.", "Wire One", "Wire One summary."]
        written = ["Lead Story summary.", "A Cluster", "Cluster summary."]
        assert {text: langs[text] for text in fallback} == dict.fromkeys(fallback, "")
        assert {text: langs[text] for text in written} == dict.fromkeys(written, "ja")

    def test_a_top_story_from_an_excerpt_fallback_leaves_its_language_unknown(self):
        langs = text_langs(render_digest_email(self._passed_through("Lead Story"), DIGEST_URL))

        lead = ["Lead Story", "Lead Story summary."]
        assert {text: langs[text] for text in lead} == dict.fromkeys(lead, "")
        assert {text: langs[text] for text in ["Second Feature summary.", "Wire One"]} == {
            "Second Feature summary.": "ja",
            "Wire One": "ja",
        }

    def test_untranslated_sections_leave_their_language_unknown(self):
        content = _full().model_copy(update={"output_language": "ja"})
        langs = text_langs(render_digest_email(content, DIGEST_URL))

        assert {text: langs[text] for text in self.PASSED_THROUGH} == dict.fromkeys(
            self.PASSED_THROUGH, ""
        )

    @staticmethod
    def _preheader_langs(content: DigestContent) -> dict[str, str | None]:
        html = render_digest_email(content, DIGEST_URL)
        preheader = re.search(r'<div class="preheader">.*?</div>', html, re.S).group()
        return text_langs(f'<div lang="en">{preheader}</div>')

    def test_the_preheader_leaves_a_single_leads_own_title_unknown(self):
        content = _full().model_copy(update={"output_language": "ja"})

        assert self._preheader_langs(content) == {"Lead Story": "", "· Kept 15 of 300": "en"}

    def test_the_preheader_keeps_a_written_heading_in_the_output_language(self):
        grouped = _full().model_copy(update={"output_language": "ja"})
        group = grouped.featured_articles[0]
        grouped.featured_articles = [group.model_copy(update={"summary": "Shared summary."})]
        no_lead = _full().model_copy(update={"output_language": "ja", "featured_articles": []})

        assert self._preheader_langs(grouped)["Lead group"] == "ja"
        assert self._preheader_langs(no_lead)["A Cluster"] == "ja"

    def test_a_plain_language_name_leaves_the_content_language_unknown(self):
        content = _full().model_copy(update={"output_language": "Traditional Chinese"})
        html = render_digest_email(content, DIGEST_URL)
        langs = text_langs(html)

        assert "Traditional Chinese" not in html
        assert {text: langs[text] for text in self.CONTENT} == dict.fromkeys(self.CONTENT, "")


class TestSiteLinks:
    def test_the_issue_links_to_its_page_raw_page_archive_and_settings(self):
        html = render_digest_email(_full(), DIGEST_URL, raw_page=True)

        for url in [
            DIGEST_URL,
            "https://digest.example.org/2026-09-27-morning-raw",
            "https://digest.example.org/",
            "https://digest.example.org/settings",
        ]:
            assert f'href="{url}"' in html

    def test_no_raw_page_means_no_raw_link(self):
        html = render_digest_email(_full(), DIGEST_URL, raw_page=False)

        assert "-raw" not in html

    def test_a_failed_publish_says_so_and_links_no_site_page(self):
        html = render_digest_email(_full(), "", publish_failed=True)

        assert "Publishing the online edition failed" in html
        assert "digest.example.org" not in html

    @pytest.mark.parametrize("digest_url", [DIGEST_URL, ""])
    def test_the_footer_credits_the_project_whether_or_not_it_published(self, digest_url):
        html = render_digest_email(_full(), digest_url, publish_failed=not digest_url)

        footer = html[html.index('<footer class="footer">') :]
        assert f'// Generated by <a href="{PROJECT_URL}">Cyris</a>' in footer


def _issue(articles_included: int = 0) -> DigestContent:
    return DigestContent(
        date="2026-09-27",
        period="morning",
        sources_processed=2,
        articles_received=7,
        articles_included=articles_included,
        usage=UsageStats(model=NO_LLM_MODEL),
    )


EMPTY_SENTENCE = "this run judged the 7 articles it received and kept none"
DEGRADED_NOTICE = '<p class="notice">Some or all of this issue is unscored or plain excerpts'


class TestEmptyAndDegraded:
    def test_an_empty_issue_says_why_and_links_all_articles(self):
        html = render_digest_email(_issue(), DIGEST_URL, raw_page=True)

        sentence = html[html.index('<p class="desc">') :]
        sentence = sentence[: sentence.index("</p>")]
        assert EMPTY_SENTENCE in sentence
        assert '<a href="https://digest.example.org/2026-09-27-morning-raw">All articles</a>' in (
            sentence
        )

    @pytest.mark.parametrize(
        ("digest_url", "raw_page"), [(DIGEST_URL, False), ("", True)], ids=["no-raw", "no-site"]
    )
    def test_an_empty_issue_without_a_raw_page_links_nothing(self, digest_url, raw_page):
        html = render_digest_email(_issue(), digest_url, raw_page=raw_page)

        assert EMPTY_SENTENCE in html
        assert "-raw" not in html

    def test_an_issue_with_articles_has_no_empty_sentence(self):
        html = render_digest_email(_full(), DIGEST_URL, raw_page=True)

        assert "kept none" not in html

    @pytest.mark.parametrize("degraded", [True, False])
    def test_only_a_degraded_issue_carries_the_warn_notice(self, degraded):
        html = render_digest_email(_issue(articles_included=3), DIGEST_URL, degraded=degraded)

        assert (DEGRADED_NOTICE in html) is degraded


class TestPalettes:
    def test_light_is_the_default_and_dark_follows_the_reader(self):
        style = _style(render_digest_email(_full(), DIGEST_URL))
        default, dark = style.split("@media (prefers-color-scheme: dark)", 1)

        assert LIGHT_PALETTE["bg"] in default
        assert tokens()["bg"] in dark
        assert tokens()["bg"] not in default

    def test_light_names_every_colour_the_page_tokens_name(self):
        colours = {k for k, v in tokens().items() if v.startswith(("#", "rgba("))}

        assert set(LIGHT_PALETTE) - {"accent-hover"} == colours

    def test_the_light_palette_is_the_one_the_spec_lists(self):
        spec = (REPO / "docs/design/ui-language.md").read_text(encoding="utf-8")
        listed = dict(re.findall(r"^\| `--([\w-]+)` \| `([^`]+)` \|", spec, re.M))

        assert listed == LIGHT_PALETTE
        assert json.loads(PALETTE_FILE.read_text(encoding="utf-8"))["light"] == LIGHT_PALETTE

    @pytest.mark.parametrize("ground", GROUNDS)
    @pytest.mark.parametrize("ink", ["text", "text-dim", "accent", "warn"])
    def test_light_text_reads_on_its_grounds(self, ink, ground):
        assert _contrast(LIGHT_PALETTE[ink], LIGHT_PALETTE[ground]) >= 4.5

    @pytest.mark.parametrize("ground", GROUNDS)
    def test_the_light_faint_mark_shows_on_its_grounds(self, ground):
        assert _contrast(LIGHT_PALETTE["text-faint"], LIGHT_PALETTE[ground]) >= 3

    # A feature's pill sits on the page, the lead's on its card.
    @pytest.mark.parametrize("ground", ["bg", "surface"])
    def test_the_score_pill_reads_on_its_tint(self, ground):
        tint = _over(LIGHT_PALETTE["accent-tint"], LIGHT_PALETTE[ground])

        assert _contrast(LIGHT_PALETTE["accent"], tint) >= 4.5

    @pytest.mark.parametrize("palette", ["light", "dark"])
    def test_no_text_takes_the_faint_colour(self, palette):
        faint = (LIGHT_PALETTE if palette == "light" else tokens())["text-faint"]
        style = _style(render_digest_email(_full(), DIGEST_URL, raw_page=True))

        assert re.findall(rf"(?<![\w-])color:\s*{faint}", style) == []

    def test_the_primary_button_label_reads_on_the_light_accent(self):
        assert _contrast(LIGHT_PALETTE["bg"], LIGHT_PALETTE["accent"]) >= 4.5


class TestLayout:
    def test_a_wire_summary_breaks_an_unbroken_token(self):
        style = _style(render_digest_email(_full(), DIGEST_URL))

        assert re.search(r"\.wire \.summary \{[^}]*overflow-wrap: anywhere;", style)

    def test_every_spacing_length_is_on_the_scale(self):
        style = _style(render_digest_email(_full(), DIGEST_URL, raw_page=True))
        # The pill's padding is the one value the spec sets outside the scale (§4).
        style = style.replace("padding: 2px 10px;", "")
        spacing = re.findall(r"(?:margin|padding|gap)[\w-]*\s*:\s*([^;}]+)", style)

        lengths = {v for value in spacing for v in value.split() if v != "0" and v != "auto"}
        assert lengths - SPACING_SCALE == set()

    def test_a_full_issue_stays_under_gmails_clipping_line(self):
        html = render_digest_email(_full(), DIGEST_URL, raw_page=True)

        assert len(html.encode()) < 100_000


def _luminance(colour: str) -> float:
    channels = [int(colour.lstrip("#")[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(a: str, b: str) -> float:
    high, low = sorted([_luminance(a), _luminance(b)], reverse=True)
    return (high + 0.05) / (low + 0.05)


def _over(tint: str, ground: str) -> str:
    """An ``rgba()`` tint laid over an opaque hex ground, as the hex a reader sees."""
    *rgb, alpha = (float(v) for v in re.findall(r"[\d.]+", tint))
    base = [int(ground.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)]
    blended = (round(c * alpha + b * (1 - alpha)) for c, b in zip(rgb, base, strict=True))
    return "#" + "".join(f"{c:02x}" for c in blended)


# Every place a pill can show: a grouped Top story and its members, a single Top story and
# the Features beside it, and an area's Features. Each scored 0.0, beside one unscored card.
SCORED_ZERO_ISSUES = [
    [
        DigestSection(
            heading="G",
            summary="One.",
            items=[
                _item("A", "https://t.test/a", "S", 0.0),
                _item("B", "https://t.test/b", "S", 0.0),
            ],
        ),
        DigestSection(
            heading="F",
            items=[_item("C", "https://t.test/c", "S", 0.0), _item("U", "https://t.test/u", "S")],
        ),
    ],
    [
        DigestSection(
            heading="H",
            items=[
                _item("L", "https://t.test/l", "S", 0.0),
                _item("R", "https://t.test/r", "S", 0.0),
            ],
        ),
        DigestSection(heading="F", items=[_item("U", "https://t.test/u", "S")]),
    ],
]


@pytest.mark.parametrize(("featured", "pills"), zip(SCORED_ZERO_ISSUES, [4, 2], strict=True))
def test_a_score_of_zero_shows_its_pill(featured, pills):
    content = _full().model_copy(update={"featured_articles": featured})
    html = render_digest_email(content, DIGEST_URL)
    assert html.count('<span class="pill">0.0</span>') == pills
    assert html.count('class="pill"') == pills
