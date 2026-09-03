"""Tests for workers/app routing and authentication."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "workers/app/src/router.js"


def test_triage_returns_404_before_archive_proxy():
    worker_js = ROUTER.read_text()
    fetch_start = worker_js.index("export async function handleRequest")
    assert '"/triage"' in worker_js
    triage_check = worker_js.index('url.pathname === "/triage"', fetch_start)
    protected_check = worker_js.index("if (!PROTECTED(url.pathname))", fetch_start)
    digest_origin = worker_js.index("DIGEST_ORIGIN", protected_check)
    assert triage_check < protected_check
    assert triage_check < digest_origin
    triage_block = worker_js[triage_check : worker_js.index("}", triage_check)]
    assert "404" in triage_block


def test_access_host_flag_is_consulted():
    assert "CYRIS_UI_ACCESS_HOST" in ROUTER.read_text()


def test_login_and_cookie_compare_via_ct_equal():
    text = ROUTER.read_text()
    assert "const ctEqual" in text
    assert "=== env.CYRIS_UI_TOKEN" not in text
    assert "!== env.CYRIS_UI_TOKEN" not in text
    assert "cookie ===" not in text
    assert "cookie !==" not in text
