"""The deployed site's file list, and publishing from it without a local archive."""

from datetime import UTC, datetime

import httpx
import pytest
from fakes import SqliteD1

from cyris.adapters.output import publish as publish_mod
from cyris.adapters.output.pages_manifest import D1PagesManifest
from cyris.adapters.output.pages_receipt import D1PagesDeployReceipt


@pytest.fixture
def manifest():
    return D1PagesManifest(SqliteD1())


def test_the_manifest_round_trips(manifest):
    manifest.save({"/index.html": "aaa", "/2026-08-27-morning.html": "bbb"})

    assert manifest.load() == {"/index.html": "aaa", "/2026-08-27-morning.html": "bbb"}


def test_saving_replaces_rather_than_merges(manifest):
    """A path that left the site has to leave the table: otherwise the next deploy
    names a file whose bytes Cloudflare may not hold, and the whole deploy fails."""
    manifest.save({"/a.html": "1", "/b.html": "2"})

    manifest.save({"/a.html": "1"})

    assert manifest.load() == {"/a.html": "1"}


def test_an_empty_manifest_is_refused(manifest):
    """Storing it would describe a site with no pages, and a Pages deployment is a
    full snapshot — the next deploy would empty the archive."""
    manifest.save({"/a.html": "1"})

    with pytest.raises(ValueError, match="empty"):
        manifest.save({})

    assert manifest.load() == {"/a.html": "1"}


class _Receipt:
    def __init__(self, present=False):
        self.present = present
        self.exists_calls = 0
        self.records = []

    def exists(self, project):
        self.exists_calls += 1
        return self.present

    def record(self, project):
        self.records.append(project)
        self.present = True


class _Store:
    def __init__(self, manifest):
        self.manifest = manifest
        self.saved = None

    def load(self):
        return dict(self.manifest)

    def save(self, manifest):
        self.saved = manifest


def _stub_client(monkeypatch, *, deployed):
    def deploy_manifest(_self, new_files, manifest, recover, branch="main"):
        deployed.append((new_files, manifest))
        merged = {**manifest, **{p: "new" for p in new_files}}
        return "dep-1", merged

    monkeypatch.setattr(publish_mod.PagesClient, "deploy_manifest", deploy_manifest)


