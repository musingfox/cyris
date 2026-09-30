"""Real newsletter samples from $CYRIS_NEWSLETTER_FIXTURES (skip if absent)."""

from datetime import datetime

import pytest
from conftest import load_newsletter_fixture

from cyris.adapters.fetch.email_parser import ParsedNewsletter
from cyris.adapters.fetch.newsletter import newsletter_article
from cyris.domain.models import SourceConfig, Tier

pytestmark = [pytest.mark.unit, pytest.mark.real_fixture]


def _parsed(
    *,
    html_content: str = "",
    text_content: str = "",
    subject: str,
    from_email: str = "list@example.com",
) -> ParsedNewsletter:
    return ParsedNewsletter(
        source_name="fixture",
        subject=subject,
        from_email=from_email,
        date=datetime(2026, 8, 1),
        html_content=html_content,
        text_content=text_content,
    )


def _source(name: str, homepage: str | None = None) -> SourceConfig:
    return SourceConfig(name=name, tier=Tier.SUMMARIZE, tags=[], homepage=homepage)


# Each sample's sender, as the deployed source knows it: the homepage and the From
# domain are what identify the sender's own host, so a sample read without them
# tests a source configured differently from the one that receives these mails.


def test_manpao_text_extracts_canonical_post_url() -> None:
    body = load_newsletter_fixture("manpao.text.txt")
    art = newsletter_article(
        _parsed(text_content=body, html_content="", subject="曼報", from_email="list@manny-li.com"),
        _source("曼報", homepage="https://pro.manny-li.com/posts"),
    )
    assert art is not None
    assert art.url == "https://pro.manny-li.com/posts/nvidia-ai-compute-financing-dzcqdpceapkv"


def test_ieo_html_strips_tracking_from_patreon_url() -> None:
    body = load_newsletter_fixture("ieo.html")
    art = newsletter_article(
        _parsed(
            html_content=body,
            text_content="",
            subject="IEO",
            from_email="list@creator.patreon.com",
        ),
        _source("IEO", homepage="https://www.patreon.com/c/ieo/home?vanity=ieo"),
    )
    assert art is not None
    assert art.url == "https://www.patreon.com/ieo/posts/ai-guang-tong-ye-166524353"
    assert "post_id" not in art.url
    assert "media_id" not in art.url
    assert "utm_" not in art.url


def test_fenshi_html_falls_back_to_synthetic_url() -> None:
    body = load_newsletter_fixture("fenshi.html")
    art = newsletter_article(
        _parsed(html_content=body, text_content="", subject="粉虱通訊"),
        _source("粉虱通訊"),
    )
    assert art is not None
    assert art.url.startswith("newsletter:")


def test_missing_fixtures_skip_instead_of_fail(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("CYRIS_NEWSLETTER_FIXTURES", str(tmp_path))
    with pytest.raises(pytest.skip.Exception):
        load_newsletter_fixture("manpao.text.txt")
