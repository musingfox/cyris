"""Publish HTML digest directory to Cloudflare Pages."""

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

DEPLOY_TIMEOUT_SECONDS = 180
DEPLOY_RECEIPT = "Deployment complete"
# Pinned to the version baked into the image (Dockerfile). Unpinned, bunx runs
# whatever was cached at build time — 4.121.0 exits 0 after the banner without
# deploying, which is how the 2026-08-13 morning digest lost its link.
WRANGLER = "wrangler@4.122.0"
# ponytail: fixed retry count, no backoff — the observed failure is instant and
# intermittent (wrangler exits 0 in ~2s having printed nothing), and an immediate
# second attempt has always succeeded. Revisit if a retry is ever seen to fail too.
DEPLOY_ATTEMPTS = 2


def publish_html_digest(html_dir: Path, pages_project: str) -> bool:
    """Deploy the HTML digest directory to Cloudflare Pages.

    Failures are logged and swallowed — publishing must never block the
    digest pipeline.

    Args:
        html_dir: Directory containing the rendered HTML digests.
        pages_project: Cloudflare Pages project name.

    Returns:
        True if the deploy succeeded.
    """
    if not pages_project:
        logger.warning("Pages publish enabled but promote.pages_project is empty")
        return False

    for attempt in range(1, DEPLOY_ATTEMPTS + 1):
        if _deploy_once(html_dir, pages_project, attempt):
            return True
    return False


def _deploy_once(html_dir: Path, pages_project: str, attempt: int) -> bool:
    """One `wrangler pages deploy` invocation, verified by its own receipt."""
    cmd = [
        "bunx",
        WRANGLER,
        "pages",
        "deploy",
        str(html_dir),
        "--project-name",
        pages_project,
        "--commit-dirty=true",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=DEPLOY_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.error("Pages deploy failed to run: %s", e)
        return False

    if result.returncode != 0:
        logger.error("Pages deploy failed (attempt %d): %s", attempt, result.stderr[-500:])
        return False

    # Exit 0 is not proof of a deploy: wrangler has been observed exiting 0 in ~2s
    # having printed nothing and deployed nothing, which is how the 2026-08-07
    # morning digest went missing while the log claimed success. Assert wrangler's
    # own receipt instead.
    output = f"{result.stdout}{result.stderr}"
    if DEPLOY_RECEIPT not in output:
        logger.error(
            "Pages deploy produced no deployment (attempt %d, rc=%d, cmd=%s): %r",
            attempt,
            result.returncode,
            " ".join(cmd),
            output[-500:],
        )
        return False

    logger.info("Published HTML digest to Pages project %s (attempt %d)", pages_project, attempt)
    return True
