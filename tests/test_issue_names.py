"""The names the owner gave the archive, the third layer, a suppression and an issue.

Each renders in English and zh-Hant on the surfaces that show it; what the code stores, the
period, the file names and the links, keeps its English identifier.
"""

import pytest

from cyris.adapters.output.email_digest import render_digest_email
from cyris.adapters.output.html_digest import HtmlDigestWriter
from cyris.domain.models import DigestContent

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
