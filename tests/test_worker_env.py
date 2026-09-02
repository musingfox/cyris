"""The app Worker forwards grade-B identity into the container process."""

from pathlib import Path

from cyris.config import B_GRADE_ENV_VARS

WORKER_JS = Path(__file__).resolve().parents[1] / "workers/app/src/index.js"

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
