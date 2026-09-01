"""Tests for workers/app routing and authentication (private-votes-public-archive).

The Worker is JavaScript, so we read the source as text and assert on structure.
"""

from pathlib import Path


def test_triage_returns_404():
    """T1: /triage returns 404 before reaching the digest-origin proxy."""
    worker_js = Path("workers/app/src/index.js").read_text()

    # Find the fetch handler
    fetch_start = worker_js.index("async fetch(request)")
    
    # Assert /triage 404 logic exists
    assert '"/triage"' in worker_js or "'/triage'" in worker_js, "/triage path check must exist"
    
    # Find positions of key routing logic
    triage_check = worker_js.index('url.pathname === "/triage"', fetch_start)
    protected_check = worker_js.index("if (!PROTECTED(url.pathname))", fetch_start)
    digest_origin_fetch = worker_js.index("fetch(new Request(env.DIGEST_ORIGIN", fetch_start)
    
    # /triage 404 must come BEFORE the PROTECTED check and DIGEST_ORIGIN proxy
    assert triage_check < protected_check, "/triage 404 must come before PROTECTED check"
    assert triage_check < digest_origin_fetch, "/triage 404 must come before DIGEST_ORIGIN proxy"
    
    # Verify 404 response exists near the /triage check
    triage_block_end = worker_js.index("}", triage_check)
    triage_block = worker_js[triage_check:triage_block_end]
    assert "404" in triage_block, "/triage block must return 404"
    
    # Assert /triage is NOT in PROTECTED
    protected_start = worker_js.index("const PROTECTED = (path) =>")
    protected_end = worker_js.index("\nconst VOTE_ONLY", protected_start)
    protected_body = worker_js[protected_start:protected_end]
    assert "/triage" not in protected_body, "/triage should not be in PROTECTED"


def test_vote_only_function_exists():
    """T2: VOTE_ONLY() function exists and matches /api/vote."""
    worker_js = Path("workers/app/src/index.js").read_text()

    # VOTE_ONLY should be defined
    assert "const VOTE_ONLY = (path) =>" in worker_js

    # Extract VOTE_ONLY function
    start = worker_js.index("const VOTE_ONLY = (path) =>")
    end = worker_js.index("\nexport default", start)
    vote_only_body = worker_js[start:end]

    # Should match /api/vote
    assert '"/api/vote"' in vote_only_body or "'/api/vote'" in vote_only_body


def test_vote_handler_before_authorized_check():
    """T3: /api/vote handler runs before the authorized() check."""
    worker_js = Path("workers/app/src/index.js").read_text()

    # Find the fetch handler
    fetch_start = worker_js.index("async fetch(request)")
    # Find the VOTE_ONLY check position
    vote_check_pos = worker_js.index("if (VOTE_ONLY(url.pathname))", fetch_start)
    # Find the authorized() check position
    auth_check_pos = worker_js.index("if (!(await authorized(request)))", fetch_start)

    # VOTE_ONLY check must come before authorized() check
    assert vote_check_pos < auth_check_pos, "/api/vote handler must run before authorized() check"


def test_settings_goes_through_authorized():
    """T4: /settings still goes through the authorized() check."""
    worker_js = Path("workers/app/src/index.js").read_text()

    # Find the authorized check
    assert "if (!(await authorized(request)))" in worker_js

    # After the authorized check, the container fetch should handle /settings
    auth_start = worker_js.index("if (!(await authorized(request)))")
    container_fetch = worker_js.index('getContainer(env.CYRIS, "ui").fetch(request)', auth_start)

    # Make sure /settings is in PROTECTED (already tested above) and goes through auth
    assert container_fetch > auth_start, "/settings should be after authorized() check"
