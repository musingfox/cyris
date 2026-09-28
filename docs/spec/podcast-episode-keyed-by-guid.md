---
id: podcast-episode-keyed-by-guid
status: proposed
scope:
  - "workers/rss/src/*.js"
  - "src/cyris/adapters/fetch/rss_source.py"
  - "src/cyris/adapters/fetch/rss_worker_source.py"
verify: null
related: [rss-parsers-agree]
source: multimedia-sources-podcast-youtube
adr: null
---

A feed item carrying an audio `<enclosure>` is stored under the key
`podcast:{source name}:{guid}`, never under its `<link>` or its enclosure URL,
and that format does not change once an episode has been published.

The key is minted at parse time, in `parseFeed` and in `RssSource`: the buffer
writes with `INSERT OR IGNORE` on `url`, so downstream re-keying cannot recover
a dropped row. The episode page goes in `ref_urls`. Detection is per item, and
the source stays `type: rss`, the only type either fetch path polls.

Out of scope: YouTube videos, keyed by their watch URL, and items without an
enclosure, which keep the link. A source renamed on `/settings` is a new source
and re-keys what is still in the 8-day buffer; that single repeat is accepted.

A violation is silent in both directions. Keyed by link, a show whose items all
point at its homepage — Hard Fork and a Captivate show, measured 2026-09-28 —
stores its first episode and ignores every later one while the source still
looks alive. Changed after launch, the format orphans the keys already written
into every archived page's vote buttons (`data-urls`), and a show's votes and
tags split across two keys with no error anywhere.

Born prose: the key does not exist yet. The binding is a test that parses
recorded feeds with no item links and with links repeating the channel's, and
asserts one key per episode in this format; it becomes `verify` when the
parsers mint the key.
