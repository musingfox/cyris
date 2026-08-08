#!/usr/bin/env python3
"""Generate src/feeds.json from sources.yaml — run before `wrangler deploy`.

sources.yaml stays the single source of truth; the Worker bundles a snapshot of
it rather than syncing at runtime (the list changes about monthly, and a KV sync
would need its own endpoint, auth, and drift handling).
"""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).parent / "src" / "feeds.json"


def main() -> None:
    sources = yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8"))["sources"]
    feeds = [
        {"name": s["name"], "url": s["url"]}
        for s in sources
        if s.get("url") and s.get("type", "rss") == "rss"
    ]
    OUT.write_text(json.dumps(feeds, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(feeds)} feeds to {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
