---
status: accepted
date: 2026-10-08
decision-makers: ""
---

# The reader's type size is injected by the Worker, and it is the only live reader preference

## Context and Problem Statement

A reader wanted to adjust the type size of the digest. Published issues are static files on
Pages ([ADR-0010](0010-the-digest-stays-static-behind-the-app-worker.md)) and are recovered byte
for byte, so a size baked in at render time would leave every earlier issue at the old size. On
2026-09-20/21 the wider question was also asked: which reader-facing preferences should be live
settings at all?

## Considered Options

* Bake the size into each page at render time
* Inject the size into every HTML page the app Worker serves, read from D1
* Open further live preferences: a theme override, or a reorderable section list

## Decision Outcome

Chosen option: "inject the size in the Worker, and stop live preferences at the type size",
because injection reaches issues already published, and the type size is the one token axis
with a grade-D setting, an injection path and a closed allowlist.

* `digest.type_scale` is one site-wide grade-D key with three values: 0.875, 1 and 1.125.
  `workers/app/src/type_scale.js` reads it from D1 over REST with the token the container already
  holds, with no binding, for the reason in
  [ADR-0012](0012-the-app-deploys-from-the-repo-root.md).
* D1 reads share the account's REST limit, so the Worker holds one read per isolate for 60 s,
  failures included, and concurrent page views share the read in flight. That bounds the added
  calls at five per isolate per five minutes plus one per `/settings` save, whatever the traffic.
* At 1 the Worker changes nothing, and pages pass through byte for byte with their validators. At
  any other value it appends `<style>html:root{--type-scale:X}</style>` to `<head>` and drops
  `ETag`, `Last-Modified` and the conditional request headers, so no browser revalidates a page
  into its old size. A failed, slow or invalid read means 1, because a page view must never fail
  on a setting.
* A theme override was not opened. The three sanctioned colour literals are hand-derived from
  tokens, so overriding `--accent` would leave the brand glow, the site bar's background and the
  primary button's hover behind, and the colour-literal test would still pass. The Google Fonts URL
  is literal in every template, so a font-token override changes no webfont.
* A reorderable section list was not opened. Section identity is a pydantic field name in four
  layers at once, so reordering is a model change, not a rename.

### Consequences

* Good, because every issue, including those already published, follows the reader's setting.
* Bad, because issues published before 2026-09-18 set literal font sizes and do not change, and
  a page opened on `pages.dev` directly, or the mail, never passes the Worker and stays at 1.
* Bad, because a new reader preference would need its own grade, writer and delivery path, not
  only a field on `/settings`.

## More Information

Extracted on 2026-10-08 from these sources at commit `c353d3f`:

* `docs/architecture.md:1188-1203` (*Every HTML page the Worker serves carries the reader's type
  size*).
* `docs/architecture.md:1211` (§7 row #35).
* `docs/hosting-and-cost.md:120-129` (§6, *UI theme customization* and *Structural UI
  customization*) and `docs/hosting-and-cost.md:144-145` (§7, *Reader-facing live preferences
  stop at the type size*).
