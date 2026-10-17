"""Articles written before the tier-to-lane rename still load, and are rewritten as lanes."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from cyris.adapters.store.article_store import ArticleStore
from cyris.domain.models import Article, Lane, StoredArticle

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
