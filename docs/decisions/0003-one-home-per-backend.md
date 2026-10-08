---
status: accepted
date: 2026-10-08
decision-makers: ""
---

# One home per backend: `[store] backend` picks where articles, settings and sources live

## Context and Problem Statement

Cyris has two storage backends, the `json` files and Cloudflare D1. Twice, state that could come
from two places came from the wrong one without anything failing.

* From 2026-08-25 to 2026-08-27 the container ran a stale image while the host ran the current
  one, so both backends took writes. The article store split in both directions.
  `cyris store migrate` uses `INSERT OR IGNORE`, which adds missing rows but never overwrites, so
  it could not heal a two-way split. A human triage decision recorded on the losing side was lost
  unless someone restored it by hand.
* Until 2026-09-19 `bootstrap.load_effective_config` loaded `cyris.toml`, overlaid D1 `settings`
  on it, and took a value from code for any key missing from both. A running value could
  therefore come from three places, and a stale one could decide a run unseen.

The question was how many places one kind of state may live in, for one deployment.

## Considered Options

* Overlay: file, then D1, then a default in code
* D1 first, with the file as a fallback when D1 is empty or cannot be read
* One home per backend, with nothing overlaid

## Decision Outcome

Chosen option: "one home per backend", because every alternative leaves a second source that can
quietly win.

* A `d1` deployment reads articles, grade-D settings and sources from D1 alone. A `json`
  deployment reads the JSON store, `cyris.toml` and `sources.yaml` alone. The two article stores
  are alternatives, never a pair.
* No grade-D value lives in code, and the environment supplies none. A missing key is an error
  where a command reads it, and the commands that fill an empty D1 still start.
* A D1 read error propagates. Falling back to the file on error would bring back the divergence
  the rule exists to prevent: one run on D1's values, the next on the file's.
* An invalid D1 row counts as missing, because D1 is fixed through `/settings`, which has to
  start. An invalid `cyris.toml` value fails the load, because the file is fixed in an editor.

### Consequences

* Good, because `cyris doctor` reports what is missing, not which home won, since only one can.
* Good, because a first boot stops every run and names each missing key, instead of publishing
  with values nobody chose.
* Bad, because a fresh D1 deployment cannot run until `/settings` or `cyris settings push` has
  filled every grade-D key and the `sources` table holds a source.
* Bad, because a cutover between backends is a deliberate copy (`cyris store migrate`,
  `cyris settings push`, `cyris sources push`), with `cyris store diff` run before and after it.

### Confirmation

`cyris doctor` fails its `settings` check on each missing key, fails `sources` on an empty table,
and on a `d1` deployment fails `file settings` when `cyris.toml` still sets a grade-D key.

## More Information

Extracted on 2026-10-08 from these sources at commit `c353d3f`:

* `docs/architecture.md:419-426` (*One store, one truth*).
* `docs/architecture.md:589-615` (*Where grade D lives*, the rules).
* `docs/architecture.md:1250-1253` (*`sources.yaml` is the `json` backend's list only*).
* `docs/architecture.md:858` (M2's row: why the read order mattered).
