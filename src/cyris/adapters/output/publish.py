"""Publish HTML digest directory to Cloudflare Pages."""

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

DEPLOY_TIMEOUT_SECONDS = 180


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

    cmd = [
        "bunx",
        "wrangler",
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
        logger.error("Pages deploy failed: %s", result.stderr[-500:])
        return False

    logger.info("Published HTML digest to Pages project %s", pages_project)
    return True
