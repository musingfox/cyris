"""Tests for the Cloudflare Pages publish step."""

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


@pytest.fixture(autouse=True)
def _credentials(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")


def _fake_deploy(monkeypatch, *, fails=False):
    """Stub the direct-upload client. Whether the deploy *worked* is _page_is_live's
    question, and that is what these tests are about."""
    runs = []

    def deploy(_self, _directory, branch="main"):
        runs.append(branch)
        if fails:
            raise publish_mod.PagesDeployError("boom")
        return "dep-1"

    monkeypatch.setattr(publish_mod.PagesClient, "deploy", deploy)
    return runs


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
    _fake_deploy(monkeypatch)
    _fake_get(monkeypatch, LIVE_PAGE)

    assert publish_html_digest(Path("html"), "cyris-digest", SLUG) is True


def test_a_created_deployment_without_a_live_page_is_a_failure(monkeypatch):
    """A deployment id is not a live page, and the 404 fallback answers 200 — that
    silently dropped the Discord link on 2026-08-18 evening and 2026-08-20 morning.
    The transport changed; the reason for verifying it did not."""
    _fake_deploy(monkeypatch)
    _fake_get(monkeypatch, ARCHIVE_PAGE)

    assert publish_html_digest(Path("html"), "cyris-digest", SLUG) is False


def test_a_no_op_deploy_is_retried(monkeypatch):
    runs = _fake_deploy(monkeypatch)
    # Every poll of the first deploy sees the fallback; the retry lands.
    _fake_get(monkeypatch, *([ARCHIVE_PAGE] * publish_mod.VERIFY_POLLS), LIVE_PAGE)

    assert publish_html_digest(Path("html"), "cyris-digest", SLUG) is True
    assert len(runs) == 2


def test_verification_tolerates_propagation_delay(monkeypatch):
    _fake_deploy(monkeypatch)
    calls = _fake_get(monkeypatch, ARCHIVE_PAGE, LIVE_PAGE)

    assert publish_html_digest(Path("html"), "cyris-digest", SLUG) is True
    assert len(calls) == 2


def test_retries_are_bounded(monkeypatch):
    runs = _fake_deploy(monkeypatch)
    _fake_get(monkeypatch, ARCHIVE_PAGE)

    assert publish_html_digest(Path("html"), "cyris-digest", SLUG) is False
    assert len(runs) == publish_mod.DEPLOY_ATTEMPTS


def test_a_refused_deployment_skips_verification(monkeypatch):
    _fake_deploy(monkeypatch, fails=True)

    def explode(*_args, **_kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("should not verify a deploy that never ran")

    monkeypatch.setattr(publish_mod.httpx, "get", explode)

    assert publish_html_digest(Path("html"), "cyris-digest", SLUG) is False


def test_unreachable_page_is_a_failure(monkeypatch):
    _fake_deploy(monkeypatch)

    def get(*_args, **_kwargs):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(publish_mod.httpx, "get", get)

    assert publish_html_digest(Path("html"), "cyris-digest", SLUG) is False


def test_missing_project_name_short_circuits(monkeypatch):
    def explode(*_args, **_kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("should not call the API without a project name")

    monkeypatch.setattr(publish_mod.PagesClient, "deploy", explode)

    assert publish_html_digest(Path("html"), "", SLUG) is False


def test_missing_credentials_fail_rather_than_calling_the_api(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)

    def explode(*_args, **_kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("should not call the API without a token")

    monkeypatch.setattr(publish_mod.PagesClient, "deploy", explode)

    assert publish_html_digest(Path("html"), "cyris-digest", SLUG) is False
