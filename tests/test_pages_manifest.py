"""The deployed site's file list, and publishing from it without a local archive."""

import httpx
import pytest
from fakes import SqliteD1

from cyris.adapters.output import publish as publish_mod
from cyris.adapters.output.pages_manifest import D1PagesManifest


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
        {"/2026-08-27-morning.html": b"<html>x</html>"}, "2026-08-27-morning", store, "proj"
    )

    assert ok is True
    assert deployed[0][1] == {"/2026-08-26-evening.html": "old"}
    assert store.saved == {"/2026-08-26-evening.html": "old", "/2026-08-27-morning.html": "new"}


def test_a_deploy_that_never_went_live_does_not_update_the_manifest(monkeypatch):
    """The manifest describes the deployed site. Recording a deploy that did not
    land would describe a site that does not exist."""
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setattr(publish_mod, "_page_is_live", lambda _p, _s: False)
    _stub_client(monkeypatch, deployed=[])
    store = _Store({"/old.html": "old"})

    assert publish_mod.publish_site({"/new.html": b"x"}, "slug", store, "proj") is False
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
    _stub_client(monkeypatch, deployed=[])
    store = _Store({})

    wired = partial(publish_mod.publish_site, manifest_store=store, pages_project="proj")

    assert wired({"/2026-08-28-evening.html": b"<html>x</html>"}, "2026-08-28-evening") is True
