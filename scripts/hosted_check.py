"""Judge the hosted tenants' reports: each tenant's runs and issues are its own.

Usage: python3 scripts/hosted_check.py REPORT.json [REPORT.json ...]

Each file is one tenant's answer to the hosted Worker's `GET /hosted/tenants/<tenant>/report`
(docs/hosted-tenants.md). A tenant passes when its database holds at least one finished,
non-dry `ok` run and at least one issue, and every article it stored came from one of its own
sources. The seed gives no two tenants a source in common, so an article another tenant's run
fetched would show up here as a source this tenant was never given.

Standard library only, so the runbook can call it with `python3`. Prints one line per tenant and
exits 1 when any tenant fails.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def problems(report: dict, others: list[dict]) -> list[str]:
    """What keeps `report`'s tenant from passing; empty when it passes."""
    found: list[str] = []
    own = set(report.get("sources", []))
    if not own:
        found.append("it has no sources: seed it first")
    runs = report.get("digest_runs", [])
    if not any(r.get("status") == "ok" and not r.get("dry_run") for r in runs):
        statuses = ", ".join(str(r.get("status")) for r in runs) or "none"
        found.append(f"no finished ok run (runs: {statuses})")
    if not report.get("digests"):
        found.append("no issue in digests")
    stored = {row.get("source_name") for row in report.get("stored_articles", [])}
    if foreign := sorted(str(name) for name in stored - own):
        found.append(f"articles from sources it was never given: {', '.join(foreign)}")
    for other in others:
        if shared := sorted(own & set(other.get("sources", []))):
            found.append(f"shares sources with {other.get('tenant')}: {', '.join(shared)}")
    return found


def main(paths: list[str]) -> int:
    if not paths:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    reports = [json.loads(Path(path).read_text(encoding="utf-8")) for path in paths]
    failed = False
    for report in reports:
        others = [r for r in reports if r is not report]
        tenant = report.get("tenant", "?")
        found = problems(report, others)
        runs = len(report.get("digest_runs", []))
        issues = len(report.get("digests", []))
        if found:
            failed = True
            print(f"FAIL {tenant}: " + "; ".join(found))
        else:
            print(f"PASS {tenant}: {runs} run(s), {issues} issue(s), only its own sources")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