def test_the_run_deploys_its_pages_plus_the_whole_archive(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setattr(publish_mod, "_page_is_live", lambda _p, _s: True)
    deployed = []
    _stub_client(monkeypatch, deployed=deployed)
    store = _Store({"/2026-08-26-evening.html": "old"})

    ok = publish_mod.publish_site(
        {"/2026-08-27-morning.html": b"<html>x</html>"},
        "2026-08-27-morning",
        store,
        "proj",
        _Receipt(),
    )

    assert ok is True
    assert deployed[0][1] == {"/2026-08-26-evening.html": "old"}
    assert store.saved == {"/2026-08-26-evening.html": "old", "/2026-08-27-morning.html": "new"}


def test_a_populated_manifest_does_not_probe_or_touch_the_receipt(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setattr(publish_mod, "_page_is_live", lambda _p, _s: True)

    def probed(_self):
        raise AssertionError("probed")

    monkeypatch.setattr(publish_mod.PagesClient, "has_deployments", probed)
    _stub_client(monkeypatch, deployed=[])
    receipt = _Receipt()

    ok = publish_mod.publish_site(
        {"/2026-08-27-morning.html": b"<html>x</html>"},
        "2026-08-27-morning",
        _Store({"/2026-08-26-evening.html": "old"}),
        "proj",
        receipt,
    )

    assert ok is True
    assert receipt.exists_calls == 0
    assert receipt.records == []


def test_a_deploy_that_never_went_live_does_not_update_the_manifest(monkeypatch):
    """The manifest describes the deployed site. Recording a deploy that did not
    land would describe a site that does not exist."""
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setattr(publish_mod, "_page_is_live", lambda _p, _s: False)
    _stub_client(monkeypatch, deployed=[])
    store = _Store({"/old.html": "old"})

    assert publish_mod.publish_site({"/new.html": b"x"}, "slug", store, "proj", _Receipt()) is False
    assert store.saved is None


def test_an_archived_page_is_recovered_from_the_live_site(monkeypatch):
    """Pages 308s `.html` to the clean URL, so the redirect has to be followed or
    the bytes come back as a redirect body."""
    seen = {}

    def get(url, **kwargs):
        seen["url"] = url
        seen["follow"] = kwargs.get("follow_redirects")
        return httpx.Response(200, content=b"<html>archived</html>")

    monkeypatch.setattr(publish_mod.httpx, "get", get)

    assert publish_mod._fetch_live("proj", "/2026-08-01-morning.html") == b"<html>archived</html>"
    assert seen["url"] == "https://proj.pages.dev/2026-08-01-morning"
    assert seen["follow"] is True


def test_a_page_the_site_cannot_serve_back_is_not_silently_dropped(monkeypatch):
    monkeypatch.setattr(publish_mod.httpx, "get", lambda _u, **_k: httpx.Response(404, content=b""))

    assert publish_mod._fetch_live("proj", "/gone.html") is None


def test_bootstrap_partial_accepts_run_digest_calling_convention(monkeypatch):
    """`Deps.publish_site` promises Callable[[files, slug], bool]; bootstrap binds
    manifest_store/pages_project by keyword. The two met for the first time in
    production and collided on the second positional — this pins the seam."""
    from functools import partial

    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setattr(publish_mod, "_page_is_live", lambda _p, _s: True)
    monkeypatch.setattr(publish_mod.PagesClient, "has_deployments", lambda _self: False)
    _stub_client(monkeypatch, deployed=[])
    store = _Store({})

    wired = partial(
        publish_mod.publish_site,
        manifest_store=store,
        pages_project="proj",
        receipt_store=_Receipt(),
    )

    assert wired({"/2026-08-28-evening.html": b"<html>x</html>"}, "2026-08-28-evening") is True


def test_a_first_ever_deploy_goes_through(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setattr(publish_mod, "_page_is_live", lambda _p, _s: True)
    monkeypatch.setattr(publish_mod.PagesClient, "has_deployments", lambda _self: False)
    deployed = []
    _stub_client(monkeypatch, deployed=deployed)
    store = _Store({})

    ok = publish_mod.publish_site(
        {"/2026-08-27-morning.html": b"<html>x</html>"},
        "2026-08-27-morning",
        store,
        "proj",
        _Receipt(),
    )

    assert ok is True
    assert deployed[0][1] == {}
    assert "/2026-08-27-morning.html" in store.saved


def test_an_empty_manifest_refuses_when_the_probe_fails(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")

    def probe(_self):
        raise publish_mod.PagesDeployError("GET ... -> 404 Project not found")

    monkeypatch.setattr(publish_mod.PagesClient, "has_deployments", probe)
    deployed = []
    _stub_client(monkeypatch, deployed=deployed)

    ok = publish_mod.publish_site({"/new.html": b"x"}, "slug", _Store({}), "proj", _Receipt())

    assert ok is False
    assert deployed == []


def test_an_empty_manifest_refuses_when_the_probe_cannot_connect(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")

    def boom(_self):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(publish_mod.PagesClient, "has_deployments", boom)
    deployed = []
    _stub_client(monkeypatch, deployed=deployed)

    ok = publish_mod.publish_site({"/new.html": b"x"}, "slug", _Store({}), "proj", _Receipt())

    assert ok is False
    assert deployed == []


def test_an_empty_manifest_refuses_when_receipt_lookup_fails(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    deployed = []
    _stub_client(monkeypatch, deployed=deployed)

    class _Down:
        def exists(self, project):
            raise RuntimeError("d1 down")

        def record(self, project):
            raise AssertionError("record")

    ok = publish_mod.publish_site({"/new.html": b"x"}, "slug", _Store({}), "proj", _Down())

    assert ok is False
    assert deployed == []


def test_record_writes_one_row_with_iso8601_created_at():
    db = SqliteD1()
    D1PagesDeployReceipt(db).record("proj")

    rows = db.query("SELECT project, created_at FROM pages_deploy_receipt").rows
    assert len(rows) == 1
    assert rows[0]["project"] == "proj"
    assert rows[0]["created_at"]
    datetime.fromisoformat(rows[0]["created_at"])


def test_record_is_idempotent_when_the_clock_moves(monkeypatch):
    from cyris.adapters.output import pages_receipt as receipt_mod

    times = iter(
        [
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 6, 1, tzinfo=UTC),
        ]
    )

    class _DateTime:
        @staticmethod
        def now(tz=None):
            return next(times)

    monkeypatch.setattr(receipt_mod, "datetime", _DateTime)
    db = SqliteD1()
    store = D1PagesDeployReceipt(db)
    store.record("proj")
    first = db.query("SELECT created_at FROM pages_deploy_receipt").rows[0]["created_at"]
    store.record("proj")
    rows = db.query("SELECT project, created_at FROM pages_deploy_receipt").rows
    assert len(rows) == 1
    assert rows[0]["created_at"] == first


def test_the_receipt_table_comes_from_schema_sql():
    """SqliteD1 loads schema.sql; the store issues no DDL."""
    db = SqliteD1(with_schema=True)
    names = {
        row["name"] for row in db.query("SELECT name FROM sqlite_master WHERE type = 'table'").rows
    }
    assert "pages_deploy_receipt" in names


def test_exists_is_false_on_an_empty_table():
    assert D1PagesDeployReceipt(SqliteD1()).exists("proj") is False


def test_exists_is_true_after_record():
    store = D1PagesDeployReceipt(SqliteD1())
    store.record("proj")
    assert store.exists("proj") is True


def test_exists_is_keyed_by_project():
    store = D1PagesDeployReceipt(SqliteD1())
    store.record("proj")
    assert store.exists("other") is False
