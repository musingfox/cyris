"""The RSS Worker's bundled fallback must be the example feeds, not a personal list.

`feeds.js` falls back to this file whenever the `sources` table is empty or
unreadable, which is exactly the state a fresh fork is in. Whatever is committed
here is what a stranger's Worker polls on its first tick, so it has to be the
tracked example rather than whoever last ran `gen-feeds.py` against their own
`sources.yaml` — that file is gitignored and never leaves the author's machine.
"""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_bundled_feeds_match_the_tracked_example() -> None:
    bundled = json.loads((ROOT / "workers/rss/src/feeds.json").read_text(encoding="utf-8"))
    sources = yaml.safe_load((ROOT / "sources.example.yaml").read_text(encoding="utf-8"))["sources"]
    expected = [
        {"name": s["name"], "url": s["url"]}
        for s in sources
        if s.get("url") and s.get("type", "rss") == "rss"
    ]
    assert bundled == expected, "regenerate with `uv run python workers/rss/gen-feeds.py`"
