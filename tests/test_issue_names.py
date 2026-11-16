"""The names the owner gave the archive, the third layer, a suppression and an issue.

Each renders in English and zh-Hant on the surfaces that show it; what the code stores, the
period, the file names and the links, keeps its English identifier.
"""

import json
import re

import pytest

from cyris.adapters.output.email_digest import render_digest_email
from cyris.adapters.output.html_digest import HtmlDigestWriter
from cyris.domain.interest_graph import InterestEdge, InterestGraph, InterestNode
from cyris.domain.models import DigestContent, DigestItem

pytestmark = pytest.mark.unit

ONLINE = "https://digest.test/2026-11-17-morning"


def _content(language: str, **overrides) -> DigestContent:
    fields = {
        "date": "2026-11-17",
        "period": "morning",
        "sources_processed": 2,
        "articles_received": 3,
        "articles_included": 1,
        "output_language": language,
    }
    return DigestContent(**(fields | overrides))


# ---- the archive -----------------------------------------------------------------------


@pytest.mark.parametrize(("language", "name"), [("en", "Archive"), ("zh-Hant", "過往精選")])
def test_the_archive_is_named_in_its_language(language, name, tmp_path) -> None:
    index = HtmlDigestWriter(tmp_path, language=language).render_index(["2026-11-17-morning.html"])
    digest = HtmlDigestWriter(tmp_path).render(_content(language))

    assert f"<title>CYRIS // {name}</title>" in index
    assert f'<span class="label">{name}</span>' in index
    assert f'aria-current="page">{name}</a>' in index
    assert f'href="index.html" aria-current="page">{name}</a>' in digest


@pytest.mark.parametrize(("language", "name"), [("en", "Archive"), ("zh-Hant", "過往精選")])
def test_the_mail_links_the_archive_by_its_name(language, name) -> None:
    html = render_digest_email(_content(language), ONLINE)

    assert f'<a href="https://digest.test/">{name}</a>' in html


# ---- the third layer -------------------------------------------------------------------


def _glance(n: int) -> list[DigestItem]:
    return [
        DigestItem(title=f"Row {i}", summary="", sources=["A"], urls=[f"https://g.test/{i}"])
        for i in range(n)
    ]


@pytest.mark.parametrize(
    ("language", "name", "one", "many"),
    [
        ("en", "Glance", "1 at a glance", "12 at a glance"),
        ("zh-Hant", "速覽", "1 則速覽", "12 則速覽"),
    ],
)
def test_the_third_layer_is_glance(language, name, one, many, tmp_path) -> None:
    writer = HtmlDigestWriter(tmp_path)

    html = writer.render(_content(language, filtered_headlines=_glance(12)))
    single = writer.render(_content(language, filtered_headlines=_glance(1)))

    assert f'<h2 class="section-tag"><span class="label">{name}</span></h2>' in html
    [words] = [json.loads(w) for w in re.findall(r"data-layer-words='([^']*)'", html)]
    assert words["count"] == many
    assert words["skip_label"].endswith(many)
    assert words["back"].endswith(name)
    [alone] = [json.loads(w) for w in re.findall(r"data-layer-words='([^']*)'", single)]
    assert alone["count"] == one
    for old in ("Headline", "快訊"):
        assert old not in html


# ---- a suppression on the interest map -------------------------------------------------


def _interest(language: str, tmp_path, *, lit: float, holds: bool) -> str:
    nodes = [InterestNode("代理", 2, 0, "up", lit), InterestNode("晶片", 0, 2, "down", 0.0)]
    graph = InterestGraph(nodes=nodes, edges=[InterestEdge("代理", "晶片")])
    return HtmlDigestWriter(tmp_path, language=language).render_interest(
        graph, holds_active=holds, generated_at="2026-11-17T08:00:00+08:00", clock_days=7
    )


def test_a_suppression_is_not_interested_in_zh_hant(tmp_path) -> None:
    lit = _interest("zh-Hant", tmp_path, lit=1.0, holds=True)
    elsewhere = _interest("zh-Hant", tmp_path, lit=0.0, holds=True)
    none = _interest("zh-Hant", tmp_path, lit=0.0, holds=False)

    assert "亮：標為沒興趣；越暗：剩餘時間越少；淡色：未標為沒興趣。" in lit
    assert "並標出每項沒興趣標記還剩多少時間。" in lit
    assert "目前有標為沒興趣的文章，但都不在這張地圖的標籤附近。" in elsewhere
    assert "目前沒有標為沒興趣的文章。" in none
    for page in (lit, elsewhere, none):
        assert "排除" not in page
        assert "壓制" not in page


def test_a_suppression_keeps_its_english_name(tmp_path) -> None:
    lit = _interest("en", tmp_path, lit=1.0, holds=True)

    assert "Lit = suppressed now; dimmer = less time left; half colour = not suppressed." in lit
    assert "No suppression is active." in _interest("en", tmp_path, lit=0.0, holds=False)
