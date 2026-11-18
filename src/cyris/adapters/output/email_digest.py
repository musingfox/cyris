"""The digest as an email body.

Not the web page: mail clients run no script, and Gmail drops CSS custom
properties and `prefers-color-scheme`. So the tokens are rendered in by value,
light is the default (Gmail inverts a light message legibly, a dark one not),
and the dark palette, the page's own tokens, applies where the reader's client
honours the media query, as Apple Mail does.
"""

import functools
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from markupsafe import Markup

from cyris.adapters.output.html_digest import (
    TEMPLATES,
    HtmlDigestWriter,
    _html_lang,
    environment,
    issue_language,
)
from cyris.domain.layers import layer_by_lane
from cyris.domain.models import DigestContent

PALETTE_FILE = Path(__file__).parent / "email_palette.json"
LIGHT_PALETTE: dict[str, str] = json.loads(PALETTE_FILE.read_text(encoding="utf-8"))["light"]


@functools.cache
def tokens() -> dict[str, str]:
    """The page's design tokens by name, read from the one file that defines them."""
    source = (TEMPLATES / "_tokens.css.j2").read_text(encoding="utf-8")
    return dict(re.findall(r"--([\w-]+):\s*([^;]+);", source))


def _site_of(digest_url: str) -> str:
    """The site the digest link's page is in: its host, and a hosted tenant's `/t/<tenant>`."""
    parts = urlsplit(digest_url)
    return f"{parts.scheme}://{parts.netloc}{parts.path.rsplit('/', 1)[0]}"


def archive_url(site: str) -> str:
    """The archive of `site`: its root on a Pages site, its `index.html` in a hosted tenant's.

    A hosted tenant's site root serves its latest issue, not the archive (ADR-0029), and both
    Pages and the hosted Worker serve `index.html`. A site at a host's root keeps `/`, the
    address a self-hosted mail has always linked.
    """
    return f"{site}/index.html" if urlsplit(site).path else f"{site}/"


def raw_page_url(content: DigestContent, digest_url: str, raw_page: bool) -> str:
    """The issue's raw page beside the digest link's page, or "" when there is none."""
    if not (digest_url and raw_page):
        return ""
    raw_name = HtmlDigestWriter.raw_filename(content.date, content.period)
    return f"{_site_of(digest_url)}/{raw_name.removesuffix('.html')}"


def render_digest_email(
    content: DigestContent,
    digest_url: str = "",
    raw_page: bool = False,
    publish_failed: bool = False,
    degraded: bool = False,
    *,
    language: str = "",
) -> str:
    """The issue as one self-contained HTML document with absolute links only.

    Worded in the issue's `output_language`, or `language`, the current setting, when the
    issue stored none.
    """
    site = _site_of(digest_url) if digest_url else ""
    raw_url = raw_page_url(content, digest_url, raw_page)

    # Our own tokens, not feed text: escaping would turn a font stack's quotes into
    # entities, which a <style> block does not decode.
    dark = {name: Markup(value) for name, value in tokens().items()}
    worded_in = issue_language(content, language)
    template = environment(worded_in).get_template("email.html.j2")
    return template.render(
        content=content,
        content_lang=_html_lang(content.output_language),
        layers=layer_by_lane(content),
        digest_url=digest_url,
        raw_url=raw_url,
        site=site,
        archive_url=archive_url(site) if site else "",
        publish_failed=publish_failed,
        degraded=degraded,
        tok=dark,
        light={**dark, **{name: Markup(value) for name, value in LIGHT_PALETTE.items()}},
        dark=dark,
    )
