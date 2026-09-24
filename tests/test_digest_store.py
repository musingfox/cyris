from datetime import datetime

from fakes import SqliteD1

from cyris.adapters.store.digests import D1DigestStore
from cyris.domain.models import DigestContent, DigestItem, DigestSection, UsageStats


def _content(period: str = "morning", articles_included: int = 5) -> DigestContent:
    return DigestContent(
        date="2026-09-24",
        period=period,
        sources_processed=4,
        articles_received=20,
        articles_included=articles_included,
        usage=UsageStats(
            input_tokens=1200, output_tokens=300, api_calls=3, model="m", neurons=0.1 + 0.2
        ),
        featured_articles=[
            DigestSection(
                heading="今日要聞",
                items=[
                    DigestItem(
                        title="標題",
                        summary="摘要",
                        sources=["S"],
                        urls=["newsletter:abc"],
                        score=87.5,
                    )
                ],
            )
        ],
        dead_link_count=1,
        synthetic_url_count=1,
    )


def test_load_returns_the_saved_content_and_flag() -> None:
    store = D1DigestStore(SqliteD1())
    content = _content()

    store.save(content, raw_page=True)

    loaded = store.load("2026-09-24", "morning")
    assert loaded.content == content
    assert loaded.raw_page is True


def test_a_false_flag_is_stored_as_zero() -> None:
    db = SqliteD1()
    store = D1DigestStore(db)

    store.save(_content(), raw_page=False)

    assert store.load("2026-09-24", "morning").raw_page is False
    assert db.query("SELECT raw_page FROM digests").rows == [{"raw_page": 0}]


def test_the_row_is_keyed_by_date_and_period_and_stamped() -> None:
    db = SqliteD1()

    D1DigestStore(db).save(_content(), raw_page=True)

    assert db.query("SELECT date, period, raw_page FROM digests").rows == [
        {"date": "2026-09-24", "period": "morning", "raw_page": 1}
    ]
    saved_at = db.query("SELECT saved_at FROM digests").rows[0]["saved_at"]
    assert saved_at
    datetime.fromisoformat(saved_at)


def test_each_period_keeps_its_own_row() -> None:
    db = SqliteD1()
    store = D1DigestStore(db)
    morning, evening = _content("morning", 5), _content("evening", 7)

    store.save(morning, raw_page=True)
    store.save(evening, raw_page=False)

    assert store.load("2026-09-24", "morning").content == morning
    assert store.load("2026-09-24", "evening").content == evening
    assert db.query("SELECT COUNT(*) AS n FROM digests").rows == [{"n": 2}]


def test_an_empty_table_loads_nothing() -> None:
    assert D1DigestStore(SqliteD1()).load("2026-09-24", "evening") is None


def test_another_period_loads_nothing() -> None:
    store = D1DigestStore(SqliteD1())
    store.save(_content("morning"), raw_page=True)

    assert store.load("2026-09-24", "evening") is None
