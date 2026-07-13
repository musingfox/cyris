"""Browser cookie loading utilities."""

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


def load_browser_cookies(browser: str, domain_filter: list[str]) -> dict[str, str]:
    """Load cookies from browser for specified domains.

    Returns cookie name→value dict. Returns empty dict on any error.
    """
    if not domain_filter:
        return {}

    # Special handling for Zen Browser (Firefox-based)
    if browser == "zen":
        return _load_zen_cookies(domain_filter)

    try:
        import browser_cookie3
    except ImportError:
        logger.warning("browser-cookie3 not installed; skipping cookie loading")
        return {}

    loader_map = {
        "chrome": browser_cookie3.chrome,
        "firefox": browser_cookie3.firefox,
        "edge": browser_cookie3.edge,
        "safari": browser_cookie3.safari,
    }
    loader = loader_map.get(browser)
    if loader is None:
        logger.error("Unknown browser: %s", browser)
        return {}

    try:
        cj = loader(domain_name="")  # Load all cookies
    except Exception:
        logger.warning("Failed to load cookies from %s", browser, exc_info=True)
        return {}

    cookies = {}
    for cookie in cj:
        if any(
            cookie.domain.endswith(d) or d.endswith(cookie.domain.lstrip("."))
            for d in domain_filter
        ):
            cookies[cookie.name] = cookie.value
    return cookies


def _load_zen_cookies(domain_filter: list[str]) -> dict[str, str]:
    """Load cookies from Zen Browser (Firefox-compatible).

    Scans ~/Library/Application Support/zen/Profiles/ for cookies.sqlite.
    Returns empty dict on error.
    """
    zen_base = Path.home() / "Library" / "Application Support" / "zen" / "Profiles"

    if not zen_base.exists():
        logger.warning("Zen Browser profile directory not found: %s", zen_base)
        return {}

    # Find first profile with cookies.sqlite
    cookies_db = None
    for profile_dir in zen_base.iterdir():
        if profile_dir.is_dir():
            candidate = profile_dir / "cookies.sqlite"
            if candidate.exists():
                cookies_db = candidate
                break

    if not cookies_db:
        logger.warning("No cookies.sqlite found in Zen profiles")
        return {}

    try:
        conn = sqlite3.connect(f"file:{cookies_db}?immutable=1", uri=True)
        cursor = conn.cursor()
        cursor.execute("SELECT host, name, value FROM moz_cookies")
        rows = cursor.fetchall()
        conn.close()

        cookies = {}
        for host, name, value in rows:
            # Match domain filter (handle both .domain.com and domain.com)
            if any(host.endswith(d) or d.endswith(host.lstrip(".")) for d in domain_filter):
                cookies[name] = value

        logger.debug("Loaded %d cookies from Zen Browser", len(cookies))
        return cookies

    except Exception:
        logger.warning("Failed to read Zen Browser cookies", exc_info=True)
        return {}
