"""Full-text markdown extraction via the defuddle Node library (subprocess shim)."""

import json
import logging
import shutil
import subprocess
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

SHIM_PATH = Path(__file__).parent / "defuddle_extract.mjs"
DEFAULT_BUN_PATH = "~/.bun/bin/bun"
TIMEOUT_SECONDS = 60
FETCH_TIMEOUT_SECONDS = 30
# Some sites 403 non-browser user agents.
FETCH_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)


def _strip_leading_title_headings(markdown: str, title: str) -> str:
    """Drop leading heading lines when one of them duplicates the document title.

    defuddle >= 0.13 keeps the page-header site name and title as leading
    headings; the exporter re-adds the title itself, so they only duplicate.
    """
    if not title:
        return markdown

    lines = markdown.split("\n")
    boundary = 0
    has_title = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            boundary = i + 1
            if stripped.lstrip("#").strip().casefold() == title.strip().casefold():
                has_title = True
            continue
        break

    if not has_title:
        return markdown
    return "\n".join(lines[boundary:]).lstrip("\n")


def _resolve_bun(bun_path: str) -> str | None:
    """Resolve the bun binary: configured path first, then PATH lookup.

    The configured default targets a host install (~/.bun/bin/bun); in a
    container bun ships on PATH instead, so which() keeps both working.
    """
    candidate = Path(bun_path).expanduser()
    if candidate.exists():
        return str(candidate)
    return shutil.which("bun")


def extract_markdown(html: str, url: str, bun_path: str = DEFAULT_BUN_PATH) -> str | None:
    """Run the defuddle shim over html; return cleaned markdown, or None on failure."""
    bun = _resolve_bun(bun_path)
    if bun is None:
        logger.warning("bun not found (%s or PATH); skipping defuddle extraction", bun_path)
        return None
    try:
        proc = subprocess.run(
            [bun, str(SHIM_PATH), url],
            input=html.encode(),
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.warning("defuddle extraction failed for %s", url, exc_info=True)
        return None

    if proc.returncode != 0:
        logger.warning("defuddle exited %d for %s: %s", proc.returncode, url, proc.stderr[:500])
        return None

    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        logger.warning("defuddle returned invalid JSON for %s", url)
        return None

    content = (result.get("content") or "").strip()
    if not content:
        logger.warning("defuddle returned empty content for %s", url)
        return None
    return _strip_leading_title_headings(content, result.get("title") or "")


def _fetch_html(url: str) -> str | None:
    try:
        resp = httpx.get(
            url,
            follow_redirects=True,
            timeout=FETCH_TIMEOUT_SECONDS,
            headers={"User-Agent": FETCH_USER_AGENT},
        )
        resp.raise_for_status()
        return resp.text
    except httpx.HTTPError:
        logger.warning("Failed to fetch %s for full-text extraction", url, exc_info=True)
        return None


def fetch_full_markdown(url: str, feed_html: str, bun_path: str = DEFAULT_BUN_PATH) -> str | None:
    """Best-effort clean markdown for an article: live page first, feed HTML second.

    Both candidates run through defuddle; the longer result wins, so a
    paywalled live page (teaser) loses to full feed content and vice versa.
    Returns None when neither yields content.
    """
    candidates = []

    page_html = _fetch_html(url)
    if page_html:
        md = extract_markdown(page_html, url, bun_path)
        if md:
            candidates.append(md)

    if feed_html:
        md = extract_markdown(feed_html, url, bun_path)
        if md:
            candidates.append(md)

    return max(candidates, key=len) if candidates else None
