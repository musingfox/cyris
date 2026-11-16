"""The names the owner gave the archive, the third layer, a suppression and an issue.

Each renders in English and zh-Hant on the surfaces that show it; what the code stores, the
period, the file names and the links, keeps its English identifier.
"""

import json
import re

import pytest

from cyris.adapters.output.email_digest import render_digest_email
from cyris.adapters.output.html_digest import HtmlDigestWriter
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
