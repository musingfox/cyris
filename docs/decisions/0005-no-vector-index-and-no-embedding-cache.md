---
status: accepted
date: 2026-10-08
decision-makers: ""
---

# Embed every run, with no vector index and no embedding cache

## Context and Problem Statement

Vote similarity embeds article titles and compares each candidate with the titles the reader
voted on. Until milestone M4 (2026-08-27) the vectors were cached in a local `embeddings.json`
of 415 MB, rewritten whole every run. A Container has no persistent disk, so the cache could not
follow the pipeline. Cloudflare Vectorize was the destination the plan had named for it.

## Considered Options

* Move the vectors into Vectorize
* Move the cache to another persistent store
* Delete the cache and embed every run

## Decision Outcome

Chosen option: "delete the cache and embed every run", because the cache needed deleting, not a
new home.

Vectorize is an approximate-nearest-neighbour index, and the access pattern here is fetch by key.
`judge_by_votes` asks for one vector per title, and `domain/similarity.judge` does the cosine work
itself, because the margin rule (how far above the cutoff, up against down) is a domain rule and
the core does not move into an adapter. A vector database whose similarity search goes unused is
a new service, a new binding and a new failure mode carrying no payload.

The cache was optimising a cost that had stopped existing. `bge-m3` on Workers AI measured 7.59
neurons for 222 texts, so a full run of about 600 texts costs about 20 neurons against a
10,000-per-day free allowance. One observed run embedded 1,221 texts in 13 requests and 17.9 s
for 42 neurons. The cache also never extended a seed's life, despite what its docstring implied:
`vote_similarity._voted` reads seeds from store rows, so a deleted row takes its seed with it.

### Consequences

* Good, because no service, binding or §4 row exists for vectors.
* Good, because the provider can change by configuration with no stored vectors to invalidate.
* Bad, because every run pays the embedding calls and their wall time again. Production embeds
  with `gemini-embedding-001` since 2026-09-21 ([ADR-0006](0006-embedding-thresholds-are-per-model-calibrations.md)),
  about 300 titles a run, and that provider caches no more than `bge-m3` did.

## More Information

Extracted on 2026-10-08 from these sources at commit `c353d3f`:

* `docs/architecture.md:1040-1063` (*Why M4 did not use Vectorize*).
* `docs/architecture.md:158` and `docs/architecture.md:406` (the deleted cache's §2 and §4 rows).
* The module docstring at `src/cyris/adapters/embedding.py:11-17`, which states the same removal
  beside the code.
