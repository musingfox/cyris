"""The pipeline dates an issue by the date it is given, and by today only when none is given."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fakes import FakeLLM

from cyris.domain.models import Article, Lane
from cyris.service_layer import digest_pipeline
from cyris.service_layer.digest_pipeline import DigestPipeline

pytestmark = pytest.mark.unit


def _pipeline() -> DigestPipeline:
    return DigestPipeline(
        FakeLLM("{}"),
        max_digest_output=10,
        summarize_snippet_length=200,
        filter_snippet_length=200,
        summarize_score_threshold=0,
        featured_threshold=0,
        max_featured=3,
        output_language="en",
        style_prompt="",
    )


def _article() -> Article:
    return Article(
        id=1,
        title="Story 1",
        url="https://example.com/1",
        content="A launch.",
        published_at=datetime.now(UTC) - timedelta(hours=1),
        source_name="TechSource",
        source_lane=Lane.READ,
        source_tags=["tech"],
    )


@pytest.fixture
def today_is_the_16th(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        digest_pipeline,
        "now_in_timezone",
        lambda tz: datetime(2026, 10, 16, 12, tzinfo=ZoneInfo(tz)),
    )


@pytest.mark.parametrize("articles", [[], [_article()]], ids=["empty", "non-empty"])
async def test_a_given_date_is_the_issue_date(today_is_the_16th, articles) -> None:
    result = await _pipeline().process(
        articles, {}, period="evening", timezone="Asia/Taipei", date="2026-10-14"
    )

    assert result.content.date == "2026-10-14"


async def test_without_a_date_the_issue_is_dated_today(today_is_the_16th) -> None:
    result = await _pipeline().process([], {}, period="evening", timezone="Asia/Taipei")

    assert result.content.date == "2026-10-16"
