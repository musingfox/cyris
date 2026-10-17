"""Pruning named issues from the published archive: refusals, preflight, rows, deploy."""

import importlib
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fakes import RecordingD1

from cyris.adapters.output import publish as publish_mod
from cyris.adapters.output.pages_deploy import DeploymentRecord, PagesClient, PagesDeployError
from cyris.adapters.output.pages_manifest import D1PagesManifest
from cyris.adapters.output.publish import _parse_archive_anchors
from cyris.adapters.store.archive_meta import D1ArchiveMeta
from cyris.adapters.store.d1 import D1Error
from cyris.adapters.store.digest_inputs import D1DigestInputStore
from cyris.adapters.store.digests import D1DigestStore

pytestmark = pytest.mark.integration

_SCRIPTS = str(Path(__file__).parent.parent / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
prune = importlib.import_module("prune_archive")

A = "2026-10-01-morning"
B = "2026-10-01-evening"
C = "2026-10-02-morning"
D = "2026-10-02-evening"
SENTINEL = "tok-SENTINEL-123"


class EventD1(RecordingD1):
    """RecordingD1 that logs deletes into a shared event list and can reject one statement."""

    def __init__(self, events):
        super().__init__()
        self.events = events
        self.fail_on: str | None = None

    def query(self, sql, params=None):
        if self.fail_on and sql.startswith(self.fail_on):
            raise D1Error("injected")
        if sql.startswith("DELETE"):
            self.events.append("delete")
        return super().query(sql, params)


class FakeSite:
    """The Pages project's live site: what its alias serves, and what a deploy hands over."""

    def __init__(self, events):
        self.events = events
        self.pages: dict[str, bytes] = {}
        self.urls: list[str] = []
        self.root_reads = 0
        self.index_by_read: dict[int, bytes] = {}
        self.deploys: list[dict[str, bytes]] = []
        self.deploy_error: Exception | None = None
        self.deploy_attempts = 0

    def set_index(self, paths):
        self.pages["/index.html"] = _index_html(paths)

    def get(self, url, **_):
        self.urls.append(url)
        request = httpx.Request("GET", url)
        path = "/" + url.split(".pages.dev/", 1)[1]
        if path == "/":
            self.root_reads += 1
            self.events.append("live")
            body = self.index_by_read.get(self.root_reads, self.pages.get("/index.html"))
        else:
            body = self.pages.get(path + ".html")
        if body is None:
            return httpx.Response(404, request=request)
        return httpx.Response(200, content=body, request=request)

    def deploy_manifest(self, new_files, kept, recover=None):
        self.deploy_attempts += 1
        if self.deploy_error is not None:
            raise self.deploy_error
        self.deploys.append(dict(new_files))
        snapshot = {p: self.pages[p] for p in kept if p in self.pages}
        snapshot.update(new_files)
        self.pages = snapshot
        updated = {**kept, **{p: f"new:{p}" for p in new_files}}
        record = DeploymentRecord.from_result(
            {"id": "dep", "latest_stage": {"name": "deploy", "status": "success"}}
        )
        return record, updated


def _index_html(paths):
    anchors = "".join(f'<a href="{p}">{p}</a>' for p in paths)
    return f"<html><body>{anchors}</body></html>".encode()


def _cfg(*, backend="d1", html=True, publish=True, pages_project="prune-proj"):
    app = SimpleNamespace(
        store=SimpleNamespace(backend=backend),
        html_output=SimpleNamespace(enabled=html, output_dir="/unused"),
        promote=SimpleNamespace(publish_enabled=publish, pages_project=pages_project),
    )
    return SimpleNamespace(app=app)


def _issue_paths(slug, *, raw=True):
    return [f"/{slug}.html"] + ([f"/{slug}-raw.html"] if raw else [])


def _seed(d1, slugs, manifest_paths):
    for slug in slugs:
        date, period = slug[:10], slug[11:]
        d1.query(
            "INSERT INTO digests (date, period, content, raw_page, saved_at) "
            "VALUES (?, ?, '{}', 0, '2026-10-01T00:00:00')",
            [date, period],
        )
        d1.query(
            "INSERT INTO digest_inputs (date, period, urls, saved_at) "
            "VALUES (?, ?, '[]', '2026-10-01T00:00:00')",
            [date, period],
        )
    D1PagesManifest(d1).save({p: f"h{p}" for p in manifest_paths})
    d1.statements.clear()


def _split(slug):
    return slug[:10], slug[11:]


def _stored(d1):
    return {f"{date}-{period}" for date, period in D1DigestStore(d1).issues()}


def _three_issues(world):
    """A, B, C stored; the manifest and the live index list A's raw page and all digests."""
    manifest = ["/index.html", f"/{A}.html", f"/{B}.html", f"/{C}.html", f"/{A}-raw.html"]
    _seed(world.d1, [A, B, C], manifest)
    world.site.set_index([p for p in manifest if p != "/index.html"])


@pytest.fixture
def world(monkeypatch):
    events: list[str] = []
    w = SimpleNamespace(
        events=events,
        d1=EventD1(events),
        site=FakeSite(events),
        config=_cfg(),
        page_is_live=[],
        serves=[],
    )
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    monkeypatch.setattr(prune, "build_d1_client", lambda cfg: w.d1)
    monkeypatch.setattr(prune, "load_effective_config", lambda config, sources: w.config)
    monkeypatch.setattr(publish_mod.httpx, "get", w.site.get)
    monkeypatch.setattr(PagesClient, "deploy_manifest", w.site.deploy_manifest)
    monkeypatch.setattr(publish_mod.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        publish_mod,
        "_page_is_live",
        lambda pages_project, slug, deadline: w.page_is_live.append(slug) or True,
    )
    return w


def _stub_publish(monkeypatch, world, result=True):
    calls = []

    def stub(*args, **kwargs):
        calls.append((args, kwargs))
        world.events.append("publish")
        return result

    monkeypatch.setattr(prune, "publish_site", stub)
    monkeypatch.setattr(prune, "page_serves", lambda *a, **k: world.serves.append(a) or True)
    return calls


# PruneRefusesBeforeAnyWrite


def test_a_slug_that_is_not_a_dated_issue_is_a_usage_error(world, monkeypatch, capsys):
    built = []
    monkeypatch.setattr(prune, "build_d1_client", lambda cfg: built.append(cfg))
    with pytest.raises(SystemExit) as exc:
        prune.main(["yesterday"])
    assert exc.value.code == 2
    assert "yesterday" in capsys.readouterr().err
    assert built == []


@pytest.mark.parametrize("slug", ["2026-10-01-noon", "2026-13-40-morning"])
def test_an_unknown_period_or_impossible_date_is_a_usage_error(world, slug):
    with pytest.raises(SystemExit) as exc:
        prune.main([slug])
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "cfg_kwargs, named",
    [
        ({"backend": "json"}, "d1"),
        ({"publish": False}, "Publishing is disabled"),
        ({"html": False}, "HTML output is disabled"),
        ({"pages_project": ""}, "pages_project"),
    ],
)
def test_a_config_that_cannot_prune_writes_nothing(world, capsys, cfg_kwargs, named):
    world.config = _cfg(**cfg_kwargs)
    _seed(world.d1, [A], [f"/{A}.html"])

    assert prune.main([A, "--yes"]) == 1

    assert named in capsys.readouterr().err
    assert world.d1.statements == []


