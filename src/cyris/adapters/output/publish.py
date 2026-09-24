"""Publish the HTML digest directory to Cloudflare Pages.

Deploying goes through `pages_deploy.PagesClient` (the REST direct-upload
protocol), not `wrangler pages deploy`. What the deploy is recorded as follows
Cloudflare's own verdict on the deployment — its stage — because the manifest
has to name what the next full snapshot must keep, and that is what Cloudflare
deployed, not what the edge happens to serve yet. Whether the digest counts as
*published* is still `_page_is_live`'s call: it is the receipt that caught the
2026-08-18/08-20 silent failures, and the reader's link depends on it.
"""

import logging
import os
import re
import time
from pathlib import Path

import httpx

from cyris.adapters.output.pages_deploy import DeploymentRecord, PagesClient, PagesDeployError

logger = logging.getLogger(__name__)

DEPLOY_ATTEMPTS = 3
# One page, because one is the largest shortfall this system can legitimately
# produce. Two things put the site one page ahead of the manifest: a deployment
# that landed but whose manifest save then failed, and one Cloudflare gave no
# verdict on and whose page the alias did not serve in time, which lands later.
# The lead cannot accumulate: the next deploy is a full snapshot of `new_files`
# plus the manifest, and that page is in neither, so it is dropped and the count
# returns to one. Anything above that is a database describing a different
# archive: a staging clone or a point-in-time restore a few days behind, which
# is exactly what this guard is for. A same-size-but-different archive is caught
# regardless, because the check is a set difference and not a count.
ARCHIVE_SHORTFALL_TOLERANCE = 1
# Deploy attempts retry a deployment Cloudflare refused, never one it accepted:
# on 2026-09-24 three deployments each reached `success` within 2s, the page was
# still the fallback 35s after the first, and each redeploy only restarted the
# wait. A new page usually shows by the second poll, but its worst case is not
# measured, so the window is two minutes — spent only on a slow day.
VERIFY_POLLS = 25
VERIFY_INTERVAL_SECONDS = 5
# Reading the archive that is already live waits on no propagation.
LIVE_INDEX_POLLS = 3
VERIFY_TIMEOUT_SECONDS = 15
# A direct upload reached `success` within 2s on 2026-09-24, so 30s of polling is
# generous; a deployment still without a verdict after it goes to the alias check.
STAGE_POLLS = 6
STAGE_INTERVAL_SECONDS = 5

_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _slash(path: str) -> str:
    return path if path.startswith("/") else f"/{path}"


_HREF_RE = re.compile(r"""href=["']([^"']+)["']""", re.IGNORECASE)
_DATED_HTML = re.compile(r"^/?\d{4}-\d{2}-\d{2}-.+\.html$")


def _parse_archive_anchors(html: str) -> set[str]:
    """Dated digest hrefs on the live archive index, each with a leading slash."""
    found: set[str] = set()
    for href in _HREF_RE.findall(html):
        if "://" in href or not _DATED_HTML.match(href):
            continue
        path = _slash(href)
        if path.endswith("-raw.html"):
            continue
        found.add(path)
    return found


def _archive_shortfall(live: set[str], manifest_paths) -> set[str]:
    """Live archive pages this manifest would not put on the next deploy."""
    kept = {_slash(p) for p in manifest_paths}
    return {_slash(p) for p in live} - kept


def _fetch_live_index(pages_project: str) -> set[str] | None:
    url = f"https://{pages_project}.pages.dev/"
    for poll in range(1, LIVE_INDEX_POLLS + 1):
        if poll > 1:
            time.sleep(VERIFY_INTERVAL_SECONDS)
        try:
            resp = httpx.get(url, timeout=VERIFY_TIMEOUT_SECONDS, follow_redirects=True)
        except httpx.HTTPError as e:
            logger.warning("Live archive request failed (poll %d): %s", poll, e)
            continue
        if resp.status_code != 200:
            logger.warning("Live archive: %s answered %d (poll %d)", url, resp.status_code, poll)
            continue
        return _parse_archive_anchors(resp.text)
    logger.error("The live archive could not be read at %s", url)
    return None


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
        if not _deploy_once(html_dir, pages_project, attempt):
            continue
        if not _page_is_live(pages_project, slug):
            return False
        logger.info(
            "Published HTML digest to Pages project %s (attempt %d)", pages_project, attempt
        )
        return True
    return False


