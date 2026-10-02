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

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from cyris.adapters.notify import period_label
from cyris.adapters.output.html_digest import (
    PROJECT_URL,
    HtmlDigestWriter,
    _features,
    _hostname,
    _html_lang,
)
from cyris.domain.models import DigestContent

TEMPLATES = Path(__file__).parent / "templates"
PALETTE_FILE = Path(__file__).parent / "email_palette.json"
LIGHT_PALETTE: dict[str, str] = json.loads(PALETTE_FILE.read_text(encoding="utf-8"))["light"]

_env = Environment(
    loader=FileSystemLoader(TEMPLATES),
    # default=True for the reason `HtmlDigestWriter` gives: the .j2 suffix would
    # otherwise leave feed-controlled titles and URLs unescaped.
    autoescape=select_autoescape(["html", "xml"], default=True),
)
_env.filters["hostname"] = _hostname
_env.globals["project_url"] = PROJECT_URL


@functools.cache
def tokens() -> dict[str, str]:
    """The page's design tokens by name, read from the one file that defines them."""
    source = (TEMPLATES / "_tokens.css.j2").read_text(encoding="utf-8")
    return dict(re.findall(r"--([\w-]+):\s*([^;]+);", source))


def raw_page_url(content: DigestContent, digest_url: str, raw_page: bool) -> str:
    """The issue's raw page on the digest link's own host, or "" when there is none."""
    if not (digest_url and raw_page):
        return ""
    parts = urlsplit(digest_url)
    raw_name = HtmlDigestWriter.raw_filename(content.date, content.period)
    return f"{parts.scheme}://{parts.netloc}/{raw_name.removesuffix('.html')}"


def render_digest_email(
    content: DigestContent,
    digest_url: str = "",
    raw_page: bool = False,
    publish_failed: bool = False,
    degraded: bool = False,
) -> str:
    """The issue as one self-contained HTML document with absolute links only."""
    site = ""
    if digest_url:
        parts = urlsplit(digest_url)
        site = f"{parts.scheme}://{parts.netloc}"
    raw_url = raw_page_url(content, digest_url, raw_page)

    features = _features(content)
    # Our own tokens, not feed text: escaping would turn a font stack's quotes into
    # entities, which a <style> block does not decode.
    dark = {name: Markup(value) for name, value in tokens().items()}
    return _env.get_template("email.html.j2").render(
        content=content,
        content_lang=_html_lang(content.output_language),
        period_label=period_label(content.period),
        lead_story=features[0] if features else None,
        featured_articles=features[1:],
        digest_url=digest_url,
        raw_url=raw_url,
        site=site,
        publish_failed=publish_failed,
        degraded=degraded,
        t=dark,
        light={**dark, **{name: Markup(value) for name, value in LIGHT_PALETTE.items()}},
        dark=dark,
    )
