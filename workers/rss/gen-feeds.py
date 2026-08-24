#!/usr/bin/env python3
"""Generate src/feeds.json from sources.yaml — run before `wrangler deploy`.

feeds.json is the Worker's **fallback**, not its source of truth. At poll time it
reads the `sources` table in D1 (written by `cyris sources push`), so adding a
feed is a write rather than a redeploy. The bundle is what a Worker polls before
the first push, or when the D1 read fails — polling nothing would be a silent
outage that just looks like a quiet news day. Keep it roughly current.
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