def _project_exists_with_deployments(client: PagesClient, pages_project: str) -> bool:
    """The empty-manifest probe, for a project that may not exist yet.

    A 404 here is not the guard's subject: the guard exists to stop a wrong
    `database_id` from deploying an empty archive over a live site, and a project
    with nothing behind it is not that. Create it and let the first publish run.
    Any other failure still refuses — the caller cannot tell an absent site from
    an unreadable one.
    """
    try:
        return client.has_deployments()
    except PagesDeployError as e:
        if e.status != 404:
            raise
    client.create_project()
    return False


def publish_site(
    new_files: dict[str, bytes],
    slug: str,
    manifest_store,
    pages_project: str,
    receipt_store,
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
    owned_at_entry = False
    if not manifest:
        try:
            owned_at_entry = receipt_store.exists(pages_project)
        except Exception as e:
            logger.error("Pages deploy receipt lookup failed: %s", e)
            return False
        if not owned_at_entry:
            try:
                if _project_exists_with_deployments(client, pages_project):
                    logger.error(
                        "Refusing to deploy an empty pages_manifest onto a Pages project "
                        "that already has deployments. Check [store] database_id, or "
                        "backfill with scripts/backfill_pages_manifest.py"
                    )
                    return False
            except Exception as e:
                logger.error("Pages deployment probe failed: %s", e)
                return False
            try:
                receipt_store.record(pages_project)
            except Exception as e:
                logger.error("Pages deploy receipt write failed: %s", e)
                return False
    if manifest or owned_at_entry:
        live = _fetch_live_index(pages_project)
        if live is None:
            if manifest:
                logger.error("Refusing to deploy: the live archive could not be read")
                return False
            # Armed only by the receipt, and the receipt is written before the
            # upload: an upload that failed outright leaves one behind for a
            # site that was never built. Refusing here would wait forever for
            # an index that only a deploy can create. A wrong database with a
            # full archive to lose does not reach this line — its index reads.
            logger.warning(
                "The live archive could not be read, but this database has no "
                "manifest to compare; deploying as a first publish"
            )
            live = set()
        missing = _archive_shortfall(live, manifest)
        if len(missing) > ARCHIVE_SHORTFALL_TOLERANCE:
            # The names, not just the count: five filenames tell an operator
            # whether this is the wrong `database_id` or a prune they meant.
            logger.error(
                "Refusing to deploy: the live archive still lists %d page(s) "
                "this manifest would drop, e.g. %s. Check [store] database_id",
                len(missing),
                ", ".join(sorted(missing)[:5]),
            )
            return False
    for attempt in range(1, DEPLOY_ATTEMPTS + 1):
        try:
            deployment, updated = client.deploy_manifest(
                new_files, manifest, recover=lambda path: _fetch_live(pages_project, path)
            )
        except (PagesDeployError, httpx.HTTPError) as e:
            logger.error("Pages deploy failed (attempt %d): %s", attempt, e)
            continue
        deployment = _await_verdict(client, deployment)
        if deployment.landed:
            # Before the alias check, not after it: on 2026-09-24 a deployment
            # was at `success` while the alias still served the fallback, and a
            # manifest that waited for the alias lost that page to the next
            # full-snapshot deploy.
            manifest_store.save(updated)
        if not _page_is_live(pages_project, slug):
            return False
        if not deployment.landed:
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
    _await_verdict(client, deployment)
    return True


def _await_verdict(client: PagesClient, deployment: DeploymentRecord) -> DeploymentRecord:
    """Re-read a deployment until Cloudflare says it landed or failed, or polls run out.

    A poll that errors is a poll spent, not a failed deployment: the deployment
    itself may be fine, and only Cloudflare's own answer decides that.
    """
    # Whether a direct upload is already at deploy/success on creation has never
    # been observed; every run's log answers it.
    logger.info(
        "Pages deployment %s created: stage %s, status %s, url %s",
        deployment.id,
        deployment.stage,
        deployment.status,
        deployment.url,
    )
    for poll in range(1, STAGE_POLLS + 1):
        if deployment.landed or deployment.failed:
            break
        time.sleep(STAGE_INTERVAL_SECONDS)
        try:
            deployment = client.get_deployment(deployment.id)
        except (PagesDeployError, httpx.HTTPError) as e:
            logger.warning(
                "Pages deployment %s: stage read failed (poll %d): %s", deployment.id, poll, e
            )
    return deployment


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
