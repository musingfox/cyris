"""Articles written before the tier-to-lane rename still load, under either key."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import SqliteD1
from pydantic import ValidationError

from cyris.adapters.store.d1_store import D1ArticleStore
from cyris.config import read_config_files
from cyris.domain.models import Lane, SourceConfig, StoredArticle

pytestmark = pytest.mark.unit

FIRST_SEEN = "2026-10-01T10:00:00+00:00"


def _row(url: str, **lane_field: str) -> dict:
    return {
        "url": url,
        "original_id": 1,
        "title": "Title",
        "content": "Content",
        "published_at": "2026-10-01T09:00:00+00:00",
        "source_name": "Source",
        "source_tags": [],
        "ref_urls": [],
        "state": "pending",
        "first_seen_at": FIRST_SEEN,
        **lane_field,
    }


@pytest.mark.parametrize(
    ("legacy", "lane"), [("summarize", Lane.READ), ("fan", Lane.FOLLOW), ("filter", Lane.SKIM)]
)
def test_a_row_still_holding_an_old_spelling_reads_back_as_its_lane(
    legacy: str, lane: Lane
) -> None:
    db = SqliteD1()
    db.query(
        "INSERT INTO stored_articles (url, original_id, title, content, published_at, "
        "source_name, source_lane, state, first_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            "https://example.com/old",
            1,
            "Title",
            "Content",
            FIRST_SEEN,
            "Source",
            legacy,
            "pending",
            FIRST_SEEN,
        ],
    )

    loaded = D1ArticleStore(db).load_by_time_range(
        datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 2, tzinfo=UTC)
    )

    assert [a.source_lane for a in loaded] == [lane]


@pytest.mark.parametrize(("legacy", "lane"), [("fan", Lane.FOLLOW), ("filter", Lane.SKIM)])
def test_old_tier_spellings_map_to_their_lanes(legacy: str, lane: Lane) -> None:
    stored = StoredArticle.model_validate(_row("https://example.com/a", source_tier=legacy))

    assert stored.source_lane == lane


def test_an_old_spelling_under_the_new_key_is_also_accepted() -> None:
    stored = StoredArticle.model_validate(_row("https://example.com/a", source_lane="filter"))

    assert stored.source_lane == Lane.SKIM


def test_an_unknown_legacy_value_is_rejected() -> None:
    with pytest.raises(ValidationError):
        StoredArticle.model_validate(_row("https://example.com/a", source_tier="bogus"))


def test_a_source_still_saying_tier_is_refused_with_the_rename() -> None:
    with pytest.raises(ValidationError, match="renamed to `lane`") as e:
        SourceConfig.model_validate({"name": "a", "tier": "summarize"})

    assert "summarize -> read" in str(e.value)


def test_a_source_with_a_bad_lane_names_the_three_lanes() -> None:
    with pytest.raises(ValidationError, match="'skim', 'read' or 'follow'"):
        SourceConfig.model_validate({"name": "a", "lane": "filter"})


def test_a_yaml_source_still_saying_tier_fails_the_load(tmp_path: Path) -> None:
    sources = tmp_path / "sources.yaml"
    sources.write_text("sources:\n  - name: Foo\n    url: https://foo/feed\n    tier: summarize\n")

    with pytest.raises(ValidationError, match="renamed to `lane`"):
        read_config_files(config_path=tmp_path / "nope.toml", sources_path=sources)


def test_a_defaults_tier_key_is_ignored_and_sources_take_lane(tmp_path: Path) -> None:
    sources = tmp_path / "sources.yaml"
    sources.write_text(
        "defaults:\n  tier: filter\n"
        "sources:\n  - name: Foo\n    url: https://foo/feed\n    lane: read\n"
    )

    raw = read_config_files(config_path=tmp_path / "nope.toml", sources_path=sources)

    assert raw.sources["Foo"].lane == Lane.READ


def test_a_source_with_both_tier_and_lane_is_still_refused() -> None:
    with pytest.raises(ValidationError, match="renamed to `lane`"):
        SourceConfig.model_validate({"name": "a", "tier": "x", "lane": "read"})
