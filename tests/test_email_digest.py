"""The digest as an email body: one column, two palettes, nothing a mail client drops."""

import json
import re
from pathlib import Path

import pytest
from css_rules import text_langs
from layered_issue import (
    UNNAMED_SOURCES,
    four_layer_issue,
    topics_only_issue,
    water_only_issue,
)

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
from cyris.i18n import translator

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
        assert f"font-family: {tokens()['font-sans']};" in style

    def test_every_link_is_absolute(self):
        html = render_digest_email(_full(), DIGEST_URL, raw_page=True)

        hrefs = re.findall(r'href="([^"]*)"', html)
        assert hrefs
        assert [h for h in hrefs if not h.startswith(("https://", "http://"))] == []

    def test_a_synthetic_url_is_never_a_link(self):
        content = _full()
        content.fan_sections[0].items[0].urls = ["newsletter:abc"]

        html = render_digest_email(content, DIGEST_URL)

        assert "newsletter:abc" not in html
        assert "Fan Item" in html

    def test_feed_text_is_escaped(self):
        content = _full()
        content.featured_articles[0].items[0].title = "<script>alert(1)</script>"

        html = render_digest_email(content, DIGEST_URL)

        assert "<script>alert" not in html
        assert "&lt;script&gt;" in html


MARKER = re.compile(r'<h2 class="section-tag"><span class="rule"></span>([^<]*)</h2>')


def _section(html: str, label: str) -> str:
    start = html.index(f'<span class="rule"></span>{label}</h2>')
    return html[start : html.index("</section>", start)]


class TestSections:
    def test_tracking_then_stories_and_topics_in_the_pages_order(self):
        html = render_digest_email(four_layer_issue(), DIGEST_URL)

        assert MARKER.findall(html) == ["Tracking", "Stories", "Topics"]

    def test_the_headlines_layer_is_left_to_the_page(self):
        html = render_digest_email(four_layer_issue(), DIGEST_URL)

        for text in ("One report", "A headline", "The filter's sentence.", "headline.example"):
            assert text not in html, text

    def test_an_empty_layer_has_no_marker(self):
        content = four_layer_issue().model_copy(update={"news_clusters": [], "tracked_topics": []})

        assert MARKER.findall(render_digest_email(content, DIGEST_URL)) == ["Stories"]

    def test_an_empty_issue_has_no_section_marker(self):
        html = render_digest_email(_issue(), DIGEST_URL)

        assert MARKER.findall(html) == []
        assert EMPTY_SENTENCE in html

    def test_tracking_lists_each_hit_as_a_link_alone(self):
        tracking = _section(render_digest_email(four_layer_issue(), DIGEST_URL), "Tracking")

        assert ">Tracked topic</h3>" in tracking
        assert '<a href="https://tracked.example.test/hit">A tracked hit</a>' in tracking
        assert "Not shown." not in tracking and "SRC-TRACKED" not in tracking

    def test_a_pebble_is_its_title_link_and_summary(self):
        stories = _section(render_digest_email(four_layer_issue(), DIGEST_URL), "Stories")

        assert '<a href="https://pebble.example.test/read">A long read</a>' in stories
        assert '<p class="summary">Why it matters.</p>' in stories
        assert '<a href="https://fan.example.test/post">A followed post</a>' in stories

    @pytest.mark.parametrize("language", ["en", "zh-Hant"])
    def test_a_sand_block_is_its_heading_and_its_whole_line_with_no_member_link(self, language):
        line = (
            "兩篇報導說的是同一件事。" * 80
            if language == "zh-Hant"
            else " ".join(["Both agree."] * 400)
        )
        label = translator(language)("layer.topics")

        html = render_digest_email(topics_only_issue(language, line), DIGEST_URL)

        topics = _section(html, label)
        assert ">Topic 0</h3>" in topics
        assert f'<p class="summary" lang="">{line}</p>' in topics
        assert "<a " not in topics
        for text in ("Report a", "Report b", "wire-a.example", "wire-b.example"):
            assert text not in topics, text

    def test_no_source_is_named(self):
        html = render_digest_email(four_layer_issue(), DIGEST_URL)

        for source in UNNAMED_SOURCES:
            assert source not in html, source

    def test_every_item_carries_its_link(self):
        html = render_digest_email(_full(), DIGEST_URL)

        for url in ["https://lead.test/a", "https://fan.test/1"]:
            assert f'href="{url}"' in html

    def test_a_radar_pebble_is_untranslated(self):
        content = DigestContent(
            date="2026-09-27",
            period="morning",
            sources_processed=1,
            articles_received=2,
            articles_included=2,
            usage=UsageStats(),
            output_language="en",
            attention_sections=[
                DigestSection(heading="Radar", items=[_item("Radar Row", "https://r.test/", "Src")])
            ],
        )

        html = render_digest_email(content, DIGEST_URL)

        assert re.findall(r'<article class="item" lang="([^"]*)"', html) == [""]


