"""Judge the hosted tenants' reports and pages: each tenant's runs, issues and page are its own.

Usage: python3 scripts/hosted_check.py REPORT.json [REPORT.json ...]

Each file is one tenant's answer to the hosted Worker's `GET /hosted/tenants/<tenant>/report`
(docs/hosted-tenants.md). Beside it, the same name with `.html` is the page the Worker served
that tenant at `/t/<tenant>/`; `scripts/hosted.sh check` writes it only when the page answered
200. A tenant passes when its database holds at least one finished, non-dry `ok` run and at
least one issue, every article it stored came from one of its own sources, and its page is its
own newest issue. The seed gives no two tenants a source in common, so an article another
tenant's run fetched would show up here as a source this tenant was never given.

The page is the tenant's when its receipt (`<meta name="cyris-issue">`) names the issue the
tenant saved last, and its SHA-256 is the one the tenant's own database reports for that page
and no other tenant's. The reader's type size, which the Worker may add to `<head>` (ADR-0013),
is left out of the hash.

Standard library only, so the runbook can call it with `python3`. Prints one line per tenant and
exits 1 when any tenant fails.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

# The issue a digest page names, and the type size the Worker may add (`typeScaleStyle`).
RECEIPT = re.compile(rb'<meta name="cyris-issue" content="([^"]*)">')
TYPE_SIZE = re.compile(rb"<style>html:root\{--type-scale:[0-9.]+\}</style>")


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


def page_problems(report: dict, others: list[dict], page: bytes | None) -> list[str]:
    """What keeps the page `/t/<tenant>/` served from being `report`'s tenant's newest issue."""
    if page is None:
        return ["its latest page did not answer 200"]
    found: list[str] = []
    issues = sorted(report.get("digests", []), key=lambda d: str(d.get("saved_at", "")))
    receipt = RECEIPT.search(page)
    served = receipt.group(1).decode("utf-8") if receipt else ""
    if issues:
        newest = f"{issues[-1].get('date')}-{issues[-1].get('period')}"
        if served != newest:
            found.append(f"its latest page is issue {served or 'none'}, not its newest {newest}")
    digest = hashlib.sha256(TYPE_SIZE.sub(b"", page)).hexdigest()
    own = (report.get("latest_page") or {}).get("sha256")
    if digest != own:
        found.append("its latest page is not the one its own database holds")
    for other in others:
        if digest == (other.get("latest_page") or {}).get("sha256"):
            found.append(f"its latest page is {other.get('tenant')}'s")
    return found


def main(paths: list[str]) -> int:
    if not paths:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    reports = [json.loads(Path(path).read_text(encoding="utf-8")) for path in paths]
    pages = [Path(path).with_suffix(".html") for path in paths]
    failed = False
    for report, page in zip(reports, pages, strict=True):
        others = [r for r in reports if r is not report]
        tenant = report.get("tenant", "?")
        served = page.read_bytes() if page.exists() else None
        found = problems(report, others) + page_problems(report, others, served)
        runs = len(report.get("digest_runs", []))
        issues = len(report.get("digests", []))
        if found:
            failed = True
            print(f"FAIL {tenant}: " + "; ".join(found))
        else:
            print(
                f"PASS {tenant}: {runs} run(s), {issues} issue(s), only its own sources, "
                "and its latest page is its own newest issue"
            )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
