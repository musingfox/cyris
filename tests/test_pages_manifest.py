"""The deployed site's file list, and publishing from it without a local archive."""

import re
from datetime import UTC, datetime

import httpx
import pytest
from fakes import SqliteD1

from cyris.adapters.output import publish as publish_mod
from cyris.adapters.output.pages_deploy import DeploymentRecord
from cyris.adapters.output.pages_manifest import D1PagesManifest
from cyris.adapters.output.pages_receipt import D1PagesDeployReceipt

pytestmark = pytest.mark.integration

LANDED = DeploymentRecord("dep-1", "https://ab12.proj.pages.dev", "deploy", "success")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(publish_mod.time, "sleep", lambda _s: None)


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


def _stub_client(monkeypatch, *, deployed, records=(LANDED,)):
    """`records` are the deployments Cloudflare reports on creation, in turn; the
    last one repeats."""

    def deploy_manifest(_self, new_files, manifest, recover, branch="main"):
        deployed.append((new_files, manifest))
        merged = {**manifest, **{p: "new" for p in new_files}}
        return records[min(len(deployed), len(records)) - 1], merged

    monkeypatch.setattr(publish_mod.PagesClient, "deploy_manifest", deploy_manifest)


def _skip_live_index(monkeypatch):
    monkeypatch.setattr(publish_mod, "_fetch_live_index", lambda _p: set())


