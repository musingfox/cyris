"""Tests for the Cloudflare Pages publish step."""

from pathlib import Path

import httpx
import pytest

from cyris.adapters.output import publish as publish_mod
from cyris.adapters.output.pages_deploy import DeploymentRecord
from cyris.adapters.output.publish import publish_html_digest

SLUG = "2026-08-20-morning"
LANDED = DeploymentRecord("dep-1", "https://ab12.cyris-digest.pages.dev", "deploy", "success")
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


def _fake_deploy(monkeypatch, *, fails=False, fail_first=0, records=(LANDED,)):
    """Stub the direct-upload client. `records` are the deployments it reports, in
    turn; the last one repeats."""
    runs = []

    def deploy(_self, _directory, branch="main"):
        runs.append(branch)
        if fails or len(runs) <= fail_first:
            raise publish_mod.PagesDeployError("boom")
        return records[min(len(runs) - fail_first, len(records)) - 1]

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


def test_the_create_stage_is_logged_even_when_cloudflare_reports_none(monkeypatch, caplog):
    _fake_deploy(monkeypatch, records=(DeploymentRecord("dep-2", None, None, None),))
    _fake_get(monkeypatch, LIVE_PAGE)

    with caplog.at_level("INFO"):
        publish_html_digest(Path("html"), "cyris-digest", SLUG)

    infos = [r.message for r in caplog.records if r.levelname == "INFO"]
    assert any("dep-2" in m and "None" in m for m in infos)


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


def test_a_deployed_page_is_polled_not_redeployed(monkeypatch):
    """On 2026-09-24 three deployments each reached `success` within 2s, and each was
    followed by a fresh one because its page had not propagated yet. Redeploying the
    same files cannot make an edge serve them sooner."""
    runs = _fake_deploy(monkeypatch)
    _fake_get(monkeypatch, *([ARCHIVE_PAGE] * (publish_mod.VERIFY_POLLS - 1)), LIVE_PAGE)

    assert publish_html_digest(Path("html"), "cyris-digest", SLUG) is True
    assert len(runs) == 1


def test_the_verify_window_outlasts_the_2026_09_24_delay():
    """That morning the page was still the fallback 35s after the first deployment."""
    assert (publish_mod.VERIFY_POLLS - 1) * publish_mod.VERIFY_INTERVAL_SECONDS > 35


def test_verification_tolerates_propagation_delay(monkeypatch):
    _fake_deploy(monkeypatch)
    calls = _fake_get(monkeypatch, ARCHIVE_PAGE, LIVE_PAGE)

    assert publish_html_digest(Path("html"), "cyris-digest", SLUG) is True
    assert len(calls) == 2


def test_a_page_that_never_goes_live_is_not_redeployed(monkeypatch):
    runs = _fake_deploy(monkeypatch)
    _fake_get(monkeypatch, ARCHIVE_PAGE)

    assert publish_html_digest(Path("html"), "cyris-digest", SLUG) is False
    assert len(runs) == 1


def test_a_refused_deployment_is_retried(monkeypatch):
    runs = _fake_deploy(monkeypatch, fail_first=1)
    _fake_get(monkeypatch, LIVE_PAGE)

    assert publish_html_digest(Path("html"), "cyris-digest", SLUG) is True
    assert len(runs) == 2


FAILED = DeploymentRecord("dep-1", None, "deploy", "failure")


def test_a_deployment_cloudflare_reports_failed_is_deployed_again(monkeypatch):
    runs = _fake_deploy(monkeypatch, records=(FAILED, LANDED))
    _fake_get(monkeypatch, LIVE_PAGE)

    assert publish_html_digest(Path("html"), "cyris-digest", SLUG) is True
    assert len(runs) == 2


def test_failed_deployment_retries_are_bounded(monkeypatch):
    runs = _fake_deploy(monkeypatch, records=(FAILED,))
    _fake_get(monkeypatch, LIVE_PAGE)

    assert publish_html_digest(Path("html"), "cyris-digest", SLUG) is False
    assert len(runs) == publish_mod.DEPLOY_ATTEMPTS


def test_a_deployment_with_no_verdict_and_no_live_page_is_not_redeployed(monkeypatch):
    queued = DeploymentRecord("dep-1", None, "queued", "active")
    runs = _fake_deploy(monkeypatch, records=(queued,))
    monkeypatch.setattr(publish_mod.PagesClient, "get_deployment", lambda _self, _id: queued)
    _fake_get(monkeypatch, ARCHIVE_PAGE)

    assert publish_html_digest(Path("html"), "cyris-digest", SLUG) is False
    assert len(runs) == 1


def test_refused_deployment_retries_are_bounded(monkeypatch):
    runs = _fake_deploy(monkeypatch, fails=True)

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
