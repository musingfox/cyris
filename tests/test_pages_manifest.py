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


def _skip_live_index(monkeypatch):
    monkeypatch.setattr(publish_mod, "_fetch_live_index", lambda _p: set())


def test_the_run_deploys_its_pages_plus_the_whole_archive(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setattr(publish_mod, "_page_is_live", lambda _p, _s: True)
    _skip_live_index(monkeypatch)
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
    _skip_live_index(monkeypatch)

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
    _skip_live_index(monkeypatch)
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


def test_an_empty_manifest_refuses_when_the_project_already_has_deployments(monkeypatch, caplog):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setattr(publish_mod.PagesClient, "has_deployments", lambda _self: True)
    deployed = []
    _stub_client(monkeypatch, deployed=deployed)
    store = _Store({})
    receipt = _Receipt()

    with caplog.at_level("ERROR"):
        ok = publish_mod.publish_site({"/new.html": b"x"}, "slug", store, "proj", receipt)

    assert ok is False
    assert deployed == []
    assert store.saved is None
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert "[store] database_id" in errors[0].message
    assert "scripts/backfill_pages_manifest.py" in errors[0].message
    assert receipt.records == []


def test_the_receipt_is_written_before_upload(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setattr(publish_mod, "_page_is_live", lambda _p, _s: True)
    monkeypatch.setattr(publish_mod.PagesClient, "has_deployments", lambda _self: False)
    events = []

    class _OrderedReceipt(_Receipt):
        def record(self, project):
            events.append("receipt")
            super().record(project)

    def deploy_manifest(_self, new_files, manifest, recover, branch="main"):
        events.append("deploy")
        return "dep-1", {**manifest, **{p: "new" for p in new_files}}

    monkeypatch.setattr(publish_mod.PagesClient, "deploy_manifest", deploy_manifest)

    publish_mod.publish_site({"/new.html": b"x"}, "slug", _Store({}), "proj", _OrderedReceipt())
    assert events[:2] == ["receipt", "deploy"]


def test_a_receipt_skips_the_probe_on_the_next_empty_manifest_run(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    _skip_live_index(monkeypatch)
    probes = []

    def probe(_self):
        probes.append(True)
        return False

    monkeypatch.setattr(publish_mod.PagesClient, "has_deployments", probe)
    _stub_client(monkeypatch, deployed=[])
    store = _Store({})
    receipt = _Receipt()
    live = [False]
    monkeypatch.setattr(publish_mod, "_page_is_live", lambda _p, _s: live[0])

    first = publish_mod.publish_site({"/new.html": b"x"}, "slug", store, "proj", receipt)
    assert first is False
    assert store.saved is None
    assert receipt.present is True
    assert len(probes) == 1

    live[0] = True
    second = publish_mod.publish_site({"/new.html": b"x"}, "slug", store, "proj", receipt)
    assert second is True
    assert len(probes) == 1


def test_a_preexisting_receipt_does_not_probe(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setattr(publish_mod, "_page_is_live", lambda _p, _s: True)
    _skip_live_index(monkeypatch)

    def probed(_self):
        raise AssertionError("probed")

    monkeypatch.setattr(publish_mod.PagesClient, "has_deployments", probed)
    _stub_client(monkeypatch, deployed=[])

    ok = publish_mod.publish_site(
        {"/new.html": b"x"}, "slug", _Store({}), "proj", _Receipt(present=True)
    )
    assert ok is True


def test_a_failed_upload_still_leaves_the_receipt(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setattr(publish_mod.PagesClient, "has_deployments", lambda _self: False)

    def deploy_manifest(_self, new_files, manifest, recover, branch="main"):
        raise publish_mod.PagesDeployError("upload failed")

    monkeypatch.setattr(publish_mod.PagesClient, "deploy_manifest", deploy_manifest)
    receipt = _Receipt()

    ok = publish_mod.publish_site({"/new.html": b"x"}, "slug", _Store({}), "proj", receipt)

    assert ok is False
    assert receipt.present is True
    assert receipt.records == ["proj"]


def test_a_receipt_write_failure_does_not_upload(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setattr(publish_mod.PagesClient, "has_deployments", lambda _self: False)
    deployed = []
    _stub_client(monkeypatch, deployed=deployed)

    class _WriteFail(_Receipt):
        def record(self, project):
            raise RuntimeError("d1 down")

    ok = publish_mod.publish_site({"/new.html": b"x"}, "slug", _Store({}), "proj", _WriteFail())

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


def _dated(n, *, month=1):
    paths = []
    for i in range(n):
        day = i // 2 + 1
        period = "morning" if i % 2 == 0 else "evening"
        paths.append(f"/2026-{month:02d}-{day:02d}-{period}.html")
    return paths


def test_archive_shortfall_is_empty_when_every_live_page_is_in_the_manifest():
    live = set(_dated(62))
    manifest = {p: "h" for p in live}
    for i in range(1, 25):
        manifest[f"/2026-01-{i:02d}-morning-raw.html"] = "r"
    manifest["/index.html"] = "i"

    assert publish_mod._archive_shortfall(live, manifest) == set()


def test_archive_shortfall_names_each_page_a_same_size_wrong_d1_would_drop():
    live = set(_dated(62, month=1))
    manifest = {p: "h" for p in _dated(62, month=2)}
    manifest["/index.html"] = "i"

    assert publish_mod._archive_shortfall(live, manifest) == live


def test_archive_shortfall_names_the_pages_a_truncated_manifest_would_drop():
    live = _dated(62)
    kept = live[:3]
    manifest = {p: "h" for p in kept}
    manifest["/index.html"] = "i"

    assert publish_mod._archive_shortfall(set(live), manifest) == set(live[3:])


def test_archive_shortfall_names_the_one_page_a_live_check_flake_would_drop():
    live = {"/2026-09-04-morning.html", "/2026-09-04-evening.html"}
    manifest = {"/2026-09-04-morning.html": "h"}

    assert publish_mod._archive_shortfall(live, manifest) == {"/2026-09-04-evening.html"}


def test_archive_shortfall_treats_an_unslashed_manifest_key_as_the_same_page():
    live = {"/2026-09-04-morning.html"}
    manifest = {"2026-09-04-morning.html": "h"}

    assert publish_mod._archive_shortfall(live, manifest) == set()


def test_archive_shortfall_is_empty_when_the_live_archive_lists_nothing():
    manifest = {"/a.html": "1", "/b.html": "2", "/c.html": "3"}
    assert publish_mod._archive_shortfall(set(), manifest) == set()


def _env(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setattr(publish_mod, "_page_is_live", lambda _p, _s: True)


def _counting_get(monkeypatch, *, body=b""):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs.get("follow_redirects")))
        return httpx.Response(200, content=body)

    monkeypatch.setattr(publish_mod.httpx, "get", get)
    return calls


def test_a_populated_manifest_reads_the_live_index_once_without_the_receipt(monkeypatch):
    _env(monkeypatch)
    calls = _counting_get(monkeypatch)
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
    assert len(calls) == 1
    assert receipt.exists_calls == 0


def test_an_empty_manifest_with_a_preexisting_receipt_reads_the_live_index_once(monkeypatch):
    _env(monkeypatch)
    calls = _counting_get(monkeypatch)

    def probed(_self):
        raise AssertionError("probed")

    monkeypatch.setattr(publish_mod.PagesClient, "has_deployments", probed)
    _stub_client(monkeypatch, deployed=[])

    ok = publish_mod.publish_site(
        {"/new.html": b"x"}, "slug", _Store({}), "proj", _Receipt(present=True)
    )

    assert ok is True
    assert len(calls) == 1


def test_a_first_ever_deploy_does_not_read_the_live_index(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setattr(publish_mod.PagesClient, "has_deployments", lambda _self: False)
    _stub_client(monkeypatch, deployed=[])

    def fetched(*_a, **_k):
        raise AssertionError("fetched")

    monkeypatch.setattr(publish_mod.httpx, "get", fetched)

    ok = publish_mod.publish_site(
        {"/2026-08-27-morning.html": b"<html>x</html>"},
        "2026-08-27-morning",
        _Store({}),
        "proj",
        _Receipt(),
    )

    assert ok is True


def test_deploy_retries_do_not_reread_the_live_index(monkeypatch):
    _env(monkeypatch)
    calls = _counting_get(monkeypatch)
    deployed = []
    n = {"i": 0}

    def deploy_manifest(_self, new_files, manifest, recover, branch="main"):
        n["i"] += 1
        if n["i"] <= 2:
            raise publish_mod.PagesDeployError("upload failed")
        deployed.append((new_files, manifest))
        merged = {**manifest, **{p: "new" for p in new_files}}
        return "dep-1", merged

    monkeypatch.setattr(publish_mod.PagesClient, "deploy_manifest", deploy_manifest)

    ok = publish_mod.publish_site(
        {"/new.html": b"x"}, "slug", _Store({"/old.html": "old"}), "proj", _Receipt()
    )

    assert ok is True
    assert len(calls) == 1
    assert len(deployed) == 1


def test_parse_archive_anchors_from_the_real_index_template(tmp_path):
    from cyris.adapters.output.html_digest import HtmlDigestWriter

    html = HtmlDigestWriter(tmp_path).render_index(
        [
            "2026-09-04-morning.html",
            "2026-09-03-evening.html",
            "2026-09-03-morning.html",
            "index.html",
            "2026-09-03-morning-raw.html",
        ]
    )

    assert publish_mod._parse_archive_anchors(html) == {
        "/2026-09-04-morning.html",
        "/2026-09-03-evening.html",
        "/2026-09-03-morning.html",
    }


def test_parse_archive_anchors_from_an_empty_archive_index(tmp_path):
    from cyris.adapters.output.html_digest import HtmlDigestWriter

    html = HtmlDigestWriter(tmp_path).render_index([])

    assert publish_mod._parse_archive_anchors(html) == set()


def test_parse_archive_anchors_does_not_double_slash_an_absolute_href():
    assert publish_mod._parse_archive_anchors('<a href="/2026-09-04-morning.html">') == {
        "/2026-09-04-morning.html"
    }


def test_parse_archive_anchors_ignores_undated_hrefs():
    html = '<a href="/"></a><a href="https://example.com/about.html">'
    assert publish_mod._parse_archive_anchors(html) == set()


def _index_body(*paths: str) -> bytes:
    return "".join(f'<a href="{p}">' for p in paths).encode()


def test_fetch_live_index_returns_anchors_on_the_first_200(monkeypatch):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs.get("follow_redirects")))
        return httpx.Response(
            200, content=_index_body("/2026-09-04-morning.html", "/2026-09-04-evening.html")
        )

    monkeypatch.setattr(publish_mod.httpx, "get", get)

    assert publish_mod._fetch_live_index("proj") == {
        "/2026-09-04-morning.html",
        "/2026-09-04-evening.html",
    }
    assert calls == [("https://proj.pages.dev/", True)]


def test_fetch_live_index_retries_connect_errors_then_returns_anchors(monkeypatch):
    monkeypatch.setattr(publish_mod.time, "sleep", lambda _s: None)
    n = {"i": 0}

    def get(url, **kwargs):
        n["i"] += 1
        if n["i"] < 3:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, content=_index_body("/2026-09-04-morning.html"))

    monkeypatch.setattr(publish_mod.httpx, "get", get)

    assert publish_mod._fetch_live_index("proj") == {"/2026-09-04-morning.html"}
    assert n["i"] == 3


def test_fetch_live_index_gives_up_after_three_404s(monkeypatch, caplog):
    monkeypatch.setattr(publish_mod.time, "sleep", lambda _s: None)
    n = {"i": 0}

    def get(url, **kwargs):
        n["i"] += 1
        return httpx.Response(404, content=b"")

    monkeypatch.setattr(publish_mod.httpx, "get", get)

    with caplog.at_level("WARNING"):
        assert publish_mod._fetch_live_index("proj") is None
    assert n["i"] == 3
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 3


def test_fetch_live_index_empty_archive_is_an_empty_set_not_unread(monkeypatch, tmp_path):
    from cyris.adapters.output.html_digest import HtmlDigestWriter

    body = HtmlDigestWriter(tmp_path).render_index([]).encode()
    monkeypatch.setattr(
        publish_mod.httpx, "get", lambda _u, **_k: httpx.Response(200, content=body)
    )

    assert publish_mod._fetch_live_index("proj") == set()


def _publish_env(monkeypatch, *, live_paths, deployed):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setattr(publish_mod, "_page_is_live", lambda _p, _s: True)
    body = _index_body(*live_paths)
    monkeypatch.setattr(
        publish_mod.httpx, "get", lambda _u, **_k: httpx.Response(200, content=body)
    )
    _stub_client(monkeypatch, deployed=deployed)


def test_publish_refuses_when_the_manifest_would_drop_most_of_the_live_archive(monkeypatch, caplog):
    live = _dated(62)
    deployed = []
    _publish_env(monkeypatch, live_paths=live, deployed=deployed)
    store = _Store({**{p: "h" for p in live[:3]}, "/index.html": "i"})

    with caplog.at_level("ERROR"):
        ok = publish_mod.publish_site({"/new.html": b"x"}, "slug", store, "proj", _Receipt())

    assert ok is False
    assert deployed == []
    assert store.saved is None
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert "[store] database_id" in errors[0].message
    assert "59" in errors[0].message


def test_publish_deploys_when_the_manifest_holds_the_live_archive(monkeypatch):
    live = _dated(62)
    deployed = []
    _publish_env(monkeypatch, live_paths=live, deployed=deployed)
    manifest = {p: "h" for p in live}
    for i in range(1, 25):
        manifest[f"/2026-01-{i:02d}-morning-raw.html"] = "r"
    manifest["/index.html"] = "i"
    store = _Store(manifest)

    ok = publish_mod.publish_site({"/new.html": b"x"}, "slug", store, "proj", _Receipt())

    assert ok is True
    assert len(deployed) == 1
    assert store.saved is not None


def test_publish_deploys_when_exactly_four_live_pages_are_missing(monkeypatch):
    live = _dated(62)
    deployed = []
    _publish_env(monkeypatch, live_paths=live, deployed=deployed)
    store = _Store({p: "h" for p in live[4:]})

    ok = publish_mod.publish_site({"/new.html": b"x"}, "slug", store, "proj", _Receipt())

    assert ok is True
    assert len(deployed) == 1


def test_publish_refuses_when_five_live_pages_are_missing(monkeypatch, caplog):
    live = _dated(62)
    deployed = []
    _publish_env(monkeypatch, live_paths=live, deployed=deployed)
    store = _Store({p: "h" for p in live[5:]})

    with caplog.at_level("ERROR"):
        ok = publish_mod.publish_site({"/new.html": b"x"}, "slug", store, "proj", _Receipt())

    assert ok is False
    assert deployed == []
    # A count alone cannot separate a wrong database_id from a deliberate
    # prune; the names can, so the refusal has to carry some of them.
    message = caplog.text
    assert "database_id" in message
    assert any(page.lstrip("/") in message for page in live[:5])


def test_publish_deploys_when_the_live_index_has_no_anchors(monkeypatch):
    deployed = []
    _publish_env(monkeypatch, live_paths=(), deployed=deployed)
    store = _Store({"/old.html": "old"})

    ok = publish_mod.publish_site({"/new.html": b"x"}, "slug", store, "proj", _Receipt())

    assert ok is True
    assert len(deployed) == 1


def test_publish_refuses_an_empty_manifest_with_receipt_against_a_full_archive(monkeypatch, caplog):
    live = _dated(62)
    deployed = []
    _publish_env(monkeypatch, live_paths=live, deployed=deployed)
    store = _Store({})

    with caplog.at_level("ERROR"):
        ok = publish_mod.publish_site(
            {"/new.html": b"x"}, "slug", store, "proj", _Receipt(present=True)
        )

    assert ok is False
    assert deployed == []
    errors = [r.message for r in caplog.records if r.levelname == "ERROR"]
    assert errors
    assert "[store] database_id" in errors[0]


def test_publish_deploys_an_empty_manifest_with_receipt_inside_tolerance(monkeypatch):
    deployed = []
    _publish_env(monkeypatch, live_paths=("/2026-09-04-morning.html",), deployed=deployed)

    ok = publish_mod.publish_site(
        {"/new.html": b"x"}, "slug", _Store({}), "proj", _Receipt(present=True)
    )

    assert ok is True
    assert len(deployed) == 1


def test_publish_refuses_when_the_live_archive_cannot_be_read(monkeypatch, caplog):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setattr(publish_mod, "_page_is_live", lambda _p, _s: True)
    monkeypatch.setattr(publish_mod.time, "sleep", lambda _s: None)

    def boom(_u, **_k):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(publish_mod.httpx, "get", boom)
    deployed = []
    _stub_client(monkeypatch, deployed=deployed)
    store = _Store({"/old.html": "old"})

    with caplog.at_level("ERROR"):
        ok = publish_mod.publish_site({"/new.html": b"x"}, "slug", store, "proj", _Receipt())

    assert ok is False
    assert deployed == []
    assert store.saved is None
    errors = [r.message for r in caplog.records if r.levelname == "ERROR"]
    assert any("could not be read" in m.lower() for m in errors)
    assert all("[store] database_id" not in m for m in errors)


def test_publish_refuses_when_the_live_archive_answers_500(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setattr(publish_mod, "_page_is_live", lambda _p, _s: True)
    monkeypatch.setattr(publish_mod.time, "sleep", lambda _s: None)
    monkeypatch.setattr(publish_mod.httpx, "get", lambda _u, **_k: httpx.Response(500, content=b""))
    deployed = []
    _stub_client(monkeypatch, deployed=deployed)

    ok = publish_mod.publish_site(
        {"/new.html": b"x"}, "slug", _Store({"/old.html": "old"}), "proj", _Receipt()
    )

    assert ok is False
    assert deployed == []
