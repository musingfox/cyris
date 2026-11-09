"""Tests for the Cloudflare Pages publish step."""

import re
from pathlib import Path

import httpx
import pytest

from cyris.adapters.output import pages_deploy
from cyris.adapters.output import publish as publish_mod
from cyris.adapters.output.pages_deploy import DeploymentRecord
from cyris.adapters.output.publish import publish_site
from cyris.service_layer import vote_order

pytestmark = pytest.mark.unit

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


class _Manifest:
    """An empty manifest, so `publish_site` reads no live index before it deploys."""

    def __init__(self) -> None:
        self.saved: dict[str, str] | None = None

    def load(self) -> dict[str, str]:
        return {}

    def save(self, manifest: dict[str, str]) -> None:
        self.saved = manifest


class _Receipts:
    def exists(self, _project: str) -> bool:
        return False

    def record(self, _project: str) -> None:
        pass


def _publish(project: str = "cyris-digest") -> bool:
    return publish_site({"/x.html": b"x"}, SLUG, _Manifest(), project, _Receipts())


def _fake_deploy(monkeypatch, *, fails=False, fail_first=0, records=(LANDED,)):
    """Stub the direct-upload client. `records` are the deployments it reports, in
    turn; the last one repeats."""
    runs = []

    def deploy_manifest(_self, _new_files, _manifest, recover, branch="main"):
        runs.append(branch)
        if fails or len(runs) <= fail_first:
            raise publish_mod.PagesDeployError("boom")
        return records[min(len(runs) - fail_first, len(records)) - 1], {"/x.html": "h"}

    monkeypatch.setattr(publish_mod.PagesClient, "deploy_manifest", deploy_manifest)
    monkeypatch.setattr(publish_mod.PagesClient, "has_deployments", lambda _self: False)
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
    blank = DeploymentRecord("dep-2", None, None, None)
    _fake_deploy(monkeypatch, records=(blank,))
    monkeypatch.setattr(publish_mod.PagesClient, "get_deployment", lambda _self, _id: blank)
    _fake_get(monkeypatch, LIVE_PAGE)

    def real_request(*_args, **_kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("reached the real Cloudflare API")

    monkeypatch.setattr(pages_deploy.httpx, "Client", real_request)

    with caplog.at_level("INFO"):
        _publish()

    infos = [r.message for r in caplog.records if r.levelname == "INFO"]
    assert any("dep-2" in m and "None" in m for m in infos)


def test_live_page_confirms_the_deploy(monkeypatch):
    _fake_deploy(monkeypatch)
    _fake_get(monkeypatch, LIVE_PAGE)

    assert _publish() is True


def test_a_landed_deployment_without_a_live_page_is_not_published(monkeypatch):
    """Cloudflare saying `success` is not the page being served, and the 404
    fallback answers 200 — that silently dropped the Discord link on 2026-08-18
    evening and 2026-08-20 morning. The alias serving the page's own title is what
    makes the digest count as published."""
    _fake_deploy(monkeypatch)
    _fake_get(monkeypatch, ARCHIVE_PAGE)

    assert _publish() is False


def test_a_deployed_page_is_polled_not_redeployed(monkeypatch):
    """On 2026-09-24 three deployments each reached `success` within 2s, and each was
    followed by a fresh one because its page had not propagated yet. Redeploying the
    same files cannot make an edge serve them sooner."""
    runs = _fake_deploy(monkeypatch)
    _fake_get(monkeypatch, *([ARCHIVE_PAGE] * (publish_mod.VERIFY_POLLS - 1)), LIVE_PAGE)
    slept = []
    monkeypatch.setattr(publish_mod.time, "sleep", slept.append)

    assert _publish() is True
    assert len(runs) == 1
    assert sum(slept) == 120, "the verify window is two minutes of waiting"


def _sleep_after_seconds(source: str) -> int:
    match = re.search(r"RUN_INACTIVITY_TIMEOUT_MS\s*=\s*([\d_]+);", source)
    assert match, (
        "RUN_INACTIVITY_TIMEOUT_MS is not a literal this test can read; pin the budget against it"
    )
    return int(match.group(1).replace("_", "")) // 1000


def test_the_sleep_after_parser_fails_rather_than_skipping():
    with pytest.raises(AssertionError, match="RUN_INACTIVITY_TIMEOUT_MS"):
        _sleep_after_seconds("const RUN_INACTIVITY_TIMEOUT_MS = TIMEOUT;")


def test_the_publish_time_budget_fits_inside_the_containers_sleep_after():
    """On 2026-09-24 the verify window was the only thing named; the deploy
    timeouts around it could add up to minutes past the container's clock."""
    p, d = publish_mod, pages_deploy
    # The verify window is two minutes of sleeps; the stage wait is half a minute.
    assert (p.VERIFY_POLLS - 1) * p.VERIFY_INTERVAL_SECONDS == 120
    assert p.STAGE_POLLS * p.STAGE_INTERVAL_SECONDS == 30
    # The worst prefix (first-publish probe, or the live-index read), one attempt
    # and one verdict read always fit, so the budget never skips the first try.
    prefix = max(
        2 * d.TIMEOUT_SECONDS,
        p.LIVE_INDEX_POLLS * p.VERIFY_TIMEOUT_SECONDS
        + (p.LIVE_INDEX_POLLS - 1) * p.VERIFY_INTERVAL_SECONDS,
    )
    first_try = d.DEPLOY_WORST_CASE_SECONDS + p.STAGE_INTERVAL_SECONDS + d.TIMEOUT_SECONDS
    assert prefix + first_try <= p.PUBLISH_BUDGET_SECONDS
    index_js = Path(__file__).parents[1] / "workers" / "app" / "src" / "index.js"
    sleep_after = _sleep_after_seconds(index_js.read_text())
    assert sleep_after == 900
    assert (
        sleep_after
        >= vote_order.CLEF_PASS_SECONDS + p.PUBLISH_BUDGET_SECONDS + p.RUN_RESERVE_SECONDS
    )


def test_verification_tolerates_propagation_delay(monkeypatch):
    _fake_deploy(monkeypatch)
    calls = _fake_get(monkeypatch, ARCHIVE_PAGE, LIVE_PAGE)

    assert _publish() is True
    assert len(calls) == 2


def test_a_page_that_never_goes_live_is_not_redeployed(monkeypatch):
    """The alias decides the return and nothing else: a landed deployment is not
    redeployed because its page is slow to show."""
    runs = _fake_deploy(monkeypatch)
    calls = _fake_get(monkeypatch, ARCHIVE_PAGE)

    assert _publish() is False
    assert len(runs) == 1
    assert len(calls) == publish_mod.VERIFY_POLLS == 25


def test_a_refused_deployment_is_retried(monkeypatch):
    runs = _fake_deploy(monkeypatch, fail_first=1)
    _fake_get(monkeypatch, LIVE_PAGE)

    assert _publish() is True
    assert len(runs) == 2


FAILED = DeploymentRecord("dep-1", None, "deploy", "failure")


def test_a_deployment_cloudflare_reports_failed_is_deployed_again(monkeypatch):
    runs = _fake_deploy(monkeypatch, records=(FAILED, LANDED))
    _fake_get(monkeypatch, LIVE_PAGE)

    assert _publish() is True
    assert len(runs) == 2


def test_failed_deployment_retries_are_bounded(monkeypatch):
    runs = _fake_deploy(monkeypatch, records=(FAILED,))
    _fake_get(monkeypatch, LIVE_PAGE)

    assert _publish() is False
    assert len(runs) == publish_mod.DEPLOY_ATTEMPTS


def test_a_deployment_with_no_verdict_and_no_live_page_is_not_redeployed(monkeypatch):
    queued = DeploymentRecord("dep-1", None, "queued", "active")
    runs = _fake_deploy(monkeypatch, records=(queued,))
    monkeypatch.setattr(publish_mod.PagesClient, "get_deployment", lambda _self, _id: queued)
    _fake_get(monkeypatch, ARCHIVE_PAGE)

    assert _publish() is False
    assert len(runs) == 1


def test_refused_deployment_retries_are_bounded(monkeypatch):
    runs = _fake_deploy(monkeypatch, fails=True)

    assert _publish() is False
    assert len(runs) == publish_mod.DEPLOY_ATTEMPTS


def test_a_refused_deployment_skips_verification(monkeypatch):
    _fake_deploy(monkeypatch, fails=True)

    def explode(*_args, **_kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("should not verify a deploy that never ran")

    monkeypatch.setattr(publish_mod.httpx, "get", explode)

    assert _publish() is False


def test_a_deployment_cloudflare_reports_failed_skips_verification(monkeypatch):
    _fake_deploy(monkeypatch, records=(FAILED,))

    def explode(*_args, **_kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("should not verify a deployment Cloudflare says failed")

    monkeypatch.setattr(publish_mod.httpx, "get", explode)

    assert _publish() is False


def test_slow_refusals_are_not_retried_past_the_publish_budget(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(publish_mod, "_clock", lambda: now[0])
    runs = []

    def deploy_manifest(_self, _new_files, _manifest, recover, branch="main"):
        runs.append(branch)
        now[0] += 100
        raise publish_mod.PagesDeployError("timed out")

    monkeypatch.setattr(publish_mod.PagesClient, "deploy_manifest", deploy_manifest)
    monkeypatch.setattr(publish_mod.PagesClient, "has_deployments", lambda _self: False)

    assert _publish() is False
    assert len(runs) == 1
    assert now[0] <= publish_mod.PUBLISH_BUDGET_SECONDS


def test_unreachable_page_is_a_failure(monkeypatch):
    _fake_deploy(monkeypatch)

    def get(*_args, **_kwargs):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(publish_mod.httpx, "get", get)

    assert _publish() is False


def test_missing_project_name_short_circuits(monkeypatch):
    def explode(*_args, **_kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("should not call the API without a project name")

    monkeypatch.setattr(publish_mod.PagesClient, "deploy_manifest", explode)

    assert _publish("") is False


def test_missing_credentials_fail_rather_than_calling_the_api(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)

    def explode(*_args, **_kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("should not call the API without a token")

    monkeypatch.setattr(publish_mod.PagesClient, "deploy_manifest", explode)

    assert _publish() is False
