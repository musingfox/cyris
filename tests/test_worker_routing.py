"""Tests for workers/app routing and authentication (private-votes-public-archive).

The Worker is JavaScript, so we read the source as text and assert on structure.
"""

from pathlib import Path


def test_triage_not_in_protected():
    """T1: /triage is NOT in the PROTECTED() function."""
    worker_js = Path("workers/app/src/index.js").read_text()

    # Find the PROTECTED function definition
    assert "const PROTECTED = (path) =>" in worker_js

    # Extract the PROTECTED function body (between its opening and the next function/export)
    start = worker_js.index("const PROTECTED = (path) =>")
    # Find the end of the PROTECTED function (next const or export)
    end = worker_js.index("\nconst VOTE_ONLY", start)
    protected_body = worker_js[start:end]

    # Assert /triage is NOT in PROTECTED
    assert "/triage" not in protected_body, "/triage should not be in PROTECTED"
    # Assert /settings IS in PROTECTED
    assert "/settings" in protected_body, "/settings should be in PROTECTED"


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
