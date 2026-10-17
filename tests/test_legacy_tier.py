"""Articles written before the tier-to-lane rename still load, and are rewritten as lanes."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from cyris.adapters.store.article_store import ArticleStore
from cyris.config import load_config
from cyris.domain.models import Article, Lane, SourceConfig, StoredArticle

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


def _partition(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "articles" / "2026-10-01.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(rows))
    return path


def test_a_partition_written_with_source_tier_still_loads(tmp_path: Path) -> None:
    _partition(tmp_path, [_row("https://example.com/old", source_tier="summarize")])
    store = ArticleStore(tmp_path)

    loaded = store.load_by_time_range(
        datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 2, tzinfo=UTC)
    )

    assert [a.source_lane for a in loaded] == [Lane.READ]


def test_saving_into_a_legacy_partition_rewrites_its_rows_as_lanes(tmp_path: Path) -> None:
    path = _partition(tmp_path, [_row("https://example.com/old", source_tier="summarize")])
    store = ArticleStore(tmp_path)
    new = Article(
        id=2,
        title="New",
        url="https://example.com/new",
        content="New",
        published_at=datetime(2026, 10, 1, 9, tzinfo=UTC),
        source_name="Source",
        source_lane=Lane.SKIM,
    )

    store.save([new], now=datetime(2026, 10, 1, 12, tzinfo=UTC))

    rows = {r["url"]: r for r in json.loads(path.read_text())}
    assert rows["https://example.com/old"]["source_lane"] == "read"
    assert "source_tier" not in rows["https://example.com/old"]
    assert rows["https://example.com/new"]["source_lane"] == "skim"


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
    config_file = tmp_path / "cyris.toml"
    config_file.write_text('[general]\ntimezone = "UTC"\n')
    sources = tmp_path / "sources.yaml"
    sources.write_text("sources:\n  - name: Foo\n    url: https://foo/feed\n    tier: summarize\n")

    with pytest.raises(ValidationError, match="renamed to `lane`"):
        load_config(config_path=config_file, sources_path=sources)


def test_a_defaults_tier_key_is_ignored_and_sources_take_lane(tmp_path: Path) -> None:
    config_file = tmp_path / "cyris.toml"
    config_file.write_text('[general]\ntimezone = "UTC"\n')
    sources = tmp_path / "sources.yaml"
    sources.write_text(
        "defaults:\n  tier: filter\n"
        "sources:\n  - name: Foo\n    url: https://foo/feed\n    lane: read\n"
    )

    cfg = load_config(config_path=config_file, sources_path=sources)

    assert cfg.sources["Foo"].lane == Lane.READ


def test_a_source_with_both_tier_and_lane_is_still_refused() -> None:
    with pytest.raises(ValidationError, match="renamed to `lane`"):
        SourceConfig.model_validate({"name": "a", "tier": "x", "lane": "read"})
