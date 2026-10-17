"""The manifest's D1 generations: reads see one whole save, writes never leave a mix."""

from datetime import UTC, datetime

import pytest
from fakes import SqliteD1

from cyris.adapters.output import pages_manifest
from cyris.adapters.output.pages_manifest import D1PagesManifest
from cyris.adapters.store.d1 import D1Error

pytestmark = pytest.mark.unit


def _insert_row(db, path, digest, stamp):
    db.query(
        "INSERT INTO pages_manifest (path, hash, updated_at) VALUES (?, ?, ?)",
        [path, digest, stamp],
    )


class FailingD1(SqliteD1):
    """SqliteD1 that refuses every statement starting with `refused`, as D1 would on an outage."""

    def __init__(self, refused: str) -> None:
        super().__init__()
        self.refused = refused
        self.sent: list[str] = []

    def query(self, sql, params=None):
        self.sent.append(sql)
        if self.refused and sql.startswith(self.refused):
            raise D1Error("HTTP 500: unavailable")
        return super().query(sql, params)


class InterleavingD1(SqliteD1):
    """SqliteD1 that runs `between` once, right after the first statement starting with `after`.

    Two runs saving at once over D1's HTTP API interleave statement by statement;
    this places the other run's whole save at one such seam.
    """

    def __init__(self) -> None:
        super().__init__()
        self.after = ""
        self.between = None

    def query(self, sql, params=None):
        result = super().query(sql, params)
        if self.between is not None and self.after and sql.startswith(self.after):
            between, self.between = self.between, None
            between()
        return result


def test_load_returns_only_the_newest_generation():
    db = SqliteD1()
    _insert_row(db, "a", "1", "2026-10-01T00:00:00+00:00")
    _insert_row(db, "b", "2", "2026-10-01T00:00:00+00:00")
    _insert_row(db, "c", "3", "2026-10-02T00:00:00+00:00")

    assert D1PagesManifest(db).load() == {"c": "3"}


def test_load_of_an_empty_table_is_empty():
    assert D1PagesManifest(SqliteD1()).load() == {}


def test_a_refused_insert_leaves_the_old_manifest_whole():
    db = FailingD1(refused="")
    manifest = D1PagesManifest(db)
    manifest.save({"a": "1", "b": "2"})
    db.refused = "INSERT INTO pages_manifest"

    with pytest.raises(D1Error):
        manifest.save({"c": "3"})

    assert manifest.load() == {"a": "1", "b": "2"}


def test_a_refused_delete_leaves_the_new_manifest_whole():
    db = FailingD1(refused="")
    manifest = D1PagesManifest(db)
    manifest.save({"a": "1", "b": "2"})
    db.refused = "DELETE FROM pages_manifest"

    with pytest.raises(D1Error):
        manifest.save({"b": "2", "c": "3"})

    assert manifest.load() == {"b": "2", "c": "3"}


def test_a_save_after_a_refused_delete_leaves_only_its_own_rows():
    db = FailingD1(refused="")
    manifest = D1PagesManifest(db)
    manifest.save({"a": "1", "b": "2"})
    db.refused = "DELETE FROM pages_manifest"
    with pytest.raises(D1Error):
        manifest.save({"b": "2", "c": "3"})
    db.refused = ""

    manifest.save({"d": "4"})

    assert manifest.load() == {"d": "4"}
    assert [row["path"] for row in db.query("SELECT path FROM pages_manifest").rows] == ["d"]


def test_a_refused_read_of_the_latest_stamp_writes_nothing():
    db = FailingD1(refused="")
    manifest = D1PagesManifest(db)
    manifest.save({"a": "1", "b": "2"})
    db.refused = "SELECT MAX(updated_at)"
    db.sent.clear()

    with pytest.raises(D1Error):
        manifest.save({"c": "3"})

    assert db.sent == ["SELECT MAX(updated_at) AS latest FROM pages_manifest"]
    assert manifest.load() == {"a": "1", "b": "2"}


def test_an_empty_manifest_is_refused_before_any_statement():
    db = FailingD1(refused="")
    manifest = D1PagesManifest(db)
    manifest.save({"a": "1"})
    db.sent.clear()

    with pytest.raises(ValueError, match="empty"):
        manifest.save({})

    assert db.sent == []
    assert manifest.load() == {"a": "1"}


