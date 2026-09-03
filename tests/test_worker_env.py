"""The app Worker forwards grade-B identity into the container process."""

from pathlib import Path

from cyris.config import B_GRADE_ENV_VARS

ROOT = Path(__file__).resolve().parents[1]
WORKER_JS = ROOT / "workers/app/src/index.js"
ROUTER_JS = ROOT / "workers/app/src/router.js"

_SECRETS = (
    "CLOUDFLARE_ACCOUNT_ID",
    "CLOUDFLARE_API_TOKEN",
    "CLOUDFLARE_EMBEDDING_API_TOKEN",
    "CYRIS_WORKER_TOKEN",
    "CYRIS_PROMOTE_TOKEN",
    "CYRIS_DISCORD_WEBHOOK_URL",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
)


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
