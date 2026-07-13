"""Tests for preference profile generation."""

import json
from datetime import UTC, datetime

import pytest
from fakes import FakeLLM

from cyris.domain.models import (
    ArticleState,
    PreferenceProfile,
    StoredArticle,
    Tier,
    TriageFeedbackData,
)
from cyris.learn.profile import (
    generate_profile_from_triage,
    load_latest_profile,
    save_profile,
)


class TestProfileStorage:
    def test_save_and_load_roundtrip(self, tmp_path):
        """Save and load profile successfully."""
        profile = PreferenceProfile(
            generated_at="2026-03-19T10:00:00Z",
            sample_size=10,
            themes=["AI", "Cloud"],
            signals=["Funding", "M&A"],
            anti_signals=["Listicles"],
            prompt_injection="User likes enterprise tech news.",
        )

        save_profile(profile, tmp_path)

        loaded = load_latest_profile(tmp_path)
        assert loaded is not None
        assert loaded.sample_size == 10
        assert loaded.themes == ["AI", "Cloud"]
        assert loaded.prompt_injection == "User likes enterprise tech news."

    def test_load_from_empty_dir(self, tmp_path):
        """Return None when no profile exists."""
        loaded = load_latest_profile(tmp_path)
        assert loaded is None


class TestGenerateProfileFromTriage:
    @pytest.mark.asyncio
    async def test_generate_from_triage_with_contrast(self, respx_mock):
        """Generate profile from 3 accepted + 2 rejected articles."""
        now = datetime.now(UTC)

        accepted = [
            StoredArticle(
                url=f"https://example.com/accepted/{i}",
                original_id=i,
                title=f"Enterprise AI News {i}",
                content="test content",
                published_at=now,
                source_name="TechSource",
                source_tier=Tier.SUMMARIZE,
                first_seen_at=now,
                state=ArticleState.ACCEPTED,
            )
            for i in range(3)
        ]

        rejected = [
            StoredArticle(
                url=f"https://example.com/rejected/{i}",
                original_id=100 + i,
                title=f"Celebrity Gossip {i}",
                content="test content",
                published_at=now,
                source_name="GossipSource",
                source_tier=Tier.FILTER,
                first_seen_at=now,
                state=ArticleState.REJECTED,
            )
            for i in range(2)
        ]

        feedback = TriageFeedbackData(
            accepted_articles=accepted,
            rejected_articles=rejected,
            date_range_start=now.isoformat(),
            date_range_end=now.isoformat(),
        )

        # Mock LLM response
        mock_response = {
            "themes": ["Enterprise AI", "Technology"],
            "signals": ["Enterprise adoption", "Technical depth"],
            "anti_signals": ["Celebrity news", "Gossip"],
            "prompt_injection": "User prefers enterprise tech news, avoids celebrity gossip.",
        }

        profile = await generate_profile_from_triage(feedback, FakeLLM(json.dumps(mock_response)))

        assert profile.sample_size == 3
        assert len(profile.themes) == 2
        assert "Enterprise AI" in profile.themes
        assert len(profile.signals) >= 1
        assert len(profile.anti_signals) >= 1
        assert "celebrity gossip" in profile.prompt_injection.lower()

    @pytest.mark.asyncio
    async def test_insufficient_accepted_raises_error(self):
        """Raise ValueError when accepted_count < 3."""
        now = datetime.now(UTC)

        accepted = [
            StoredArticle(
                url=f"https://example.com/{i}",
                original_id=i,
                title=f"Article {i}",
                content="test",
                published_at=now,
                source_name="Source",
                source_tier=Tier.SUMMARIZE,
                first_seen_at=now,
                state=ArticleState.ACCEPTED,
            )
            for i in range(2)  # Only 2 accepted
        ]

        feedback = TriageFeedbackData(
            accepted_articles=accepted,
            rejected_articles=[],
            date_range_start=now.isoformat(),
            date_range_end=now.isoformat(),
        )

        with pytest.raises(ValueError, match="Insufficient"):
            await generate_profile_from_triage(feedback, FakeLLM())
