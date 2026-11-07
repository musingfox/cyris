---
status: accepted
date: 2026-11-07
decision-makers: ""
---

# A local install keeps its state in a local SQLite file behind the D1 interface

## Context and Problem Statement

`docs/architecture.md` §2 says "Local filesystem is a defect, not a tier", and treats the `json`
backend as the one documented fallback. That was written for a system moving to the cloud. The
project now has a local edition that readers are meant to run without Cloudflare, and the owner
expects every feature to work on it.

The `json` backend cannot carry that. `/settings` cannot save on it, votes have no home that two
processes can share, and the features that read D1 tables answer 409 or nothing. In the feasibility
test, the `json` store left 89 of 150 rows and lost all 50 votes under concurrent writers.

The question is what a local install stores its state in, and whether the architecture's stance
still holds.

## Decision Drivers

* Every feature should work locally without a line of `service_layer/` or `domain/` changing.
* A local install's server and its scheduled runs write at the same time.
* Less code to keep: each feature ported to files is a second implementation of something D1 has.
* The project has not been announced, so nearly the only local install is the owner's. This is the
  cheapest time to change the base.

## Considered Options

* A local SQLite file behind the existing D1 interface
* `json` plus a vote queue and new file stores for settings, sources and tracked topics
* A local install talks to real Cloudflare D1
* Keep `json` and accept the missing features

## Decision Outcome

Chosen option: "a local SQLite file behind the existing D1 interface", because D1 is SQLite: the
prototype opened every settings form, sources, tracked topics and votes with `service_layer/` and
`domain/` unchanged, while the file option needs a new store for each and still loses writes.

* `[store] backend` gains a local SQLite value, and it becomes the local default. ADR-0003 holds: it
  is a third alternative home, never overlaid on another. Grade-D settings and sources live in the
  file, not in `cyris.toml` or `sources.yaml`.
* `is_d1` means both "speaks SQL" and "is Cloudflare" today. It is split, so the store tables are
  shared and only Cloudflare's edges stay behind the Cloudflare check.
* A compose install keeps the database in a named volume, and every command runs in a container. A
  `uv` install keeps it on the host, and every command runs on the host. One file is never opened
  from both sides. Generated pages stay plain files in `agent-vault/html/`.
* The architecture's stance changes. A local file is a supported home for a local install. Cloud
  deployments still write nothing locally.
* `json` is removed before the project is announced, once `cyris store migrate` moves a `json`
  install across, `topics.json`, `usage.jsonl` and `[[tracked_topics]]` included. A public release
  then never has two local stores to choose between.
* The database holds recent state, not everything forever. The owner's direction for every edition
  is that old data rotates out to compressed files: on disk locally, and in object storage in the
  cloud, where the container has no disk. It is separate work with its own decision on what rotates
  and when, because the volume has not been measured. Rows with a human verdict stay in the database,
  because vote similarity reads them as seeds (`service_layer/vote_similarity.py:83`).

### Consequences

* Good, because settings, the wizard's saves, votes, holds, run records, regenerating old issues,
  domains and the interest map stop being separate porting jobs. The gap inventory counts 14 of its
  29 gaps as solved or nearly so.
* Good, because about 700 lines of `json` store go (`article_store.py` 580, `json_topics.py` 124).
* Good, because two writers work: 124974 votes across OrbStack and Linux named volumes, with 48 hard
  kills on each, gave 0 wrong states, 0 lost rows and 0 lock errors, and `integrity_check` passed.
* Bad, because a macOS host process and a container on one bind-mounted file corrupt it, even when
  the host only reads. The rule above is the only guard, and a reader who breaks it loses data.
* Bad, because a reader can no longer read or hand-edit state as plain files. `cp` of the one file
  can silently lose recent writes, so backup goes through `.backup` or `VACUUM INTO`.
* Bad, because ADR-0003's first-boot cost reaches local installs: `ingest` and `run` stop until the
  wizard or `cyris settings push` has filled the settings.
* Bad, because no path to Cloudflare D1 exists yet. `.dump` produced a 186889-byte line against
  D1's 100 KB statement limit, so moving to the cloud edition needs a copy tool, not tested yet.
* Bad, because the wizard's last step, `POST /run`, exists only in the Worker and needs a local
  equivalent.
* Neutral, because the local server still has no sign-in. Its Host and Origin check stops other
  pages in the reader's browser, and it answers only on loopback. Phone access is a separate decision.

### Confirmation

A new install finishes the wizard, saves its settings and sources, and produces its first issue with
no file edited by hand. `cyris doctor` names the store it opened. Before this is accepted as
implemented, the concurrency and hard-kill test is rerun against the shipped client rather than the
prototype's.

Not verified: a real power cut, Docker Desktop, Windows, and real D1 as the destination of a move.

## Pros and Cons of the Options

### `json` plus a vote queue and new file stores

* Good, because state stays in files a person can read and edit.
* Bad, because it adds three file stores, a queue adapter and two dependencies, and every other D1
  feature still needs its own port.
* Bad, because of the tested designs only one file per vote with a rename kept all votes in every
  setup, and draining cannot run alongside `ingest` or `run`.

### A local install talks to real Cloudflare D1

* Good, because no new store or migration is needed.
* Bad, because it ties the local edition to a Cloudflare account, which that edition exists to avoid.

### Keep `json` and accept the missing features

* Good, because it costs nothing now.
* Bad, because it contradicts the owner's expectation that every feature works locally, and the
  `json` store still loses concurrent writes.

## More Information

Amends the scope of [ADR-0003](0003-one-home-per-backend.md), which names two backends, without
changing its rule. Extends [ADR-0020](0020-new-deployments-start-from-onboarding.md)'s wizard to a
local install, whose `POST /run` the local server answers under the same date rule. `docs/architecture.md` §2 *Local filesystem is a defect, not a tier*, §4 and §5
change in the same work, and `CLAUDE.md`'s project overview names `json` as the local path.