class TestLanguage:
    CONTENT = [
        "Lead Story summary.",
        "A Cluster",
        "Cluster summary.",
    ]
    PASSED_THROUGH = ["Fan Item"]
    TITLES = ["Lead Story", "Second Feature"]
    CHROME = ["Stories"]

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

    def test_a_grouped_section_is_one_pebble_per_article_under_its_own_summary(self):
        content = _full().model_copy(update={"output_language": "ja"})
        group = content.featured_articles[0]
        content.featured_articles = [group.model_copy(update={"summary": "Shared summary."})]
        html = render_digest_email(content, DIGEST_URL)
        langs = text_langs(html)

        assert "Shared summary." not in html and "Lead group" not in html
        assert {text: langs[text] for text in self.TITLES} == dict.fromkeys(self.TITLES, "")
        assert langs["Second Feature summary."] == "ja"

    @staticmethod
    def _passed_through(*titles: str) -> DigestContent:
        content = _full().model_copy(update={"output_language": "ja"})
        for item in (*content.featured_articles[0].items, *content.filtered_headlines):
            item.passed_through = item.title in titles
        return content

    def test_an_excerpt_fallback_leaves_its_language_unknown_beside_written_text(self):
        content = self._passed_through("Second Feature")
        langs = text_langs(render_digest_email(content, DIGEST_URL))

        fallback = ["Second Feature", "Second Feature summary."]
        written = ["Lead Story summary.", "A Cluster", "Cluster summary."]
        assert {text: langs[text] for text in fallback} == dict.fromkeys(fallback, "")
        assert {text: langs[text] for text in written} == dict.fromkeys(written, "ja")

    def test_a_top_story_from_an_excerpt_fallback_leaves_its_language_unknown(self):
        langs = text_langs(render_digest_email(self._passed_through("Lead Story"), DIGEST_URL))

        lead = ["Lead Story", "Lead Story summary."]
        assert {text: langs[text] for text in lead} == dict.fromkeys(lead, "")
        assert langs["Second Feature summary."] == "ja"

    def test_untranslated_sections_leave_their_language_unknown(self):
        content = _full().model_copy(update={"output_language": "ja"})
        langs = text_langs(render_digest_email(content, DIGEST_URL))

        assert {text: langs[text] for text in self.PASSED_THROUGH} == dict.fromkeys(
            self.PASSED_THROUGH, ""
        )

    @pytest.mark.parametrize(
        ("passed_through", "lang"), [(False, "ja"), (True, "")], ids=["explained", "excerpt"]
    )
    def test_a_follow_pebble_is_tagged_by_what_wrote_its_text(self, passed_through, lang):
        content = _full().model_copy(update={"output_language": "ja"})
        content.fan_sections[0].items[0].passed_through = passed_through
        langs = text_langs(render_digest_email(content, DIGEST_URL))

        assert langs["Fan Item summary."] == lang
        assert langs["Fan Item"] == ""

    @staticmethod
    def _preheader_langs(content: DigestContent) -> dict[str, str | None]:
        html = render_digest_email(content, DIGEST_URL)
        preheader = re.search(r'<div class="preheader">.*?</div>', html, re.S).group()
        return text_langs(f'<div lang="en">{preheader}</div>')

    def test_the_preheader_leaves_a_single_leads_own_title_unknown(self):
        content = _full().model_copy(update={"output_language": "ja"})

        assert self._preheader_langs(content) == {"Lead Story": "", "· Kept 15 of 300": "en"}

    def test_with_no_pebble_the_preheader_is_a_topics_written_heading(self):
        no_pebble = _full().model_copy(
            update={
                "output_language": "ja",
                "featured_articles": [],
                "fan_sections": [],
                "attention_sections": [],
            }
        )

        assert self._preheader_langs(no_pebble)["A Cluster"] == "ja"

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

    def test_a_read_online_button_closes_the_issue_above_the_footer(self):
        html = render_digest_email(_full(), DIGEST_URL)

        button = f'<a class="btn primary" href="{DIGEST_URL}">Read online</a>'
        assert html.count(button) == 2
        assert html.index("Topics</h2>") < html.rindex(button) < html.index("<footer")

    def test_a_failed_publish_has_no_read_online_button(self):
        assert "Read online" not in render_digest_email(_full(), "", publish_failed=True)

    def test_the_footer_gives_the_runs_cost(self):
        content = _full()
        content.usage = UsageStats(
            input_tokens=1_000_000, output_tokens=200_000, api_calls=3, model="claude-haiku-4-5"
        )

        html = render_digest_email(content, DIGEST_URL)

        assert "3 api calls · cost $2.0000</div>" in html[html.index("<footer") :]

    def test_the_footer_gives_no_cost_for_a_model_with_no_known_price(self):
        html = render_digest_email(_full(), DIGEST_URL)

        assert "cost" not in html[html.index("<footer") :]


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


