"""A vote card's URL is the URL the first run reads for the same article.

The cards are built by `bootstrap.build_onboarding_articles`, the run's fetch
by `bootstrap.build_deps`. Both are fed one feed's content here — a feed the
reader polls directly, and the rss Worker's buffer rows — and the URLs that come
out of each must be the same set. A vote lands on a stored row by URL, so a card
URL the run spells differently is a vote the first issue never sees.
"""

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
import respx
from fakes import TEST_SETTINGS, load_files

from cyris.adapters.fetch.onboarding_cards import draw_cards
from cyris.bootstrap import build_deps, build_onboarding_articles
from cyris.service_layer.fetching import fetch_all_articles

pytestmark = pytest.mark.integration

NOW = datetime.now(UTC).replace(microsecond=0)
FEED_URL = "https://feed.test/rss"
WORKER = "https://rss.test"

CONFIG = """
[agent_vault]
path = "{vault}"
{rss}
"""

SOURCES = f"""
sources:
  - name: "Feed"
    url: "{FEED_URL}"
"""

# Links as a feed really writes them: tracking parameters, a fragment, a query
# that is the page's own, so a normalisation either path skipped would show.
LINKS = [
    "https://feed.test/a?utm_source=rss&utm_medium=feed",
    "https://feed.test/b?id=2&utm_campaign=x",
    "https://feed.test/c#comments",
    "https://feed.test/d",
    "https://feed.test/e?fbclid=abc",
    "https://feed.test/f",
]


def _feed() -> bytes:
    items = "".join(
        f"<item><title>Item {i}</title><link>{link.replace('&', '&amp;')}</link>"
        f"<guid>g{i}</guid><pubDate>{format_datetime(NOW - timedelta(days=i + 1))}</pubDate>"
        "<description>body</description></item>"
        for i, link in enumerate(LINKS)
    )
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title>'
        f"<link>https://feed.test/</link>{items}</channel></rss>"
    ).encode()


def _rows() -> list[dict]:
    """The Worker's `GET /articles` rows for the same entries, URL as its parser stored it."""
    return [
        {
            "url": link,
            "guid": f"g{i}",
            "title": f"Item {i}",
            "content": "body",
            "author": None,
            "published_at": (NOW - timedelta(days=i + 1)).isoformat().replace("+00:00", "Z"),
            "source_name": "Feed",
        }
        for i, link in enumerate(LINKS)
    ]


def _config(tmp_path, monkeypatch, rss: str):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    config_path = tmp_path / "cyris.toml"
    config_path.write_text(CONFIG.format(vault=tmp_path, rss=rss), "utf-8")
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text(SOURCES, encoding="utf-8")
    return load_files(config_path, sources_path, TEST_SETTINGS)


async def _both(cfg) -> tuple[set[str], set[str], set[str]]:
    """The run's URLs, the card reader's, and the drawn cards', over one window."""
    after = NOW - timedelta(days=8)
    run, _failed = await fetch_all_articles(build_deps(cfg).fetch_sources, after, NOW, cfg.sources)
    cards = build_onboarding_articles(cfg)
    read = await cards.refresh(cfg.sources, lambda: NOW)
    drawn = draw_cards(read.articles, cfg.sources).cards
    return {a.url for a in run}, {a.url for a in read.articles}, {a.url for a in drawn}


@respx.mock
async def test_a_directly_polled_feed_gives_the_run_and_the_cards_one_url_set(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("CYRIS_WORKER_TOKEN", raising=False)
    cfg = _config(tmp_path, monkeypatch, "")
    feed = respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=_feed()))

    run, read, drawn = await _both(cfg)

    # One fetch each: the run's, and the cards' own, of the same feed content.
    assert feed.call_count == 2
    assert run and read == run
    assert drawn and drawn <= run
    # The strip both apply, seen: no tracking parameter reaches either side.
    assert not any("utm_" in url or "fbclid" in url for url in run | read)


@respx.mock
async def test_the_workers_buffer_gives_the_run_and_the_cards_one_url_set(tmp_path, monkeypatch):
    monkeypatch.setenv("CYRIS_WORKER_TOKEN", "tok")
    cfg = _config(tmp_path, monkeypatch, f'\n[rss]\nworker_url = "{WORKER}"\n')
    poll = respx.post(f"{WORKER}/poll").mock(return_value=httpx.Response(200, json={"feeds": 1}))
    rows = respx.get(f"{WORKER}/articles").mock(return_value=httpx.Response(200, json=_rows()))

    run, read, drawn = await _both(cfg)

    assert rows.call_count == 2
    assert run and read == run == set(LINKS)
    assert drawn and drawn <= run
    # The cards fill the buffer before they read it; the run only reads.
    assert poll.call_count == 1
