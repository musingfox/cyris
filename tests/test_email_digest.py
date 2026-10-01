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
from cyris.domain.models import DigestContent, DigestItem, DigestSection, UsageStats

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]
DIGEST_URL = "https://digest.example.org/2026-09-27-morning"
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


class TestSections:
    def test_every_section_the_page_has_is_named_in_its_order(self):
        html = render_digest_email(_full(), DIGEST_URL)

        labels = ["Top story", "Features", "In Focus", "Following", "On the Radar", "The Wire"]
        positions = [html.index(f">{label}<") for label in labels]
        assert positions == sorted(positions)

    def test_an_empty_section_is_left_out(self):
        content = DigestContent(
            date="2026-09-27",
            period="evening",
            sources_processed=1,
            articles_received=2,
            articles_included=1,
            filtered_headlines=[_item("Only", "https://wire.test/only", "Wire")],
        )

        html = render_digest_email(content, DIGEST_URL)

        assert ">The Wire<" in html
        for label in ["Top story", "Features", "In Focus", "Following", "On the Radar"]:
            assert f">{label}<" not in html

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


class TestLanguage:
    CONTENT = [
        "Lead Story",
        "Lead Story summary.",
        "Second Feature",
        "A Cluster",
        "Cluster summary.",
        "Fan Item",
        "Watching — Why these.",
        "Radar Item",
        "Wire One",
    ]
    CHROME = ["Top story", "Features", "In Focus", "Following", "On the Radar", "The Wire"]

    def test_the_chrome_is_english_and_the_content_carries_the_output_language(self):
        content = _full().model_copy(update={"output_language": "ja"})
        html = render_digest_email(content, DIGEST_URL)
        langs = text_langs(html)

        assert '<html lang="en">' in html
        assert {text: langs[text] for text in self.CHROME} == dict.fromkeys(self.CHROME, "en")
        assert {text: langs[text] for text in self.CONTENT} == dict.fromkeys(self.CONTENT, "ja")

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


class TestPalettes:
    def test_light_is_the_default_and_dark_follows_the_reader(self):
        style = _style(render_digest_email(_full(), DIGEST_URL))
        default, dark = style.split("@media (prefers-color-scheme: dark)", 1)

        assert LIGHT_PALETTE["bg"] in default
        assert tokens()["bg"] in dark
        assert tokens()["bg"] not in default

    def test_light_names_every_colour_the_page_tokens_name(self):
        colours = {k for k, v in tokens().items() if v.startswith(("#", "rgba("))}

        assert set(LIGHT_PALETTE) == colours - {"grid"}

    def test_the_light_palette_is_the_one_the_spec_lists(self):
        spec = (REPO / "docs/design/ui-language.md").read_text(encoding="utf-8")
        listed = dict(re.findall(r"^\| `--([\w-]+)` \| `([^`]+)` \|", spec, re.M))

        assert listed == LIGHT_PALETTE
        assert json.loads(PALETTE_FILE.read_text(encoding="utf-8"))["light"] == LIGHT_PALETTE

    @pytest.mark.parametrize("ground", ["bg", "surface"])
    @pytest.mark.parametrize("ink", ["text", "text-dim", "text-faint", "accent", "warn"])
    def test_light_text_reads_on_its_grounds(self, ink, ground):
        assert _contrast(LIGHT_PALETTE[ink], LIGHT_PALETTE[ground]) >= 4.5

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
