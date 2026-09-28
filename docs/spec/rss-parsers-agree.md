---
id: rss-parsers-agree
status: proposed
scope:
  - "workers/rss/src/*.js"
  - "src/cyris/adapters/fetch/rss_source.py"
  - "src/cyris/adapters/fetch/rss_worker_source.py"
verify: null
related: [podcast-episode-keyed-by-guid]
source: multimedia-sources-podcast-youtube
adr: null
---

A feed item must become the same row whichever path reads it: the rss Worker's
`parseFeed` and Python's `RssSource` agree on its key, link, content and
transcript pointer.

`parseFeed` feeds production through the D1 buffer that `CloudflareRssSource`
reads; `RssSource`, over feedparser, is the no-Worker fallback. Every rule that
decides what an item is — the episode key, the Shorts filter, which field holds
a video's description — exists in both, and a rule that is data is data both
read. The precedent is `TRACKING_KEYS`, mirrored in both parsers and held
together by `tests/test_email_parser.py`.

Out of scope: operational fields only one path has, such as the buffer's
`fetched_at`. A deployment that runs one path still needs the other to agree,
because `cyris store migrate` carries rows from the `json` store into D1.

A violation is silent because each path is consistent with itself. On
2026-09-28 feedparser read 2,397 characters of `media:description` from a
YouTube feed while `parseFeed` read none, so a local install showed a
description that production left empty; neither path could tell.

The binding is a test that runs the same recorded feeds through both parsers and
compares the rows. A check written today would be red, so the test lands with
the parser change that makes the two agree, and becomes `verify` then.
