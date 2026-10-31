"""Both feed parsers give one entry the same story image.

The Worker's `parse.js` fills the buffer and `RssSource` polls feeds directly; a
deployment on either path must show the same picture for the same article. Each
case of `feed_image_cases` goes through both, and the two answers are compared.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from pathlib import Path

import feed_image_cases
import feedparser
import pytest

from cyris.adapters.fetch.rss_source import _entry_image

pytestmark = [pytest.mark.unit, pytest.mark.js]

RSS_WORKER = Path(__file__).resolve().parents[1] / "workers" / "rss"

# Reads a JSON list of feeds on stdin and prints each one's first entry's image URL.
WORKER_IMAGES = """
import { parseFeed } from "./src/parse.js";
let input = "";
for await (const chunk of process.stdin) input += chunk;
console.log(JSON.stringify(JSON.parse(input).map((xml) => parseFeed(xml, "F")[0].image_url)));
"""


def _worker_images(feeds: list[str]) -> list[str | None]:
    if shutil.which("node") is None or not (RSS_WORKER / "node_modules").exists():
        assert not os.environ.get("CI"), "CI must provide node and the rss Worker's install"
        pytest.skip("node or workers/rss/node_modules missing")
    result = subprocess.run(
        ["node", "--input-type=module", "-e", WORKER_IMAGES],
        cwd=RSS_WORKER,
        input=json.dumps(feeds),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _direct_image(xml: str) -> str | None:
    (entry,) = feedparser.parse(io.BytesIO(xml.encode())).entries
    return _entry_image(entry, entry.link)


def test_both_parsers_give_each_entry_the_same_image() -> None:
    names = list(feed_image_cases.CASES)
    feeds = [feed_image_cases.feed(feed_image_cases.CASES[n][0]) for n in names]

    worker = dict(zip(names, _worker_images(feeds), strict=True))
    direct = {name: _direct_image(xml) for name, xml in zip(names, feeds, strict=True)}

    assert worker == direct
    assert worker == {name: wanted for name, (_, wanted) in feed_image_cases.CASES.items()}
