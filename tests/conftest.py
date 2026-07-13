"""Shared test fixtures for Cyris."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cyris.domain.models import (
    Article,
    DigestContent,
    DigestItem,
    DigestSection,
    SourceConfig,
    Tier,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def sample_sources() -> dict[str, SourceConfig]:
    return {
        "TechCrunch": SourceConfig(
            name="TechCrunch",
            url="https://techcrunch.com/feed/",
            tier=Tier.FILTER,
            tags=["tech", "startup"],
        ),
        "Reuters - World": SourceConfig(
            name="Reuters - World",
            url="https://www.reutersagency.com/feed/",
            tier=Tier.FILTER,
            tags=["international", "news"],
        ),
        "Stratechery": SourceConfig(
            name="Stratechery",
            url="https://stratechery.com/feed/",
            tier=Tier.SUMMARIZE,
            tags=["tech", "business-strategy"],
            paywall=True,
        ),
    }


@pytest.fixture
def sample_filter_articles() -> list[Article]:
    return [
        Article(
            id=101,
            title="Apple Vision Pro 2 announced at $2499",
            url="https://techcrunch.com/2026/03/16/apple-vision-pro-2",
            content="Apple announced the second generation Vision Pro today.",
            published_at=datetime(2026, 3, 16, 10, 0, tzinfo=UTC),
            source_name="TechCrunch",
            source_tier=Tier.FILTER,
            source_tags=["tech", "startup"],
        ),
        Article(
            id=102,
            title="TSMC Arizona Phase 2 delayed by 6 months",
            url="https://reuters.com/2026/03/16/tsmc-arizona",
            content="TSMC confirmed that its Arizona Phase 2 expansion has been delayed.",
            published_at=datetime(2026, 3, 16, 8, 30, tzinfo=UTC),
            source_name="Reuters - World",
            source_tier=Tier.FILTER,
            source_tags=["international", "news"],
        ),
    ]


@pytest.fixture
def sample_summarize_articles() -> list[Article]:
    return [
        Article(
            id=103,
            title="Weekly tech trends: AI regulation momentum",
            url="https://stratechery.com/2026/03/16/weekly-trends",
            content="Ben Thompson analyzes the growing momentum behind AI regulation.",
            author="Ben Thompson",
            published_at=datetime(2026, 3, 16, 7, 0, tzinfo=UTC),
            source_name="Stratechery",
            source_tier=Tier.SUMMARIZE,
            source_tags=["tech", "business-strategy"],
        ),
    ]


@pytest.fixture
def sample_digest_content() -> DigestContent:
    return DigestContent(
        date="2026-03-16",
        period="morning",
        sources_processed=3,
        articles_received=10,
        articles_included=3,
        thematic_summaries=[
            DigestSection(
                heading="AI 產業本週動態",
                items=[
                    DigestItem(
                        title="AI regulation momentum",
                        summary="歐盟 AI 法案執行力度加強，科技公司面臨新的合規挑戰。",
                        sources=["Stratechery"],
                        urls=["https://stratechery.com/2026/03/16/weekly-trends"],
                    ),
                ],
            ),
        ],
        filtered_headlines=[
            DigestItem(
                title="Apple Vision Pro 第二代發表",
                summary="價格降至 $2499，新增企業功能",
                sources=["TechCrunch"],
                urls=["https://techcrunch.com/2026/03/16/apple-vision-pro-2"],
            ),
            DigestItem(
                title="台積電亞利桑那廠延期六個月",
                summary="Phase 2 因人力問題延後",
                sources=["Reuters"],
                urls=["https://reuters.com/2026/03/16/tsmc-arizona"],
            ),
        ],
    )


@pytest.fixture
def miniflux_response() -> dict:
    with open(FIXTURES_DIR / "miniflux_response.json") as f:
        return json.load(f)