def test_a_large_manifest_is_one_insert_with_two_bound_parameters():
    db = FailingD1(refused="")
    manifest = D1PagesManifest(db)
    big = {f"/p{i}.html": f"h{i}" for i in range(2000)}
    db.sent.clear()
    params_seen = []
    real_query = db.query

    def recording(sql, params=None):
        params_seen.append(params)
        return real_query(sql, params)

    db.query = recording

    manifest.save(big)

    assert len(db.sent) == 3
    assert [sql.split(" ")[0] for sql in db.sent] == ["SELECT", "INSERT", "DELETE"]
    insert_params = params_seen[1]
    assert len(insert_params) == 2
    assert manifest.load() == big


def _at(monkeypatch, when):
    monkeypatch.setattr(pages_manifest, "_now", lambda: when)


def test_a_repeated_clock_still_replaces_the_whole_manifest(monkeypatch):
    _at(monkeypatch, datetime(2026, 10, 18, 10, 0, 0, tzinfo=UTC))
    manifest = D1PagesManifest(SqliteD1())
    manifest.save({"a": "1", "b": "2"})

    manifest.save({"b": "2", "c": "3"})

    assert manifest.load() == {"b": "2", "c": "3"}


def test_a_stepped_back_clock_still_wins(monkeypatch):
    db = FailingD1(refused="")
    manifest = D1PagesManifest(db)
    _at(monkeypatch, datetime(2026, 10, 18, 10, 0, 0, tzinfo=UTC))
    manifest.save({"a": "1"})
    _at(monkeypatch, datetime(2026, 10, 18, 9, 0, 0, tzinfo=UTC))
    db.refused = "DELETE FROM pages_manifest"

    with pytest.raises(D1Error):
        manifest.save({"b": "2"})

    assert manifest.load() == {"b": "2"}


def test_a_save_beats_a_stamp_stored_in_the_future():
    db = FailingD1(refused="DELETE FROM pages_manifest")
    _insert_row(db, "old", "h", "2999-01-01T00:00:00+00:00")
    manifest = D1PagesManifest(db)

    with pytest.raises(D1Error):
        manifest.save({"/n.html": "h2"})

    assert manifest.load() == {"/n.html": "h2"}


def test_a_save_beats_a_legacy_second_precision_stamp(monkeypatch):
    db = SqliteD1()
    _insert_row(db, "old", "h", "2026-10-18T10:00:00+00:00")
    _at(monkeypatch, datetime(2026, 10, 18, 10, 0, 0, 250000, tzinfo=UTC))

    D1PagesManifest(db).save({"/n.html": "h2"})

    assert D1PagesManifest(db).load() == {"/n.html": "h2"}


def test_an_older_save_cleaning_up_keeps_a_newer_save_that_landed_meanwhile(monkeypatch):
    db = InterleavingD1()
    older, newer = D1PagesManifest(db), D1PagesManifest(db)
    _at(monkeypatch, datetime(2026, 10, 18, 10, 0, 0, tzinfo=UTC))
    older.save({"/a.html": "1"})

    def newer_saves():
        _at(monkeypatch, datetime(2026, 10, 18, 10, 0, 5, tzinfo=UTC))
        newer.save({"/a.html": "2", "/b.html": "2"})
        _at(monkeypatch, datetime(2026, 10, 18, 10, 0, 1, tzinfo=UTC))

    db.after, db.between = "INSERT", newer_saves
    _at(monkeypatch, datetime(2026, 10, 18, 10, 0, 1, tzinfo=UTC))
    older.save({"/a.html": "1", "/c.html": "1"})

    assert older.load() == {"/a.html": "2", "/b.html": "2"}


def test_an_older_save_inserting_late_never_overwrites_a_newer_generation(monkeypatch):
    db = InterleavingD1()
    older, newer = D1PagesManifest(db), D1PagesManifest(db)
    _at(monkeypatch, datetime(2026, 10, 18, 10, 0, 0, tzinfo=UTC))
    older.save({"/a.html": "1"})

    def newer_saves():
        _at(monkeypatch, datetime(2026, 10, 18, 10, 0, 5, tzinfo=UTC))
        newer.save({"/a.html": "2", "/b.html": "2"})
        _at(monkeypatch, datetime(2026, 10, 18, 10, 0, 1, tzinfo=UTC))

    db.after, db.between = "SELECT MAX(updated_at)", newer_saves
    _at(monkeypatch, datetime(2026, 10, 18, 10, 0, 1, tzinfo=UTC))
    older.save({"/a.html": "1", "/c.html": "1"})

    assert older.load() == {"/a.html": "2", "/b.html": "2"}
