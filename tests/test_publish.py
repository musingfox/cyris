"""Tests for the Cloudflare Pages publish step."""

import subprocess
from pathlib import Path

from cyris.adapters.output import publish as publish_mod
from cyris.adapters.output.publish import publish_html_digest

RECEIPT = "🌎 Deploying...\n✨ Deployment complete! Take a peek over at https://abc.pages.dev\n"


def _fake_run(monkeypatch, *, returncode=0, stdout="", stderr=""):
    def run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(publish_mod.subprocess, "run", run)


def test_deploy_with_receipt_succeeds(monkeypatch):
    _fake_run(monkeypatch, stdout=f"Uploading... (12/12)\n{RECEIPT}")

    assert publish_html_digest(Path("html"), "cyris-digest") is True


def test_exit_zero_without_a_deployment_is_a_failure(monkeypatch):
    """wrangler has exited 0 having deployed nothing — that silently dropped the
    2026-08-07 morning digest while the log claimed success."""
    _fake_run(monkeypatch, returncode=0, stdout="")

    assert publish_html_digest(Path("html"), "cyris-digest") is False


def test_a_no_op_deploy_is_retried_once(monkeypatch):
    """The failure is intermittent and instant; the immediate retry has always won."""
    outputs = ["", RECEIPT]
    calls = []

    def run(*_args, **_kwargs):
        calls.append(1)
        return subprocess.CompletedProcess([], 0, stdout=outputs[len(calls) - 1], stderr="")

    monkeypatch.setattr(publish_mod.subprocess, "run", run)

    assert publish_html_digest(Path("html"), "cyris-digest") is True
    assert len(calls) == 2


def test_retries_are_bounded(monkeypatch):
    calls = []

    def run(*_args, **_kwargs):
        calls.append(1)
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    monkeypatch.setattr(publish_mod.subprocess, "run", run)

    assert publish_html_digest(Path("html"), "cyris-digest") is False
    assert len(calls) == publish_mod.DEPLOY_ATTEMPTS


def test_nonzero_exit_is_a_failure(monkeypatch):
    _fake_run(monkeypatch, returncode=1, stderr="boom")

    assert publish_html_digest(Path("html"), "cyris-digest") is False


def test_receipt_on_stderr_still_counts(monkeypatch):
    """wrangler's progress output has moved between streams across versions."""
    _fake_run(monkeypatch, stderr=RECEIPT)

    assert publish_html_digest(Path("html"), "cyris-digest") is True


def test_missing_project_name_short_circuits(monkeypatch):
    def explode(*_args, **_kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("should not shell out without a project name")

    monkeypatch.setattr(publish_mod.subprocess, "run", explode)

    assert publish_html_digest(Path("html"), "") is False
