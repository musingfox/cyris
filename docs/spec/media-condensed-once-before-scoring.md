---
id: media-condensed-once-before-scoring
status: proposed
scope:
  - "src/cyris/service_layer/run_digest.py"
  - "src/cyris/service_layer/ports.py"
  - "src/cyris/adapters/fetch/*"
  - "workers/rss/src/*.js"
verify: null
related: [media-condensation-falls-back-to-announcement]
source: multimedia-sources-podcast-youtube
adr: null
---

Summarize-tier media are condensed once, inside the digest run, on the articles
that run newly saved — after the store's dedup and before scoring — never in a
`FetchSource`, never in a Worker, never after scoring.

The stage sits in `run_digest` between `store.save` and `select_scorable`,
behind a Protocol in `service_layer/ports.py`, and writes the condensation over
the article's stored content so the scorer and the summarizer both read it.
"Newly saved by this run" is the only once-marker; nothing records that a row
was condensed.

Out of scope: filter- and fan-tier media, which are never condensed; a dry run,
which saves nothing and so condenses nothing without a special case; retrying a
failed condensation, which would need a marker this design does not keep.

A violation is silent because a misplaced condenser still yields a good digest.
The window is 24 hours and runs come twice a day, so every item is fetched by
two runs: condensing at fetch time pays twice for every episode, condensing
after scoring ranks episodes on their show notes, and condensing in the rss
Worker moves the credential and the spend out of `usage_log`. Each shows up
only as a larger bill or a slightly worse ranking.

Born prose: the stage does not exist yet. The binding is a test that runs
`run_digest` twice over one window with a recording fake condenser and asserts
one call per new media item, each made before the scorer's first call.