def test_the_run_deploys_its_pages_plus_the_whole_archive(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setattr(publish_mod, "_page_is_live", lambda *_a, **_k: True)
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
    monkeypatch.setattr(publish_mod, "_page_is_live", lambda *_a, **_k: True)
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


def _stub_stages(monkeypatch, *stages):
    """`get_deployment` answers each record in turn (an exception is raised); the
    last one repeats. Returns the ids it was asked for."""
    asked = []

    def get_deployment(_self, deployment_id):
        asked.append(deployment_id)
        answer = stages[min(len(asked), len(stages)) - 1]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(publish_mod.PagesClient, "get_deployment", get_deployment)
    return asked


def test_the_create_stage_is_logged_once(monkeypatch, caplog):
    """Whether a direct upload is already deploy/success on creation is unobserved;
    one production log line has to answer it."""
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setattr(publish_mod, "_page_is_live", lambda *_a, **_k: True)
    _skip_live_index(monkeypatch)
    queued = DeploymentRecord("dep-1", "https://ab12.proj.pages.dev", "queued", "active")
    _stub_client(monkeypatch, deployed=[], records=(queued,))
    _stub_stages(monkeypatch, LANDED)

    with caplog.at_level("INFO"):
        publish_mod.publish_site({"/new.html": b"x"}, "slug", _Store({}), "proj", _Receipt(True))

    tokens = ("dep-1", "queued", "active", "https://ab12.proj.pages.dev")
    matching = [
        r
        for r in caplog.records
        if r.levelname == "INFO" and all(t in r.getMessage() for t in tokens)
    ]
    assert len(matching) == 1


QUEUED = DeploymentRecord("dep-1", "https://ab12.proj.pages.dev", "queued", "active")
ACTIVE = DeploymentRecord("dep-1", "https://ab12.proj.pages.dev", "deploy", "active")


def _publish_with_stages(monkeypatch, *, created, stages, live=True):
    """publish_site over a populated manifest: the create call reports `created`,
    each re-read answers the next of `stages`. Returns (result, deployed, asked, store)."""
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setattr(publish_mod, "_page_is_live", lambda *_a, **_k: live)
    _skip_live_index(monkeypatch)
    deployed = []
    _stub_client(monkeypatch, deployed=deployed, records=created)
    asked = _stub_stages(monkeypatch, *stages)
    store = _Store({"/old.html": "old"})
    ok = publish_mod.publish_site({"/new.html": b"x"}, "slug", store, "proj", _Receipt())
    return ok, deployed, asked, store


def test_a_deployment_landed_on_creation_is_not_reread(monkeypatch):
    _ok, _deployed, asked, _store = _publish_with_stages(
        monkeypatch, created=(LANDED,), stages=(LANDED,)
    )

    assert asked == []


def test_a_queued_deployment_is_reread_until_it_lands(monkeypatch):
    _ok, _deployed, asked, store = _publish_with_stages(
        monkeypatch, created=(QUEUED,), stages=(ACTIVE, ACTIVE, LANDED)
    )

    assert asked == ["dep-1"] * 3
    assert store.saved == {"/old.html": "old", "/new.html": "new"}


def test_a_failed_stage_read_is_a_spent_poll_not_a_failed_deployment(monkeypatch, caplog):
    with caplog.at_level("WARNING", logger=publish_mod.logger.name):
        _ok, _deployed, asked, store = _publish_with_stages(
            monkeypatch, created=(QUEUED,), stages=(httpx.ConnectError("reset"), LANDED)
        )

    assert len(asked) == 2
    assert store.saved == {"/old.html": "old", "/new.html": "new"}
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1


def test_a_deployment_that_never_reports_a_verdict_is_reread_a_bounded_number_of_times(
    monkeypatch,
):
    _ok, _deployed, asked, _store = _publish_with_stages(
        monkeypatch, created=(QUEUED,), stages=(ACTIVE,)
    )

    assert len(asked) == publish_mod.STAGE_POLLS == 6


FAILED = DeploymentRecord("dep-1", "https://ab12.proj.pages.dev", "deploy", "failure")
CANCELED = DeploymentRecord("dep-1", "https://ab12.proj.pages.dev", "deploy", "canceled")


def test_a_deployment_cloudflare_reports_failed_is_deployed_again(monkeypatch, caplog):
    with caplog.at_level("ERROR"):
        ok, deployed, _asked, store = _publish_with_stages(
            monkeypatch, created=(FAILED, LANDED), stages=(LANDED,)
        )

    assert len(deployed) == 2
    assert store.saved is not None
    assert ok is True
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any("dep-1" in m and "failure" in m for m in errors)


def test_a_deployment_canceled_while_waiting_is_deployed_again(monkeypatch):
    ok, deployed, _asked, store = _publish_with_stages(
        monkeypatch, created=(QUEUED, LANDED), stages=(CANCELED,)
    )

    assert len(deployed) == 2
    assert store.saved == {"/old.html": "old", "/new.html": "new"}
    assert ok is True


def test_failed_deployments_are_retried_a_bounded_number_of_times(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")

    def verified(*_a, **_k):  # pragma: no cover - must not be reached
        raise AssertionError("verified a deployment Cloudflare says failed")

    monkeypatch.setattr(publish_mod, "_page_is_live", verified)
    _skip_live_index(monkeypatch)
    deployed = []
    _stub_client(monkeypatch, deployed=deployed, records=(FAILED,))
    store = _Store({"/old.html": "old"})

    ok = publish_mod.publish_site({"/new.html": b"x"}, "slug", store, "proj", _Receipt())

    assert ok is False
    assert len(deployed) == publish_mod.DEPLOY_ATTEMPTS == 3
    assert store.saved is None


def test_a_landed_deployment_is_recorded_even_before_its_page_is_live(monkeypatch):
    """On 2026-09-24 a deployment reached `success` within 2s while the alias still
    served the fallback, and the page was lost: the manifest only recorded pages
    seen live, so the next full-snapshot deploy dropped it. What Cloudflare says it
    deployed is recorded; what the alias serves decides only the return. And a
    deployment that succeeded is waited on, not replaced: three identical redeploys
    that morning each restarted the wait they were meant to end."""
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setattr(publish_mod, "_page_is_live", lambda *_a, **_k: False)
    _skip_live_index(monkeypatch)
    deployed = []
    _stub_client(monkeypatch, deployed=deployed)
    store = _Store({"/old.html": "old"})

    assert publish_mod.publish_site({"/new.html": b"x"}, "slug", store, "proj", _Receipt()) is False
    assert store.saved == {"/old.html": "old", "/new.html": "new"}
    assert len(deployed) == 1


def test_a_landed_deployment_whose_page_is_not_live_is_named_as_recorded(monkeypatch, caplog):
    """The publish fails, so the operator has to learn that the page will still come:
    which deployment it was, where to open it, and that the manifest kept it."""
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setattr(publish_mod, "_page_is_live", lambda *_a, **_k: False)
    _skip_live_index(monkeypatch)
    _stub_client(monkeypatch, deployed=[])

    with caplog.at_level("WARNING", logger=publish_mod.logger.name):
        publish_mod.publish_site({"/new.html": b"x"}, "slug", _Store({}), "proj", _Receipt(True))

    tokens = ("dep-1", "https://ab12.proj.pages.dev", "manifest")
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelname == "WARNING" and all(t in r.getMessage() for t in tokens)
    ]
    assert len(warnings) == 1


def test_a_deployment_that_lands_after_creation_is_recorded(monkeypatch):
    ok, _deployed, _asked, store = _publish_with_stages(
        monkeypatch, created=(QUEUED,), stages=(LANDED,)
    )

    assert ok is True
    assert store.saved == {"/old.html": "old", "/new.html": "new"}


def test_a_deployment_without_a_verdict_is_recorded_once_its_page_is_live(monkeypatch):
    """Whether a direct upload ever reports deploy/success is unobserved. If it never
    does, the live page is still a receipt the manifest can follow."""
    ok, deployed, _asked, store = _publish_with_stages(
        monkeypatch, created=(QUEUED,), stages=(ACTIVE,)
    )

    assert store.saved == {"/old.html": "old", "/new.html": "new"}
    assert ok is True
    assert len(deployed) == 1


def test_a_deployment_with_no_stage_at_all_is_recorded_once_its_page_is_live(monkeypatch):
    blank = DeploymentRecord("dep-1", None, None, None)
    ok, _deployed, _asked, store = _publish_with_stages(
        monkeypatch, created=(blank,), stages=(blank,)
    )

    assert store.saved == {"/old.html": "old", "/new.html": "new"}
    assert ok is True


def test_a_landed_deployment_whose_page_is_live_is_published(monkeypatch):
    ok, _deployed, _asked, _store = _publish_with_stages(
        monkeypatch, created=(LANDED,), stages=(LANDED,), live=True
    )

    assert ok is True


def test_a_deployment_with_no_verdict_and_no_live_page_is_a_clear_failure(monkeypatch, caplog):
    with caplog.at_level("ERROR"):
        ok, deployed, _asked, store = _publish_with_stages(
            monkeypatch, created=(QUEUED,), stages=(QUEUED,), live=False
        )

    assert ok is False
    assert len(deployed) == 1
    assert store.saved is None
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any("dep-1" in m and "queued" in m for m in errors)


def test_a_manifest_that_cannot_be_saved_fails_the_publish_loudly(monkeypatch):
    """The next full snapshot would drop this page, so the operator has to hear it."""
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setattr(publish_mod, "_page_is_live", lambda *_a, **_k: True)
    _skip_live_index(monkeypatch)
    _stub_client(monkeypatch, deployed=[])

    class _Down(_Store):
        def save(self, manifest):
            raise RuntimeError("d1 down")

    with pytest.raises(RuntimeError, match="d1 down"):
        publish_mod.publish_site(
            {"/new.html": b"x"}, "slug", _Down({"/old.html": "old"}), "proj", _Receipt()
        )


class _FakeClock:
    """Stands in for `publish._clock`; every stub below spends its full timeout."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _slow_cloudflare(monkeypatch):
    """A day when every Pages request hangs until its timeout: the live index reads
    on the third try (55s of prefix). Returns (clock, alias GETs)."""
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    clock = _FakeClock()
    monkeypatch.setattr(publish_mod, "_clock", clock)
    monkeypatch.setattr(publish_mod.time, "sleep", clock.advance)
    index_reads, alias_reads = [], []

    def get(url, **_k):
        clock.advance(publish_mod.VERIFY_TIMEOUT_SECONDS)
        if url == "https://proj.pages.dev/":
            index_reads.append(url)
            if len(index_reads) <= 2:
                raise httpx.ConnectError("timed out")
            return httpx.Response(200, content=b"")
        alias_reads.append(url)
        return httpx.Response(200, text="<title>Archive</title>")

    monkeypatch.setattr(publish_mod.httpx, "get", get)
    return clock, alias_reads


def test_a_slow_deploy_attempt_is_not_retried_past_the_publish_budget(monkeypatch):
    clock, _alias = _slow_cloudflare(monkeypatch)
    calls = []

    def deploy_manifest(_self, new_files, manifest, recover, branch="main"):
        calls.append(1)
        clock.advance(100)
        raise publish_mod.PagesDeployError("timed out")

    monkeypatch.setattr(publish_mod.PagesClient, "deploy_manifest", deploy_manifest)

    ok = publish_mod.publish_site(
        {"/new.html": b"x"}, "slug", _Store({"/old.html": "old"}), "proj", _Receipt()
    )

    assert ok is False
    assert len(calls) == 1
    assert clock.now == 155 <= publish_mod.PUBLISH_BUDGET_SECONDS


def test_a_slow_verdict_is_not_waited_on_past_the_publish_budget(monkeypatch):
    clock, alias = _slow_cloudflare(monkeypatch)

    def deploy_manifest(_self, new_files, manifest, recover, branch="main"):
        clock.advance(100)
        return QUEUED, {**manifest, **{p: "new" for p in new_files}}

    asked = []

    def get_deployment(_self, deployment_id):
        asked.append(deployment_id)
        clock.advance(20)
        return ACTIVE

    monkeypatch.setattr(publish_mod.PagesClient, "deploy_manifest", deploy_manifest)
    monkeypatch.setattr(publish_mod.PagesClient, "get_deployment", get_deployment)
    store = _Store({"/old.html": "old"})

    ok = publish_mod.publish_site({"/new.html": b"x"}, "slug", store, "proj", _Receipt())

    assert ok is False
    assert len(asked) == 1
    assert alias == []
    assert store.saved is None
    assert clock.now <= publish_mod.PUBLISH_BUDGET_SECONDS


def test_the_budget_leaves_a_fast_retry_alone(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    _skip_live_index(monkeypatch)
    monkeypatch.setattr(
        publish_mod.httpx,
        "get",
        lambda _u, **_k: httpx.Response(200, text="<title>CYRIS // 2026-08-27</title>"),
    )
    deployed = []

    def deploy_manifest(_self, new_files, manifest, recover, branch="main"):
        deployed.append(1)
        if len(deployed) == 1:
            raise publish_mod.PagesDeployError("refused")
        return LANDED, {**manifest, **{p: "new" for p in new_files}}

    monkeypatch.setattr(publish_mod.PagesClient, "deploy_manifest", deploy_manifest)

    ok = publish_mod.publish_site(
        {"/new.html": b"x"}, "2026-08-27-morning", _Store({"/old.html": "old"}), "proj", _Receipt()
    )

    assert ok is True
    assert len(deployed) == 2


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
    monkeypatch.setattr(publish_mod, "_page_is_live", lambda *_a, **_k: True)
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
    monkeypatch.setattr(publish_mod, "_page_is_live", lambda *_a, **_k: True)
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
        # Not a 404: that one means the project is absent, which is a first
        # publish, not a failure. Anything else leaves the site unreadable.
        raise publish_mod.PagesDeployError("GET ... -> 500 internal error", 500)

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
    monkeypatch.setattr(publish_mod, "_page_is_live", lambda *_a, **_k: True)
    monkeypatch.setattr(publish_mod.PagesClient, "has_deployments", lambda _self: False)
    events = []

    class _OrderedReceipt(_Receipt):
        def record(self, project):
            events.append("receipt")
            super().record(project)

    def deploy_manifest(_self, new_files, manifest, recover, branch="main"):
        events.append("deploy")
        return LANDED, {**manifest, **{p: "new" for p in new_files}}

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
    refused = [True]

    def deploy_manifest(_self, new_files, manifest, recover, branch="main"):
        if refused[0]:
            raise publish_mod.PagesDeployError("upload failed")
        return LANDED, {**manifest, **{p: "new" for p in new_files}}

    monkeypatch.setattr(publish_mod.PagesClient, "deploy_manifest", deploy_manifest)
    store = _Store({})
    receipt = _Receipt()
    monkeypatch.setattr(publish_mod, "_page_is_live", lambda *_a, **_k: True)

    first = publish_mod.publish_site({"/new.html": b"x"}, "slug", store, "proj", receipt)
    assert first is False
    assert store.saved is None
    assert receipt.present is True
    assert len(probes) == 1

    refused[0] = False
    second = publish_mod.publish_site({"/new.html": b"x"}, "slug", store, "proj", receipt)
    assert second is True
    assert len(probes) == 1


def test_a_preexisting_receipt_does_not_probe(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setattr(publish_mod, "_page_is_live", lambda *_a, **_k: True)
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
    monkeypatch.setattr(publish_mod, "_page_is_live", lambda *_a, **_k: True)


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
        return LANDED, merged

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


def _sixty_five_issues() -> list[str]:
    """Two issues a day over three months, and one day with a single issue."""
    days = [f"2026-{month:02d}-{day:02d}" for month in (6, 7, 8) for day in range(1, 12)]
    issues = [f"{day}-{period}" for day in days for period in ("morning", "evening")]
    issues.remove(f"{days[0]}-evening")
    return issues


def _missing_from_archive(html: str, issues: list[str]) -> set[str]:
    """The issues whose digest the archive's recovery reading cannot see."""
    return {f"/{issue}.html" for issue in issues} - publish_mod._parse_archive_anchors(html)


def test_the_archive_lists_every_issue_the_site_holds(tmp_path):
    """Pages recovery rebuilds from what the live index lists: no issue may drop off."""
    from cyris.adapters.output.html_digest import HtmlDigestWriter

    issues = _sixty_five_issues()
    raws = [f"{issue}-raw.html" for issue in issues[::2]]

    html = HtmlDigestWriter(tmp_path).render_index([f"{i}.html" for i in issues] + raws)

    assert len(issues) == 65
    assert _missing_from_archive(html, issues) == set()
    assert html.count(">Digest</a>") == 65
    assert "<details" not in html


def test_the_completeness_check_names_an_issue_cut_from_the_archive(tmp_path):
    from cyris.adapters.output.html_digest import HtmlDigestWriter

    issues = _sixty_five_issues()
    html = HtmlDigestWriter(tmp_path).render_index([f"{i}.html" for i in issues])
    cut = "2026-07-05-morning"
    row = re.search(
        rf'<div class="list-row archive-row[^"]*">(?:(?!</div>).)*?{cut}\.html.*?</div>', html, re.S
    )
    assert row

    assert _missing_from_archive(html.replace(row.group(0), ""), issues) == {f"/{cut}.html"}


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
    monkeypatch.setattr(publish_mod, "_page_is_live", lambda *_a, **_k: True)
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


def test_publish_deploys_when_one_live_page_is_missing(monkeypatch):
    """A deploy that landed while the live check said no leaves exactly this."""
    live = _dated(62)
    deployed = []
    _publish_env(monkeypatch, live_paths=live, deployed=deployed)
    store = _Store({p: "h" for p in live[1:]})

    ok = publish_mod.publish_site({"/new.html": b"x"}, "slug", store, "proj", _Receipt())

    assert ok is True
    assert len(deployed) == 1


def test_publish_refuses_a_database_two_pages_behind_the_live_archive(monkeypatch):
    """A staging clone or a restore is days behind; a flake can never be."""
    live = _dated(62)
    deployed = []
    _publish_env(monkeypatch, live_paths=live, deployed=deployed)
    store = _Store({p: "h" for p in live[2:]})

    ok = publish_mod.publish_site({"/new.html": b"x"}, "slug", store, "proj", _Receipt())

    assert ok is False
    assert deployed == []


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


def test_a_receipt_from_a_failed_upload_does_not_wedge_the_next_publish(monkeypatch):
    """The receipt is written before the upload, so a failed one leaves one
    behind for a site that was never built. Refusing on the unreadable index
    of that non-existent site would wait forever for what only a deploy makes."""
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setattr(publish_mod, "_page_is_live", lambda *_a, **_k: True)
    monkeypatch.setattr(publish_mod.time, "sleep", lambda _s: None)

    def boom(_u, **_k):
        raise httpx.ConnectError("no such host")

    monkeypatch.setattr(publish_mod.httpx, "get", boom)
    deployed = []
    _stub_client(monkeypatch, deployed=deployed)

    ok = publish_mod.publish_site(
        {"/new.html": b"x"}, "slug", _Store({}), "proj", _Receipt(present=True)
    )

    assert ok is True
    assert len(deployed) == 1


def test_publish_refuses_when_the_live_archive_cannot_be_read(monkeypatch, caplog):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setattr(publish_mod, "_page_is_live", lambda *_a, **_k: True)
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
    monkeypatch.setattr(publish_mod, "_page_is_live", lambda *_a, **_k: True)
    monkeypatch.setattr(publish_mod.time, "sleep", lambda _s: None)
    monkeypatch.setattr(publish_mod.httpx, "get", lambda _u, **_k: httpx.Response(500, content=b""))
    deployed = []
    _stub_client(monkeypatch, deployed=deployed)

    ok = publish_mod.publish_site(
        {"/new.html": b"x"}, "slug", _Store({"/old.html": "old"}), "proj", _Receipt()
    )

    assert ok is False
    assert deployed == []


def test_a_project_that_does_not_exist_yet_is_created_and_published_to(monkeypatch):
    """Deploy buttons create Workers, not Pages projects. The run creates its own."""
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setattr(publish_mod, "_page_is_live", lambda *_a, **_k: True)

    def probe(_self):
        raise publish_mod.PagesDeployError("GET ... -> 404 Project not found", 404)

    created: list[str] = []
    monkeypatch.setattr(publish_mod.PagesClient, "has_deployments", probe)
    monkeypatch.setattr(
        publish_mod.PagesClient, "create_project", lambda _self: created.append("proj")
    )
    deployed = []
    _stub_client(monkeypatch, deployed=deployed)
    store = _Store({})
    receipt = _Receipt()

    ok = publish_mod.publish_site({"/new.html": b"x"}, "slug", store, "proj", receipt)

    assert ok is True
    assert created == ["proj"]
    assert receipt.records == ["proj"]
    assert deployed[0][1] == {}


def test_a_creation_that_fails_does_not_deploy(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")

    def probe(_self):
        raise publish_mod.PagesDeployError("GET ... -> 404 Project not found", 404)

    def refuse(_self):
        raise publish_mod.PagesDeployError("POST ... -> 403 forbidden", 403)

    monkeypatch.setattr(publish_mod.PagesClient, "has_deployments", probe)
    monkeypatch.setattr(publish_mod.PagesClient, "create_project", refuse)
    deployed = []
    _stub_client(monkeypatch, deployed=deployed)

    ok = publish_mod.publish_site({"/new.html": b"x"}, "slug", _Store({}), "proj", _Receipt())

    assert ok is False
    assert deployed == []
