from pathlib import Path

from fakes import SqliteD1

from cyris.adapters.store.stories import D1StoryStore
from cyris.domain.models import StoryRecord


def test_save_persists_story_and_members() -> None:
    db = SqliteD1()

    D1StoryStore(db).save(
        "2026-08-28",
        "morning",
        [
            StoryRecord(
                id="2026-08-28-morning-0",
                heading="H",
                urls=["u1", "u2"],
                tags=[],
            )
        ],
    )

    assert len(db.query("SELECT * FROM stories").rows) == 1
    members = db.query("SELECT story_id, article_url FROM story_members ORDER BY article_url")
    assert members.rows == [
        {"story_id": "2026-08-28-morning-0", "article_url": "u1"},
        {"story_id": "2026-08-28-morning-0", "article_url": "u2"},
    ]


def test_save_replaces_the_date_period_window() -> None:
    db = SqliteD1()
    store = D1StoryStore(db)
    store.save(
        "2026-08-28",
        "morning",
        [StoryRecord(id="old", heading="Old", urls=["u1", "u2"], tags=[])],
    )

    store.save(
        "2026-08-28",
        "morning",
        [StoryRecord(id="new", heading="New", urls=["u3"], tags=["tag"])],
    )

    assert [row["id"] for row in db.query("SELECT id FROM stories").rows] == ["new"]
    assert db.query("SELECT story_id, article_url FROM story_members").rows == [
        {"story_id": "new", "article_url": "u3"}
    ]


def test_architecture_lists_story_residency() -> None:
    architecture = Path("docs/architecture.md").read_text()

    assert "D1 `stories`" in architecture
    assert "D1 `story_members`" in architecture
