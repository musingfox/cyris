"""Tests for the Cloudflare Pages publish step."""

import subprocess
from pathlib import Path

import httpx
import pytest

from cyris.adapters.output import publish as publish_mod
from cyris.adapters.output.publish import publish_html_digest

SLUG = "2026-08-20-morning"
LIVE_PAGE = "<html><head><title>CYRIS // 2026-08-20 · morning</title></head></html>"
# A missing page is served as the Archive index — HTTP 200, and its body even
# lists other digests' dates. Only the <title> tells them apart.
ARCHIVE_PAGE = "<html><head><title>CYRIS // Archive</title></head><body>2026-08-20</body></html>"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(publish_mod.time, "sleep", lambda _s: None)


def _fake_run(monkeypatch, *, returncode=0, stdout="", stderr=""):
    def run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(publish_mod.subprocess, "run", run)


def _fake_get(monkeypatch, *pages):
    """Serve each page body in turn; the last one repeats."""
    calls = []

    def get(*_args, **_kwargs):
        calls.append(1)
        body = pages[min(len(calls), len(pages)) - 1]
        return httpx.Response(200, text=body)

    monkeypatch.setattr(publish_mod.httpx, "get", get)
    return calls


def test_live_page_confirms_the_deploy(monkeypatch):
    _fake_run(monkeypatch)
    _fake_get(monkeypatch, LIVE_PAGE)

    assert publish_html_digest(Path("html"), "cyris-digest", SLUG) is True


def test_exit_zero_without_a_live_page_is_a_failure(monkeypatch):
    """wrangler exits 0 having deployed nothing, and the 404 fallback answers 200 —
    that silently dropped the Discord link on 2026-08-18 evening and 2026-08-20 morning."""
    _fake_run(monkeypatch)
    _fake_get(monkeypatch, ARCHIVE_PAGE)

    assert publish_html_digest(Path("html"), "cyris-digest", SLUG) is False


def test_a_no_op_deploy_is_retried(monkeypatch):
    runs = []

    def run(*_args, **_kwargs):
        runs.append(1)
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    monkeypatch.setattr(publish_mod.subprocess, "run", run)
    # Every poll of the first deploy sees the fallback; the retry lands.
    _fake_get(monkeypatch, *([ARCHIVE_PAGE] * publish_mod.VERIFY_POLLS), LIVE_PAGE)

    assert publish_html_digest(Path("html"), "cyris-digest", SLUG) is True
    assert len(runs) == 2


def test_verification_tolerates_propagation_delay(monkeypatch):
    _fake_run(monkeypatch)
    calls = _fake_get(monkeypatch, ARCHIVE_PAGE, LIVE_PAGE)

    assert publish_html_digest(Path("html"), "cyris-digest", SLUG) is True
    assert len(calls) == 2


def test_retries_are_bounded(monkeypatch):
    runs = []

    def run(*_args, **_kwargs):
        runs.append(1)
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    monkeypatch.setattr(publish_mod.subprocess, "run", run)
    _fake_get(monkeypatch, ARCHIVE_PAGE)

    assert publish_html_digest(Path("html"), "cyris-digest", SLUG) is False
    assert len(runs) == publish_mod.DEPLOY_ATTEMPTS


def test_nonzero_exit_skips_verification(monkeypatch):
    _fake_run(monkeypatch, returncode=1, stderr="boom")

    def explode(*_args, **_kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("should not verify a deploy that never ran")

    monkeypatch.setattr(publish_mod.httpx, "get", explode)

    assert publish_html_digest(Path("html"), "cyris-digest", SLUG) is False


def test_unreachable_page_is_a_failure(monkeypatch):
    _fake_run(monkeypatch)

    def get(*_args, **_kwargs):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(publish_mod.httpx, "get", get)

    assert publish_html_digest(Path("html"), "cyris-digest", SLUG) is False


def test_missing_project_name_short_circuits(monkeypatch):
    def explode(*_args, **_kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("should not shell out without a project name")

    monkeypatch.setattr(publish_mod.subprocess, "run", explode)

    assert publish_html_digest(Path("html"), "", SLUG) is False


def test_baked_wrangler_is_preferred_over_bunx(monkeypatch):
    """The image runs wrangler on node; bunx silently no-op'd mid-deploy."""
    seen = []

    def run(cmd, *_args, **_kwargs):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(publish_mod.subprocess, "run", run)
    _fake_get(monkeypatch, LIVE_PAGE)

    monkeypatch.setattr(publish_mod.shutil, "which", lambda _n: "/usr/local/bin/wrangler")
    publish_html_digest(Path("html"), "cyris-digest", SLUG)
    assert seen[-1][:1] == ["wrangler"]

    monkeypatch.setattr(publish_mod.shutil, "which", lambda _n: None)
    publish_html_digest(Path("html"), "cyris-digest", SLUG)
    assert seen[-1][:2] == ["bunx", publish_mod.WRANGLER]
