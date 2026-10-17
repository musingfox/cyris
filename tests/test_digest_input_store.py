import pytest
from fakes import CountingD1, SqliteD1

from cyris.adapters.store.digest_inputs import D1DigestInputStore

pytestmark = pytest.mark.unit


def _urls(n: int) -> list[str]:
    return [f"https://example.com/{i:03d}/" + "x" * 100 for i in range(n)]


def test_load_returns_the_urls_in_saved_order() -> None:
    store = D1DigestInputStore(SqliteD1())
    urls = ["https://a.example/1", "https://b.example/2"]

    store.save("2026-10-14", "evening", urls)

    assert store.load("2026-10-14", "evening") == urls


def test_a_second_save_replaces_the_first() -> None:
    db = SqliteD1()
    store = D1DigestInputStore(db)

    store.save("2026-10-14", "evening", ["https://a.example/1"])
    store.save("2026-10-14", "evening", ["https://b.example/2", "https://c.example/3"])

    assert store.load("2026-10-14", "evening") == ["https://b.example/2", "https://c.example/3"]
    assert db.query("SELECT COUNT(*) AS n FROM digest_inputs").rows[0]["n"] == 1


def test_an_empty_list_is_saved_and_a_missing_key_is_none() -> None:
    store = D1DigestInputStore(SqliteD1())
    assert store.load("2026-10-14", "evening") is None

    store.save("2026-10-14", "evening", [])

    assert store.load("2026-10-14", "evening") == []


def test_four_hundred_long_urls_round_trip_in_one_statement() -> None:
    db = CountingD1()
    store = D1DigestInputStore(db)
    urls = _urls(400)

    before = db.query_count
    store.save("2026-10-14", "evening", urls)
    writes = db.query_count - before

    assert writes == 1
    assert store.load("2026-10-14", "evening") == urls


def test_delete_removes_one_issue_and_leaves_the_others() -> None:
    store = D1DigestInputStore(SqliteD1())
    store.save("2026-10-01", "morning", ["u0"])
    store.save("2026-10-01", "evening", ["u1"])

    store.delete("2026-10-01", "morning")

    assert store.load("2026-10-01", "morning") is None
    assert store.load("2026-10-01", "evening") == ["u1"]


def test_deleting_an_absent_issue_is_a_no_op() -> None:
    store = D1DigestInputStore(SqliteD1())

    store.delete("2026-10-01", "morning")

    assert store.load("2026-10-01", "morning") is None