def test_the_configuration_is_read_from_the_given_paths(world, monkeypatch, capsys):
    received = []
    world.config = _cfg(backend="json")
    monkeypatch.setattr(
        prune,
        "load_effective_config",
        lambda config, sources: received.append((config, sources)) or world.config,
    )

    assert prune.main([A, "--config", "x/cyris.toml", "--sources", "x/sources.yaml"]) == 1

    assert received == [(Path("x/cyris.toml"), Path("x/sources.yaml"))]


def test_a_malformed_sources_file_is_one_stderr_line_not_a_traceback(
    world, monkeypatch, tmp_path, capsys
):
    from cyris import bootstrap

    (tmp_path / "cyris.toml").write_text("[html_output]\nenabled = true\n")
    (tmp_path / "sources.yaml").write_text("sources: [\n  - bad: : :\n")
    monkeypatch.setattr(prune, "load_effective_config", bootstrap.load_effective_config)
    _seed(world.d1, [A], [f"/{A}.html"])

    assert (
        prune.main(
            [
                A,
                "--yes",
                "--config",
                str(tmp_path / "cyris.toml"),
                "--sources",
                str(tmp_path / "sources.yaml"),
            ]
        )
        == 1
    )

    err = capsys.readouterr().err.splitlines()
    assert len(err) == 1
    assert err[0].startswith("Configuration error")
    assert world.d1.statements == []


