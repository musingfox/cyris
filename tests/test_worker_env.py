"""The app Worker forwards grade-B identity into the container process."""

from pathlib import Path

import pytest

from cyris.config import B_GRADE_ENV_VARS

pytestmark = [pytest.mark.unit, pytest.mark.guard]

ROOT = Path(__file__).resolve().parents[1]
WORKER_JS = ROOT / "workers/app/src/index.js"
ROUTER_JS = ROOT / "workers/app/src/router.js"

_SECRETS = (
    "CLOUDFLARE_ACCOUNT_ID",
    "CLOUDFLARE_API_TOKEN",
    "CLOUDFLARE_EMBEDDING_API_TOKEN",
    "CYRIS_WORKER_TOKEN",
    "CYRIS_PROMOTE_TOKEN",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "CLOUDFLARE_AI_TOKEN",
)


def test_no_deploy_input_carries_the_retired_webhook_variable():
    """The webhook is a runtime setting in D1; the environment no longer supplies it."""
    deploy_inputs = (
        ".env.example",
        "package.json",
        "workers/app/src/index.js",
    )
    for rel in deploy_inputs:
        assert "CYRIS_DISCORD_WEBHOOK_URL" not in (ROOT / rel).read_text(encoding="utf-8"), rel


def test_worker_names_every_b_grade_env_var():
    text = WORKER_JS.read_text()
    for name in B_GRADE_ENV_VARS.values():
        assert name in text, f"{name} must appear in {WORKER_JS}"


def test_worker_still_names_every_secret():
    text = WORKER_JS.read_text()
    for name in _SECRETS:
        assert name in text, f"{name} must still appear in {WORKER_JS}"


def test_worker_names_access_host_and_digest_origin():
    text = WORKER_JS.read_text() + ROUTER_JS.read_text()
    assert "CYRIS_UI_ACCESS_HOST" in text
    assert "DIGEST_ORIGIN" in text
