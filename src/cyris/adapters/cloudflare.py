"""Cloudflare account-level checks that aren't tied to one Worker."""

from __future__ import annotations

import httpx

VERIFY_URL = "https://api.cloudflare.com/client/v4/user/tokens/verify"
TIMEOUT_SECONDS = 15


def verify_api_token(token: str) -> tuple[bool, str]:
    """Ask Cloudflare whether an API token is live. Returns (valid, message).

    An expired or revoked token fails the same way a wrong one does, and every
    caller of it — `wrangler pages deploy`, the D1 store — reports that as its
    own failure, which is how a dead token stays invisible.
    """
    try:
        resp = httpx.get(
            VERIFY_URL, headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT_SECONDS
        )
        body = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        return False, f"could not reach the Cloudflare API: {e}"

    if body.get("success"):
        return True, str((body.get("result") or {}).get("status", "active"))

    errors = body.get("errors") or [{"message": "unknown error"}]
    return False, "; ".join(str(e.get("message", e)) for e in errors)
