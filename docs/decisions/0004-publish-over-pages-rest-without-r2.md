---
status: accepted
date: 2026-10-08
decision-makers: ""
---

# Publish over the Pages REST API, and keep only the archive's file list, in D1

## Context and Problem Statement

Until milestone M3 (2026-08-27) the digest was written to a local directory and published with
`wrangler pages deploy`. A Container has no persistent disk, so neither the directory nor the
subprocess could follow the pipeline into one. R2 was the destination the plan had named for the
HTML archive.

A Pages deployment is a full snapshot: a path missing from its manifest is deleted from the site.
Publishing therefore appeared to require holding the whole archive between runs.

## Considered Options

* Keep `wrangler pages deploy`, with node and wrangler in the image and the archive in a directory
* Keep the rendered archive in R2 and deploy from it
* Speak the Pages direct-upload protocol over REST, keeping only path → hash in D1

## Decision Outcome

Chosen option: "Pages direct upload over REST, with path → hash in D1 `pages_manifest`", because
the archive's bytes were never ours to keep.

The first REST deploy showed it: `check-missing` answered that 57 of 57 files were already in
Cloudflare's account-wide, content-addressed asset store. Only the list has to survive between
runs, and it is a few KB. For the rare asset Cloudflare ages out, the bytes come back from the
deployed site, which serves exactly what it was given; that was verified byte for byte against
`asset_hash` on 2026-08-27. The live site is the archive of record. The protocol was read off
wrangler's own `wrangler-dist/cli.js` rather than reconstructed from documentation.

### Consequences

* Good, because node and wrangler left the image, and the pages are rendered in memory and never
  reach a disk.
* Good, because nothing waits on R2. Every token in `.env` answered 403 on `/r2/buckets`, a missing
  `R2 → Edit` permission rather than a missing service.
* Bad, because the deployed site holds the only copy of what D1 `digests` does not: the raw
  companion pages and the issues published before 2026-09-24. D1 and Pages sit in one account, so
  an off-account backup is still open (`docs/architecture.md` §7 #14). If one is wanted, R2 is
  where it goes; that is a durability decision, not a publishing one.
* Bad, because losing D1 empties the manifest, and the next full-snapshot deploy would wipe every
  live page. The guards that refuse such a deploy, and the recovery path, are described in
  `docs/architecture.md` §7 under *What this costs, stated plainly*.
* Bad, because the asset key formula is load-bearing:
  `blake3(base64(bytes) + extension)`, hex, first 32 characters. Any other formulation makes
  `check-missing` answer "all new" and re-uploads the whole archive on every deploy.
* Neutral, because the account's upload token caps a deployment at 20,000 files, about thirteen
  years at four files a day. The path past it is pruning the archive tail, not a storage tier.
* Neutral, because a `json` deployment keeps the local-directory writer as its fallback.

## More Information

Extracted on 2026-10-08 from these sources at commit `c353d3f`:

* `docs/architecture.md:914-937` (*Publishing without a subprocess*, the protocol and its three
  load-bearing details).
* `docs/architecture.md:982-1002` (*Why M3 did not use R2 either*).
* `docs/architecture.md:1035-1038` (the *Ceiling* bullet).

The publish timing budget and how `_page_is_live` and Cloudflare's reported stage decide what
`pages_manifest` records are current behaviour, described in `docs/architecture.md` §7 beside
these sources.
