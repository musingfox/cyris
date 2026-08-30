"""Rebuild D1 pages_manifest from a local html directory, wholesale.

A fresh image's first deploy names only the files its manifest knows about, and a
Pages deployment is a full snapshot — so a manifest missing archive pages deletes
them from the site. This backfills the table from a directory that still holds
the archive, using the production hash path (`_collect` → `asset_hash`) so the
stored hashes match what Cloudflare's asset store is keyed by.

Usage:
    uv run python scripts/backfill_pages_manifest.py --html-dir agent-vault/html \
        --database-id <id>

Reads `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` from the environment,
the same two `cyris.config` uses.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from cyris.adapters.output.pages_deploy import _collect
from cyris.adapters.output.pages_manifest import D1PagesManifest
from cyris.adapters.store.d1 import D1Client


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rebuild D1 pages_manifest from a html directory")
    parser.add_argument("--html-dir", required=True, type=Path)
    parser.add_argument("--database-id", required=True)
    args = parser.parse_args(argv)

    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    api_token = os.environ.get("CLOUDFLARE_API_TOKEN")
    missing = [
        name
        for name, value in [
            ("CLOUDFLARE_ACCOUNT_ID", account_id),
            ("CLOUDFLARE_API_TOKEN", api_token),
        ]
        if not value
    ]
    if missing:
        print(f"missing environment variable(s): {', '.join(missing)}", file=sys.stderr)
        return 1

    files = _collect(args.html_dir)
    if not files:
        print(
            f"{args.html_dir} holds no files — refusing to write an empty manifest", file=sys.stderr
        )
        return 1

    client = D1Client(account_id=account_id, database_id=args.database_id, api_token=api_token)
    D1PagesManifest(client).save({f["path"]: f["hash"] for f in files})
    print(f"pages_manifest: {len(files)} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