def test_the_mail_shows_no_story_image():
    content = _full()
    for section in content.featured_articles:
        for item in section.items:
            item.image_url = "https://img.example.test/story.jpg"

    html = render_digest_email(content, DIGEST_URL)

    assert "<img" not in html
    assert "img.example.test" not in html


def test_no_item_shows_a_score_pill_as_on_the_page():
    content = _full()
    content.featured_articles[0].items[0].score = 0.0
    html = render_digest_email(content, DIGEST_URL)

    assert 'class="pill"' not in html.split("</style>", 1)[1]
    assert "91.0" not in html and "80.0" not in html


def _paragraph_issue():
    from cyris.domain.models import DigestContent, DigestItem, NewsParagraph

    pair = DigestItem(
        title="Quake",
        summary="",
        sources=["Wire A", "Wire B"],
        urls=["https://a.test/quake", "https://b.test/quake"],
        area="World",
    )
    letter = DigestItem(
        title="Letter",
        summary="",
        sources=["Letters"],
        urls=["newsletter:abc"],
        area="World",
    )
    return DigestContent(
        date="2026-10-27",
        period="morning",
        sources_processed=2,
        articles_received=3,
        articles_included=3,
        area_order=["World"],
        news_paragraphs=[
            NewsParagraph(text="A quake struck; a letter followed.", units=[pair, letter])
        ],
    )


def test_a_legacy_news_paragraph_in_the_mail_takes_the_layers_of_its_units() -> None:
    html = render_digest_email(_paragraph_issue(), DIGEST_URL)
    topics = _section(html, "Topics")
    assert '<p class="summary">A quake struck; a letter followed.</p>' in topics
    assert "Wire A" not in topics and "Wire B" not in topics
    assert "Letter" not in html


class TestGlanceWhenAlone:
    @pytest.mark.parametrize("language", ["en", "zh-Hant"])
    def test_water_alone_is_listed_under_the_glance_marker(self, language):
        glance = translator(language)("layer.glance")

        html = render_digest_email(water_only_issue(language), DIGEST_URL)

        assert MARKER.findall(html) == [glance]
        section = _section(html, glance)
        assert '<a href="https://lone.example.test/1">One report</a>' in section
        assert '<p class="summary">Its summary.</p>' in section
        assert '<a href="https://headline.example.test/1">A headline</a>' in section
        assert '<p class="summary">The filter&#39;s sentence.</p>' in section

    @pytest.mark.parametrize("language", ["en", "zh-Hant"])
    def test_glance_has_no_source_tag_or_score(self, language):
        html = render_digest_email(water_only_issue(language), DIGEST_URL)

        for source in UNNAMED_SOURCES:
            assert source not in html, source
        assert "pill" not in _section(html, translator(language)("layer.glance"))

    def test_an_untranslated_water_item_leaves_its_language_unknown(self):
        content = water_only_issue("en")
        headline = content.filtered_headlines[0].model_copy(update={"passed_through": True})
        content = content.model_copy(update={"filtered_headlines": [headline]})

        html = render_digest_email(content, DIGEST_URL)

        assert re.findall(r'<article class="item" lang="([^"]*)"', html) == ["en", ""]

    @pytest.mark.parametrize("language", ["en", "zh-Hant"])
    @pytest.mark.parametrize("kept", ["tracked_topics", "featured_articles", "news_clusters"])
    def test_any_other_layer_keeps_glance_off_the_mail(self, language, kept):
        full = four_layer_issue()
        content = water_only_issue(language).model_copy(
            update={
                kept: [full.news_clusters[0]] if kept == "news_clusters" else getattr(full, kept)
            }
        )

        html = render_digest_email(content, DIGEST_URL)

        assert translator(language)("layer.glance") not in MARKER.findall(html)
        for text in ("A headline", "The filter's sentence.", "headline.example"):
            assert text not in html, text
