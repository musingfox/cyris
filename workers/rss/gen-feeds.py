#!/usr/bin/env python3
"""Regenerate src/feeds.json from sources.example.yaml.

feeds.json is the Worker's **fallback**, not its source of truth. At poll time it
reads the `sources` table in D1 (written by `cyris sources push`), so adding a
feed is a write rather than a redeploy. The bundle is what a Worker polls before
the first push, or when the D1 read fails — polling nothing would be a silent
outage that just looks like a quiet news day.

It is generated from the *example* list on purpose, never from `sources.yaml`.
This file is committed to a public repository and is what a stranger's fork
polls on its first tick; `sources.yaml` is gitignored personal subscriptions.
Pointing this at it would publish the author's reading list and make every fork
buffer feeds nobody asked for. tests/test_rss_worker_bundle.py enforces it.
"""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).parent / "src" / "feeds.json"


def sources_path(root: Path) -> Path:
    example = root / "sources.example.yaml"
    if not example.is_file():
        raise FileNotFoundError(f"sources.example.yaml not found under {root}")
    return example


def main() -> None:
    sources = yaml.safe_load(sources_path(ROOT).read_text(encoding="utf-8"))["sources"]
    feeds = [
        {"name": s["name"], "url": s["url"]}
        for s in sources
        if s.get("url") and s.get("type", "rss") == "rss"
    ]
    OUT.write_text(json.dumps(feeds, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(feeds)} feeds to {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
