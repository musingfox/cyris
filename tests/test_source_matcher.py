"""Tests for source name matcher."""

from cyris.adapters.fetch.source_matcher import SourceMatcher
from cyris.domain.models import SourceConfig, Tier


def _make_sources():
    return {
        "TechCrunch": SourceConfig(name="TechCrunch", tier=Tier.FILTER, tags=["tech"]),
        "Stratechery": SourceConfig(name="Stratechery", tier=Tier.SUMMARIZE, tags=["tech"]),
        "The Verge": SourceConfig(name="The Verge", tier=Tier.FILTER, tags=["tech"]),
    }


class TestSourceMatcher:
    def test_exact_match(self):
        matcher = SourceMatcher(_make_sources())
        result = matcher.match("TechCrunch")
        assert result is not None
        assert result.name == "TechCrunch"

    def test_case_insensitive_match(self):
        matcher = SourceMatcher(_make_sources())
        result = matcher.match("techcrunch")
        assert result is not None
        assert result.name == "TechCrunch"

    def test_case_insensitive_mixed(self):
        matcher = SourceMatcher(_make_sources())
        result = matcher.match("the verge")
        assert result is not None
        assert result.name == "The Verge"

    def test_fuzzy_match(self):
        matcher = SourceMatcher(_make_sources())
        result = matcher.match("Tech Crunch")  # space added
        assert result is not None
        assert result.name == "TechCrunch"

    def test_alias_override(self):
        matcher = SourceMatcher(_make_sources(), alias_map={"TC News": "TechCrunch"})
        result = matcher.match("TC News")
        assert result is not None
        assert result.name == "TechCrunch"

    def test_no_match_returns_none(self):
        matcher = SourceMatcher(_make_sources())
        result = matcher.match("Completely Unknown Feed")
        assert result is None

    def test_alias_takes_precedence_over_fuzzy(self):
        matcher = SourceMatcher(
            _make_sources(),
            alias_map={"Stratechery Daily": "Stratechery"},
        )
        result = matcher.match("Stratechery Daily")
        assert result is not None
        assert result.name == "Stratechery"
        assert result.tier == Tier.SUMMARIZE
