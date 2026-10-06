#!/usr/bin/env python3
"""Ask Cloudflare what the trial wizard cannot see from the shell.

`scripts/trial-wizard.sh` calls this in stage 7, to check a new trial token before
anything relies on it: a token missing Cloudflare Pages → Edit let the first
trial's run fetch, score and summarize, then fail at publishing (2026-10-06).

The token comes on stdin, never as an argument, so it stays out of the process
list and out of every line this prints. Every call is read-only or rejected
before it acts: the mail probe sends an empty body.

Usage:
  ... | trial_api.py check-token --account-id ID --d1-id UUID --pages-project P
                                 --app-worker W [--email]
"""

import argparse
import sys
from collections.abc import Callable

import httpx

API_ROOT = "https://api.cloudflare.com/client/v4"
TIMEOUT_SECONDS = 30
# Measured 2026-10-06 with a token that may send mail: an empty body is refused
# with this schema error, after the permission check; a token without Email
# Sending gets 10000 instead. Nothing else answers the question without a send.
MAIL_SCHEMA_ERROR = 10001


def _body(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except ValueError:
        return {"success": False, "errors": [{"code": resp.status_code}]}


def _codes(body: dict) -> set[int]:
    return {e.get("code") for e in body.get("errors") or []}


def missing_permissions(
    client: httpx.Client,
    account: str,
    d1_id: str,
    pages_project: str,
    app_worker: str,
    email: bool,
) -> list[str]:
    """Each permission the app needs that this token lacks, as the dashboard names it.

    Each probe is the call the app itself makes. D1 accepts a read token for a
    `SELECT`, so D1 → Read passes here and fails at the first boot's settings push.
    """
    base = f"/accounts/{account}"
    probes = [
        (
            "D1 → Edit",
            lambda: client.post(f"{base}/d1/database/{d1_id}/query", json={"sql": "SELECT 1"}),
            lambda b: b.get("success") is True,
        ),
        (
            "Cloudflare Pages → Edit",
            lambda: client.get(f"{base}/pages/projects/{pages_project}/upload-token"),
            lambda b: b.get("success") is True,
        ),
        (
            "Workers Scripts → Read",
            lambda: client.get(f"{base}/workers/domains", params={"service": app_worker}),
            lambda b: b.get("success") is True,
        ),
    ]
    if email:
        probes.append(
            (
                "Email Sending → Edit",
                lambda: client.post(f"{base}/email/sending/send", json={}),
                lambda b: MAIL_SCHEMA_ERROR in _codes(b),
            )
        )
    return [name for name, call, granted in probes if not granted(_body(call()))]


def _client(token: str) -> httpx.Client:
    return httpx.Client(
        base_url=API_ROOT,
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT_SECONDS,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("check-token")
    p.add_argument("--account-id", required=True)
    p.add_argument("--d1-id", required=True)
    p.add_argument("--app-worker", required=True)
    p.add_argument("--pages-project", required=True)
    p.add_argument("--email", action="store_true")
    return parser


def main(
    argv: list[str] | None = None,
    stdin=None,
    client_for: Callable[[str], httpx.Client] = _client,
) -> int:
    args = build_parser().parse_args(argv)
    token = (stdin or sys.stdin).readline().strip()
    if not token:
        print("No token on stdin.", file=sys.stderr)
        return 1
    with client_for(token) as client:
        missing = missing_permissions(
            client, args.account_id, args.d1_id, args.pages_project, args.app_worker, args.email
        )
        for name in missing:
            print(f"  ✗ the token lacks {name}")
        if not missing:
            print("  ✓ the token has every permission the trial needs")
        return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
