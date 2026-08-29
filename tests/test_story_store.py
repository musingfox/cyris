from pathlib import Path

from fakes import CountingD1, SqliteD1

from cyris.adapters.store.stories import D1StoryStore
from cyris.domain.models import StoryRecord


def test_save_persists_story_and_members_and_reports_rows_written() -> None:
    db = SqliteD1()

    written = D1StoryStore(db).save(
        "2026-08-28",
        "morning",
        [StoryRecord(id="2026-08-28-morning-0", heading="H", urls=["u1", "u2"])],
    )

    assert written == 3  # 1 stories row + 2 story_members rows
    stories = db.query("SELECT id, created_at FROM stories").rows
    assert [row["id"] for row in stories] == ["2026-08-28-morning-0"]
    assert stories[0]["created_at"]  # stamped, not NULL
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
        [StoryRecord(id="old", heading="Old", urls=["u1", "u2"])],
    )

    store.save(
        "2026-08-28",
        "morning",
        [StoryRecord(id="new", heading="New", urls=["u3"])],
    )

    assert [row["id"] for row in db.query("SELECT id FROM stories").rows] == ["new"]
    assert db.query("SELECT story_id, article_url FROM story_members").rows == [
        {"story_id": "new", "article_url": "u3"}
    ]


def test_rerun_with_zero_stories_clears_the_window_only() -> None:
    """A re-run that clustered nothing must not leave a previous run's rows as current."""
    db = SqliteD1()
    store = D1StoryStore(db)
    store.save(
        "2026-08-28",
        "morning",
        [
            StoryRecord(id="a", heading="A", urls=["u1"]),
            StoryRecord(id="b", heading="B", urls=["u2"]),
            StoryRecord(id="c", heading="C", urls=["u3"]),
        ],
    )
    store.save("2026-08-28", "evening", [StoryRecord(id="e", heading="E", urls=["u9"])])

    store.save("2026-08-28", "morning", [])

    assert [row["id"] for row in db.query("SELECT id FROM stories").rows] == ["e"]
    assert db.query("SELECT story_id, article_url FROM story_members").rows == [
        {"story_id": "e", "article_url": "u9"}
    ]


def test_save_batches_writes_within_the_bound_param_budget() -> None:
    db = CountingD1()
    records = [
        StoryRecord(
            id=f"2026-08-28-morning-{n}",
            heading=f"H{n}",
            urls=[f"u{n}-{i}" for i in range(30)],
        )
        for n in range(2)
    ]

    written = D1StoryStore(db).save("2026-08-28", "morning", records)
    queries = db.query_count

    assert written == 62  # 2 stories + 60 members
    # 1 stories statement + 2 member statements (60 rows, 50 per) + 2 stale-row deletes
    assert queries == 5


def test_architecture_lists_story_residency() -> None:
    architecture = Path("docs/architecture.md").read_text()

    assert "D1 `stories`" in architecture
    assert "D1 `story_members`" in architecture
