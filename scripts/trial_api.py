#!/usr/bin/env python3
"""Ask Cloudflare what the trial wizard cannot see from the shell.

`scripts/trial-wizard.sh` calls this at two points. Its stage 7 checks a new trial
token before anything relies on it: a token missing Cloudflare Pages → Edit let
the first trial's run fetch, score and summarize, then fail at publishing
(2026-10-06). Its stage 17 waits for the first run's `digest_runs` row, because
`wrangler tail` never shows it: the container's stdout reaches Workers Logs only.

The token comes on stdin, never as an argument, so it stays out of the process
list and out of every line this prints. Every call is read-only or rejected
before it acts: the mail probe sends an empty body.

Usage:
  ... | trial_api.py check-token --account-id ID --d1-id UUID --pages-project P
                                 --app-worker W [--email]
  ... | trial_api.py latest-run  --account-id ID --d1-id UUID
  ... | trial_api.py wait-run    --account-id ID --d1-id UUID --app-worker W --after N
                                 [--timeout S] [--interval S]
"""

import argparse
import json
import sys
import time
from collections.abc import Callable

import httpx

API_ROOT = "https://api.cloudflare.com/client/v4"
TIMEOUT_SECONDS = 30
# Measured 2026-10-06 with a token that may send mail: an empty body is refused
# with this schema error, after the permission check; a token without Email
# Sending gets 10000 instead. Nothing else answers the question without a send.
MAIL_SCHEMA_ERROR = 10001
NOT_COUNTS = {"id", "summary"}


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


def latest_run(client: httpx.Client, account: str, d1_id: str) -> dict | None:
    resp = client.post(
        f"/accounts/{account}/d1/database/{d1_id}/query",
        json={"sql": "SELECT * FROM digest_runs ORDER BY id DESC LIMIT 1"},
    )
    body = _body(resp)
    if body.get("success") is not True:
        raise RuntimeError(f"digest_runs query failed: errors {sorted(_codes(body))}")
    rows = body["result"][0]["results"]
    return rows[0] if rows else None


def wait_for_run(
    client: httpx.Client,
    account: str,
    d1_id: str,
    after: int,
    timeout: float,
    interval: float,
    report: Callable[[str], None],
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict | None:
    """The first `digest_runs` row with an id above `after`, or None at `timeout`."""
    start = clock()
    while True:
        row = latest_run(client, account, d1_id)
        if row is not None and row["id"] > after:
            return row
        waited = clock() - start
        if waited >= timeout:
            return None
        report(f"  no new run yet, {int(waited)}s waited")
        sleep(interval)


def describe(row: dict) -> list[str]:
    lines = [f"  run {row['id']}: status {row.get('status')}"]
    lines += [f"    {k} = {v}" for k, v in row.items() if k not in NOT_COUNTS | {"status"}]
    try:
        summary = json.loads(row.get("summary") or "{}")
    except ValueError:
        summary = {}
    counts = {k: v for k, v in summary.items() if isinstance(v, int | float) and k not in row}
    lines += [f"    {k} = {v}" for k, v in counts.items()]
    if row.get("status") == "publish_failed":
        lines.append(
            "  Publishing failed. The usual cause is a token without Cloudflare Pages → Edit:"
            " re-run the wizard with --from 7 to check it."
        )
    return lines


def _client(token: str) -> httpx.Client:
    return httpx.Client(
        base_url=API_ROOT,
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT_SECONDS,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check-token", "latest-run", "wait-run"):
        p = sub.add_parser(name)
        p.add_argument("--account-id", required=True)
        p.add_argument("--d1-id", required=True)
        if name != "latest-run":
            p.add_argument("--app-worker", required=True)
        if name == "check-token":
            p.add_argument("--pages-project", required=True)
            p.add_argument("--email", action="store_true")
        if name == "wait-run":
            p.add_argument("--after", required=True, type=int)
            p.add_argument("--timeout", type=float, default=900)
            p.add_argument("--interval", type=float, default=15)
    return parser


def main(
    argv: list[str] | None = None,
    stdin=None,
    client_for: Callable[[str], httpx.Client] = _client,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    args = build_parser().parse_args(argv)
    token = (stdin or sys.stdin).readline().strip()
    if not token:
        print("No token on stdin.", file=sys.stderr)
        return 1
    try:
        with client_for(token) as client:
            return _run(args, client, sleep, clock)
    except (RuntimeError, httpx.HTTPError) as exc:
        print(f"  {exc}", file=sys.stderr)
        return 1


def _run(args, client: httpx.Client, sleep, clock) -> int:
    if args.command == "check-token":
        missing = missing_permissions(
            client, args.account_id, args.d1_id, args.pages_project, args.app_worker, args.email
        )
        for name in missing:
            print(f"  ✗ the token lacks {name}")
        if not missing:
            print("  ✓ the token has every permission the trial needs")
        return 1 if missing else 0
    if args.command == "latest-run":
        row = latest_run(client, args.account_id, args.d1_id)
        print(row["id"] if row else 0)
        return 0
    row = wait_for_run(
        client,
        args.account_id,
        args.d1_id,
        args.after,
        args.timeout,
        args.interval,
        report=lambda line: print(line, flush=True),
        sleep=sleep,
        clock=clock,
    )
    if row is None:
        print(
            f"  No digest_runs row after {int(args.timeout)}s. The run's own log is in"
            f" Workers Logs: https://dash.cloudflare.com/{args.account_id}/workers-and-pages"
            f" → {args.app_worker} → Observability."
        )
        return 2
    print("\n".join(describe(row)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
