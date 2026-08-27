"""Publish the HTML digest directory to Cloudflare Pages.

Deploying goes through `pages_deploy.PagesClient` (the REST direct-upload
protocol), not `wrangler pages deploy`. Verifying it stayed exactly as it was —
`_page_is_live` is the receipt that caught the 2026-08-18/08-20 silent failures,
and swapping the transport underneath it is no reason to trust the new one more.
"""

import logging
import os
import re
import time
from pathlib import Path

import httpx

from cyris.adapters.output.pages_deploy import PagesClient, PagesDeployError

logger = logging.getLogger(__name__)

DEPLOY_ATTEMPTS = 3
# Cloudflare Pages serves the extensionless clean URL almost immediately, but
# not always on the first read.
VERIFY_POLLS = 3
VERIFY_INTERVAL_SECONDS = 5
VERIFY_TIMEOUT_SECONDS = 15

_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def publish_html_digest(html_dir: Path, pages_project: str, slug: str) -> bool:
    """Deploy the HTML digest directory to Cloudflare Pages.

    Failures are logged and swallowed — publishing must never block the
    digest pipeline.

    Args:
        html_dir: Directory containing the rendered HTML digests.
        pages_project: Cloudflare Pages project name.
        slug: Digest slug (``{date}-{period}``) whose page must be live for the
            deploy to count as successful.

    Returns:
        True if the digest page is live at its published URL.
    """
    if not pages_project:
        logger.warning("Pages publish enabled but promote.pages_project is empty")
        return False

    for attempt in range(1, DEPLOY_ATTEMPTS + 1):
        if _deploy_once(html_dir, pages_project, attempt) and _page_is_live(pages_project, slug):
            logger.info(
                "Published HTML digest to Pages project %s (attempt %d)", pages_project, attempt
            )
            return True
    return False


def publish_site(
    new_files: dict[str, bytes],
    manifest_store,
    pages_project: str,
    slug: str,
) -> bool:
    """Publish without a local archive: this run's files plus the D1 manifest.

    The archive is not a directory any more. Cloudflare's asset store already
    holds every byte ever deployed, so all that has to survive between runs is
    path → hash; the bytes of an evicted asset come back from the live site,
    which serves exactly what it was given.
    """
    if not pages_project:
        logger.warning("Pages publish enabled but promote.pages_project is empty")
        return False
    client = _client(pages_project)
    if client is None:
        return False

    manifest = manifest_store.load()
    for attempt in range(1, DEPLOY_ATTEMPTS + 1):
        try:
            deployment, updated = client.deploy_manifest(
                new_files, manifest, recover=lambda path: _fetch_live(pages_project, path)
            )
        except (PagesDeployError, httpx.HTTPError) as e:
            logger.error("Pages deploy failed (attempt %d): %s", attempt, e)
            continue
        logger.info("Pages deployment %s created (%d file(s))", deployment, len(updated))
        if _page_is_live(pages_project, slug):
            # Only after the page is proven live: a manifest recording a deploy
            # that did not land would describe a site that does not exist.
            manifest_store.save(updated)
            logger.info(
                "Published HTML digest to Pages project %s (attempt %d)", pages_project, attempt
            )
            return True
    return False


def _fetch_live(pages_project: str, path: str) -> bytes | None:
    """Re-fetch an archived page from the site it was deployed to.

    Pages serves the extensionless clean URL and 308s `.html` to it, so the
    redirect has to be followed or the bytes come back as a redirect body.
    """
    clean = path.removesuffix(".html").removesuffix("/index")
    url = f"https://{pages_project}.pages.dev{clean or '/'}"
    try:
        resp = httpx.get(url, timeout=VERIFY_TIMEOUT_SECONDS, follow_redirects=True)
    except httpx.HTTPError as e:
        logger.error("Could not recover %s from the live site: %s", path, e)
        return None
    if resp.status_code != 200:
        logger.error("Could not recover %s: %s answered %d", path, url, resp.status_code)
        return None
    return resp.content


def _client(pages_project: str) -> PagesClient | None:
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    if not (account and token):
        logger.error(
            "Pages deploy needs CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN "
            "(the token needs Cloudflare Pages -> Edit)."
        )
        return None
    return PagesClient(account, token, pages_project)


def _deploy_once(html_dir: Path, pages_project: str, attempt: int) -> bool:
    """One direct-upload deployment. Whether it is *live* is `_page_is_live`'s job."""
    client = _client(pages_project)
    if client is None:
        return False
    try:
        deployment = client.deploy(html_dir)
    except (PagesDeployError, httpx.HTTPError) as e:
        logger.error("Pages deploy failed (attempt %d): %s", attempt, e)
        return False
    logger.info("Pages deployment %s created", deployment)
    return True


def _page_is_live(pages_project: str, slug: str) -> bool:
    """Fetch the deployed page and assert it is the digest, not the 404 fallback.

    wrangler exits 0 without deploying, and truncates its own output mid-upload,
    so neither the exit code nor a stdout receipt proves anything — that is how
    the 2026-08-18 evening and 2026-08-20 morning digests lost their Discord
    links while both deploy attempts "succeeded". A missing page is served as
    the Archive index with HTTP 200, so the status code proves nothing either:
    the receipt is the page's own <title>, which carries the digest date.

    ponytail: a re-run of the same period passes on the previous deploy's page.
    Re-runs overwrite the same URL, so the link is never dead — only possibly stale.
    """
    date = slug[:10]
    url = f"https://{pages_project}.pages.dev/{slug}"
    for poll in range(1, VERIFY_POLLS + 1):
        if poll > 1:
            time.sleep(VERIFY_INTERVAL_SECONDS)
        try:
            resp = httpx.get(url, timeout=VERIFY_TIMEOUT_SECONDS, follow_redirects=True)
        except httpx.HTTPError as e:
            logger.warning("Pages verify request failed (poll %d): %s", poll, e)
            continue
        match = _TITLE_RE.search(resp.text)
        title = match.group(1).strip() if match else ""
        if date in title:
            return True
        logger.warning(
            "Pages verify: %s served %d with title %r, expected one containing %s (poll %d)",
            url,
            resp.status_code,
            title,
            date,
            poll,
        )
    logger.error("Pages deploy reported success but %s is not live", url)
    return False