def test_a_stored_config_read_failure_is_one_stderr_line(world, monkeypatch, capsys):
    def unreadable(config, sources):
        raise D1Error("HTTP 500: unavailable")

    monkeypatch.setattr(prune, "load_effective_config", unreadable)
    _seed(world.d1, [A], [f"/{A}.html"])

    assert prune.main([A, "--yes"]) == 1

    err = capsys.readouterr().err.splitlines()
    assert len(err) == 1
    assert "HTTP 500" in err[0]
    assert world.d1.statements == []


def test_a_prune_that_would_empty_the_archive_is_refused(world, capsys):
    _seed(
        world.d1,
        [A, B],
        ["/index.html", f"/{A}.html", f"/{A}-raw.html", f"/{B}.html", f"/{B}-raw.html"],
    )

    assert prune.main([A, B, "--yes"]) == 1

    assert "empty" in capsys.readouterr().err
    assert world.d1.written_tables() == set()
    assert _stored(world.d1) == {A, B}
    assert world.site.urls == []


# PruneDryRunWritesNothing


def test_a_dry_run_prints_the_counts_and_writes_nothing(world, monkeypatch, capsys):
    manifest = ["/index.html", f"/{A}.html", f"/{B}.html", f"/{C}.html", f"/{A}-raw.html"]
    _seed(world.d1, [A, B, C], manifest)
    world.site.set_index([p for p in manifest if p != "/index.html"])
    publish_calls = _stub_publish(monkeypatch, world)
    before = (
        D1DigestStore(world.d1).issues(),
        D1PagesManifest(world.d1).load(),
    )

    assert prune.main([A, B]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert f"{A}: digests=1 digest_inputs=1 manifest_paths=2" in lines
    assert f"{B}: digests=1 digest_inputs=1 manifest_paths=1" in lines
    assert "total: digests=2 digest_inputs=2 manifest_paths=3" in lines
    assert "Dry run: nothing was written. Re-run with --yes to prune." in lines
    assert world.d1.written_tables() == set()
    assert (D1DigestStore(world.d1).issues(), D1PagesManifest(world.d1).load()) == before
    assert publish_calls == []
    assert world.serves == []
    assert world.page_is_live == []
    assert world.site.root_reads == 1
    assert world.site.deploys == []


def test_a_repeated_slug_is_counted_and_pruned_once(world, monkeypatch, capsys):
    _three_issues(world)
    _stub_publish(monkeypatch, world)

    assert prune.main([A, A]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert sum(line.startswith(f"{A}:") for line in lines) == 1
    assert "total: digests=1 digest_inputs=1 manifest_paths=2" in lines

    assert prune.main([A, A, "--yes"]) == 0

    out = capsys.readouterr().out
    assert out.count(f"{A}: rows deleted") == 1
    assert "Pruned 1 issue(s)." in out
    assert _stored(world.d1) == {B, C}


def test_a_slug_that_names_nothing_is_reported_as_nothing_to_prune(world, capsys):
    _three_issues(world)

    assert prune.main(["2026-09-01-morning"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert "2026-09-01-morning: digests=0 digest_inputs=0 manifest_paths=0" in lines
    assert "Nothing to prune." in lines
    assert world.d1.written_tables() == set()


def test_a_dry_run_never_prints_the_token(world, monkeypatch, capsys):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", SENTINEL)
    _three_issues(world)

    assert prune.main([A]) == 0

    captured = capsys.readouterr()
    assert SENTINEL not in captured.out
    assert SENTINEL not in captured.err


# PruneChecksLiveArchiveFirst


def test_an_unnamed_live_issue_refuses_before_any_write(world, monkeypatch, capsys):
    manifest = ["/index.html", f"/{A}.html", f"/{B}.html", f"/{C}.html", f"/{A}-raw.html"]
    _seed(world.d1, [A, B, C], manifest)
    world.site.set_index(
        [p for p in manifest if p != "/index.html"] + [f"/{D}.html", f"/{D}-raw.html"]
    )
    publish_calls = _stub_publish(monkeypatch, world)

    assert prune.main([A, "--yes"]) == 1

    err = capsys.readouterr().err
    assert f"/{D}.html" in err and f"/{D}-raw.html" in err
    assert "database_id" in err
    assert world.d1.written_tables() == set()
    assert publish_calls == []
    assert world.site.root_reads == 1
    assert world.site.deploys == []
    assert _stored(world.d1) == {A, B, C}


def test_the_refusal_also_holds_without_yes(world, capsys):
    manifest = ["/index.html", f"/{A}.html", f"/{B}.html", f"/{C}.html", f"/{A}-raw.html"]
    _seed(world.d1, [A, B, C], manifest)
    world.site.set_index(
        [p for p in manifest if p != "/index.html"] + [f"/{D}.html", f"/{D}-raw.html"]
    )

    assert prune.main([A]) == 1

    captured = capsys.readouterr()
    assert f"/{D}.html" in captured.err
    assert "Pruned" not in captured.out
    assert "total:" not in captured.out
    assert world.d1.written_tables() == set()
    assert world.site.root_reads == 1
    assert world.site.deploys == []


def test_an_unreadable_live_archive_refuses_with_and_without_yes(world, capsys):
    _three_issues(world)
    world.site.pages.pop("/index.html")

    for argv in ([A], [A, "--yes"]):
        assert prune.main(argv) == 1
        assert "the live archive could not be read" in capsys.readouterr().err

    assert world.d1.written_tables() == set()
    assert world.site.root_reads == 6
    assert _stored(world.d1) == {A, B, C}


def test_named_pages_on_the_live_archive_are_exempt(world, capsys):
    _seed(
        world.d1,
        [A, B, C],
        ["/index.html", f"/{A}.html", f"/{A}-raw.html", f"/{B}.html", f"/{C}.html"],
    )
    world.site.set_index([f"/{A}.html", f"/{A}-raw.html", f"/{B}.html", f"/{C}.html"])

    assert prune.main([A]) == 0
    assert "total: digests=1 digest_inputs=1 manifest_paths=2" in capsys.readouterr().out


def test_a_missing_named_raw_page_on_the_live_archive_is_not_a_refusal(world, capsys):
    _seed(
        world.d1,
        [A, B, C],
        ["/index.html", f"/{A}.html", f"/{A}-raw.html", f"/{B}.html", f"/{C}.html"],
    )
    world.site.set_index([f"/{A}.html", f"/{B}.html", f"/{C}.html"])

    assert prune.main([A]) == 0


# PruneDeletesStoredRows


def test_yes_deletes_only_the_named_issues_rows(world, monkeypatch, capsys):
    _three_issues(world)
    publish_calls = _stub_publish(monkeypatch, world, result=False)

    assert prune.main([A, B, "--yes"]) == 1

    assert len(publish_calls) == 1

    assert _stored(world.d1) == {C}
    assert D1DigestInputStore(world.d1).load(*_split(C)) is not None
    assert D1DigestInputStore(world.d1).load(*_split(A)) is None
    assert world.d1.written_tables() <= {"digests", "digest_inputs", "pages_manifest"}


def test_rows_are_deleted_after_the_live_read_and_before_the_deploy(world, monkeypatch):
    _three_issues(world)
    world.events.clear()
    _stub_publish(monkeypatch, world, result=True)

    assert prune.main([A, B, "--yes"]) == 0

    assert world.events.index("live") < world.events.index("delete")
    last_delete = len(world.events) - 1 - world.events[::-1].index("delete")
    assert last_delete < world.events.index("publish")


def test_the_deploy_is_not_started_when_a_delete_fails(world, monkeypatch, capsys):
    _three_issues(world)
    publish_calls = _stub_publish(monkeypatch, world)
    world.d1.fail_on = "DELETE FROM digest_inputs"

    assert prune.main([A, "--yes"]) == 1

    assert publish_calls == []
    assert "nothing was deployed" in capsys.readouterr().err


def test_a_failed_article_count_read_is_logged_and_the_prune_goes_on(
    world, monkeypatch, capsys, caplog
):
    _three_issues(world)
    _stub_publish(monkeypatch, world)
    caplog.set_level(logging.ERROR)

    def unreadable(self):
        raise D1Error("HTTP 500: unavailable")

    monkeypatch.setattr(D1ArchiveMeta, "article_counts", unreadable)

    assert prune.main([A, "--yes"]) == 0

    assert "Failed to read the archive's article counts: HTTP 500" in caplog.text


def test_a_run_after_everything_is_gone_is_nothing_to_prune(world, monkeypatch, capsys):
    world.site.set_index(_issue_paths(C))
    publish_calls = _stub_publish(monkeypatch, world)

    assert prune.main([A, B, "--yes"]) == 0

    assert "Nothing to prune." in capsys.readouterr().out
    assert world.d1.written_tables() == set()
    assert publish_calls == []


# PruneDeploysWithoutIssues


def test_yes_redeploys_without_the_named_issues(world, capsys):
    _three_issues(world)

    assert prune.main([A, B, "--yes"]) == 0

    out = capsys.readouterr().out
    assert "Deploying..." in out
    assert out.strip().endswith("Pruned 2 issue(s).")
    names = set(D1PagesManifest(world.d1).load())
    assert not any(name.startswith("/2026-10-01") for name in names)
    assert {"/index.html", f"/{C}.html"} <= names
    assert world.d1.written_tables() <= {"digests", "digest_inputs", "pages_manifest"}
    assert world.site.root_reads == 3
    assert len(world.site.deploys) == 1


def test_the_served_index_lists_only_the_remaining_issues(world):
    _three_issues(world)

    assert prune.main([A, B, "--yes"]) == 0

    served = world.site.pages["/index.html"]
    anchors = _parse_archive_anchors(served.decode())
    assert not any(a.startswith("/2026-10-01") for a in anchors)
    assert f"/{C}.html" in anchors
    assert served == world.site.deploys[-1]["/index.html"]
    assert world.site.urls.count("https://prune-proj.pages.dev/") == 3
    assert len(world.site.deploys) == 1


def test_the_page_check_names_the_newest_remaining_issue(world):
    _three_issues(world)

    assert prune.main([A, B, "--yes"]) == 0

    assert world.page_is_live == [C]
    assert len(world.site.deploys) == 1


def test_every_remaining_issue_is_in_the_manifest(world):
    _three_issues(world)

    assert prune.main([A, B, "--yes"]) == 0

    stored = {f"/{d}-{p}.html" for d, p in D1DigestStore(world.d1).issues()}
    assert stored <= set(D1PagesManifest(world.d1).load())


def test_the_site_and_the_rows_come_from_the_given_config(world, monkeypatch):
    _three_issues(world)
    world.config = _cfg(pages_project="prune-proj")
    built = []

    def build(cfg):
        built.append(cfg)
        return world.d1

    monkeypatch.setattr(prune, "build_d1_client", build)

    assert prune.main([A, B, "--yes"]) == 0

    assert built == [world.config]
    assert world.site.urls
    assert len(world.site.urls) == 3
    assert all("prune-proj.pages.dev" in url for url in world.site.urls)
    assert D1PagesManifest(world.d1).load()


def test_the_token_never_reaches_output_or_logs(world, monkeypatch, capsys, caplog):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", SENTINEL)
    caplog.set_level(logging.DEBUG)
    _three_issues(world)

    assert prune.main([A, B, "--yes"]) == 0

    captured = capsys.readouterr()
    assert SENTINEL not in captured.out + captured.err
    assert SENTINEL not in caplog.text


def test_a_deploy_error_naming_the_token_is_redacted(world, monkeypatch, capsys, caplog):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", SENTINEL)
    caplog.set_level(logging.DEBUG)
    _three_issues(world)
    world.site.deploy_error = RuntimeError(f"upstream rejected {SENTINEL}")

    assert prune.main([A, B, "--yes"]) == 1

    captured = capsys.readouterr()
    assert SENTINEL not in captured.out + captured.err
    assert "<redacted>" in captured.err
    assert SENTINEL not in caplog.text
    assert world.site.root_reads == 2
    assert world.site.deploys == []


def test_a_logged_deploy_error_naming_the_token_is_redacted(world, monkeypatch, capsys, caplog):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", SENTINEL)
    caplog.set_level(logging.DEBUG)
    _three_issues(world)
    world.site.deploy_error = PagesDeployError(f"upstream rejected {SENTINEL}")

    assert prune.main([A, B, "--yes"]) == 1

    captured = capsys.readouterr()
    assert SENTINEL not in captured.out + captured.err
    assert SENTINEL not in caplog.text
    assert "<redacted>" in caplog.text
    assert world.site.deploys == []


# PruneKeepsGuardForShortfallAfterPreflight


def _guard_world(world):
    _seed(
        world.d1,
        [A, B, C],
        ["/index.html"] + _issue_paths(A) + _issue_paths(B) + _issue_paths(C),
    )
    world.site.set_index(_issue_paths(A) + _issue_paths(B) + _issue_paths(C))


def test_a_shortfall_after_the_preflight_is_refused_by_the_deploy_guard(world, caplog):
    _guard_world(world)
    world.site.index_by_read = {
        2: _index_html(_issue_paths(A) + _issue_paths(B) + _issue_paths(C) + _issue_paths(D))
    }
    manifest_before = D1PagesManifest(world.d1).load()
    caplog.set_level(logging.ERROR)

    assert prune.main([A, "--yes"]) == 1

    assert world.site.deploys == []
    assert world.site.root_reads == 2
    assert D1PagesManifest(world.d1).load() == manifest_before
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any(f"/{D}-raw.html" in m or f"/{D}.html" in m for m in messages)
    assert any("database_id" in m for m in messages)
    assert A not in _stored(world.d1)


def test_the_same_wiring_passes_when_nothing_unnamed_is_missing(world):
    _guard_world(world)
    world.site.index_by_read = {2: _index_html(_issue_paths(A) + _issue_paths(B) + _issue_paths(C))}

    assert prune.main([A, "--yes"]) == 0

    assert world.site.root_reads == 3
    assert len(world.site.deploys) == 1


# PruneRerunCompletes


def test_a_rerun_refuses_while_the_site_still_lists_the_pruned_issue(world, capsys):
    _seed(world.d1, [C], [f"/{C}.html"])
    world.site.set_index([f"/{A}.html", f"/{C}.html"])

    assert prune.main([A]) == 1

    captured = capsys.readouterr()
    assert f"/{A}.html" in captured.err
    assert "wait, then re-run" in captured.err
    assert "Nothing to prune" not in captured.out

    world.site.set_index([f"/{C}.html"])

    assert prune.main([A]) == 0
    assert "Nothing to prune." in capsys.readouterr().out


def test_a_rerun_with_nothing_stored_refuses_an_unreadable_live_archive(world, capsys):
    _seed(world.d1, [C], [f"/{C}.html"])

    assert prune.main([A]) == 1

    assert "the live archive could not be read" in capsys.readouterr().err


def test_a_page_check_that_fails_names_the_wait_before_the_rerun(world, monkeypatch, capsys):
    _three_issues(world)
    _stub_publish(monkeypatch, world)
    monkeypatch.setattr(prune, "page_serves", lambda *a, **k: False)

    assert prune.main([A, "--yes"]) == 1

    captured = capsys.readouterr()
    assert "wait for the site to update, then re-run" in captured.err
    assert "Pruned" not in captured.out


def test_a_failed_deploy_is_completed_by_the_same_command(world, capsys):
    _three_issues(world)
    world.site.deploy_error = PagesDeployError("down")

    assert prune.main([A, "--yes"]) == 1

    assert A not in _stored(world.d1)
    assert f"/{A}.html" in D1PagesManifest(world.d1).load()
    assert world.site.deploy_attempts == 3
    assert world.site.root_reads == 2

    world.site.deploy_error = None
    assert prune.main([A, "--yes"]) == 0

    assert world.site.root_reads == 5
    assert len(world.site.deploys) == 1
    names = D1PagesManifest(world.d1).load()
    assert f"/{A}.html" not in names
    assert f"/{A}-raw.html" not in names
    anchors = _parse_archive_anchors(world.site.pages["/index.html"].decode())
    assert f"/{A}.html" not in anchors
    assert f"/{A}-raw.html" not in anchors

    assert prune.main([A, "--yes"]) == 0
    assert "Nothing to prune." in capsys.readouterr().out


def test_a_save_failure_after_the_deploy_is_completed_by_rerun(world, monkeypatch, capsys):
    _three_issues(world)
    original_save = D1PagesManifest.save

    def broken_save(self, manifest):
        raise RuntimeError("save broke")

    monkeypatch.setattr(D1PagesManifest, "save", broken_save)

    assert prune.main([A, "--yes"]) == 1

    assert f"/{A}.html" in D1PagesManifest(world.d1).load()
    assert f"/{A}.html" not in _parse_archive_anchors(world.site.pages["/index.html"].decode())
    assert len(world.site.deploys) == 1

    monkeypatch.setattr(D1PagesManifest, "save", original_save)
    assert prune.main([A, "--yes"]) == 0

    assert len(world.site.deploys) == 2
    assert world.site.root_reads == 5

    assert f"/{A}.html" not in D1PagesManifest(world.d1).load()
    assert f"/{A}-raw.html" not in D1PagesManifest(world.d1).load()
