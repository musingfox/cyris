"""Cloudflare account-level checks that aren't tied to one Worker."""

from __future__ import annotations

import httpx

API_ROOT = "https://api.cloudflare.com/client/v4"
TIMEOUT_SECONDS = 15


def check_pages_access(account_id: str, project: str, token: str) -> tuple[bool, str]:
    """Ask the Pages API whether this token can see the project. (ok, message).

    Deliberately not `/user/tokens/verify`: that endpoint only answers for
    *user* tokens, and rejects a perfectly good account-owned one. Asking the
    API that publishing actually uses tests liveness and the right permission in
    the same call, which is the question worth answering.
    """
    if not project:
        return False, "[promote] pages_project is empty, so there is nothing to publish to"

    url = f"{API_ROOT}/accounts/{account_id}/pages/projects/{project}"
    try:
        resp = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT_SECONDS)
        body = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        return False, f"could not reach the Pages API: {e}"

    if body.get("success"):
        return True, f"can publish to {project}"

    errors = body.get("errors") or [{"message": f"HTTP {resp.status_code}"}]
    return False, "; ".join(str(e.get("message", e)) for e in errors)


def list_worker_domains(account_id: str, token: str, service: str) -> list[str]:
    """The custom domains attached to the Worker named `service`, sorted.

    Needs Workers Scripts Read on the token. Raises RuntimeError carrying the
    API's own message, so a caller that falls back can still say why.
    """
    url = f"{API_ROOT}/accounts/{account_id}/workers/domains"
    try:
        resp = httpx.get(
            url,
            params={"service": service},
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT_SECONDS,
        )
        body = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        raise RuntimeError(f"could not reach the Workers domains API: {e}") from e

    if not body.get("success"):
        errors = body.get("errors") or [{"message": f"HTTP {resp.status_code}"}]
        raise RuntimeError("; ".join(str(e.get("message", e)) for e in errors))
    return sorted(d["hostname"] for d in body.get("result") or [] if d.get("hostname"))
