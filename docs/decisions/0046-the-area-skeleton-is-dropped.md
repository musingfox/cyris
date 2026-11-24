---
status: accepted
date: 2026-11-24
decision-makers: "the owner"
---

# The area skeleton is dropped, its tables with it

## Context and Problem Statement

Areas were the reader's closed set of topics, introduced by the milestone
[digest-by-topic-and-area.md](../milestones/digest-by-topic-and-area.md) rather than by an ADR. A
reader kept them on the Areas tab of `/settings`: the active names in order, the model's proposals
with their counts against a birth threshold, and each source's area. Scoring offered the active
names to the model, a run stamped each digest item with an area, and the mail, the plain-text mail
and Discord laid an issue out in one section per area (`layout_by_area`).

[ADR-0023](0023-votes-go-on-topics-and-the-digest-has-three-layers.md) replaced that layout with
the three layers, pebble, sand and water, decided by lane and member count alone. The page stopped
drawing areas then, and the mail and Discord followed, so by 2026-11-24 nothing drew an area:
`layout_by_area` had no caller, no run wrote news paragraphs, and an item's stamped area and the
issue's `area_order` were stored and never read. What was left still cost a model call per Suggest
and per Propose, a scoring clause on every run with an active area, a settings tab of five parts,
two tables, a key in every source's config, and a stub, `hidden_tabs`, that kept the tab off the
hosted page ([ADR-0043](0043-one-settings-page-an-edition-locks-a-key.md)). Whether to keep any of
it was undecided.

## Decision Drivers

* A reader should not tend a setting that changes nothing they see.
* The digest a reader gets must not change: the layers are decided without areas.
* Every edition shows one settings page (ADR-0043), so a tab hidden on one edition is a fork.
* A store reaches a new shape only through `schema.sql` and a versioned step
  ([ADR-0016](0016-version-table-schema-migrations.md)), on SQLite, D1 and every hosted tenant.

## Considered Options

* Delete the area skeleton, and drop its tables and keys through a versioned step.
* Delete the code and leave the tables and keys in place, unread.
* Keep areas as an organising aid on `/settings` only, with no effect on a run.

## Decision Outcome

Chosen: "Delete the area skeleton, and drop its tables and keys through a versioned step", because
the three layers replaced the area layout and nothing reads what the skeleton stores, so keeping
any part of it keeps a cost with no reader.

Removed: `domain/areas.py`, `domain/area_layout.py`, `service_layer/area_stamp.py`,
`service_layer/area_llm.py`, `adapters/store/areas.py`, `adapters/store/source_titles.py` and
`scripts/propose_rerun.py`; the scoring clause and its area picks; `DigestItem.area`,
`DigestContent.area_order`, `DigestContent.news_paragraphs` and `NewsParagraph`; the Areas tab, the
source editor's Area field and Suggest, every `/api/areas*` route and `/api/sources/suggest-area`;
and `hidden_tabs`, which ADR-0043 kept only until this. The step `0003_drop_areas` in
`adapters/store/migrations.py` removes each `sources` row's `area_id` key, then drops
`area_birth_threshold` and `areas`. Every statement is a no-op where there is nothing to remove, so
the step has no precondition, and one cut short is finished by the next boot.

### Consequences

* Good, because the scoring system prompt is again the one it was before areas, on every deployment.
* Good, because the settings page has the same tabs on every edition, with nothing hidden.
* Good, because a SQLite file, a D1 and each hosted tenant's object lose the tables on their next
  boot through `apply_schema`, with no step for anyone to run.
* Bad, because the drop is irreversible: a reader's area names, order, proposals, birth threshold
  and source assignments are deleted and kept nowhere. The owner accepted this on 2026-11-24.
* Bad, because a deployment that had active areas scores with a shorter prompt from its next run,
  which may move scores slightly; no output used the area picks.
* Bad, because an issue stored from 2026-10-27 to 10-29 holds its sand and water news only in
  `news_paragraphs`, which now loads as nothing: a `cyris regenerate` of such an issue, or
  `_republish_missing` publishing it, draws none of that news, not its articles, their links or
  their vote buttons. Pages already published are unaffected, and the issue's raw JSON stays in
  D1 `digests`. The owner accepted this on 2026-11-24 and chose no load-time compatibility for
  those issues, since only the owner uses cyris.
* Neutral, because every other issue stored while areas existed still holds `area_order` and item
  areas, which load and are dropped unread; its page draws as before.

### Confirmation

`tests/test_drop_areas_step.py` boots an old SQLite store holding both tables, a merged area that
names another and sources with `area_id`, with foreign keys on as D1 has them, and checks that the
tables are gone, every source row survives with its other keys, the step is recorded once, and a
step cut short at any statement is finished by the next boot. `tests/test_models.py` loads an issue
stored with the old fields.

## Pros and Cons of the Options

### Delete the area skeleton, and drop its tables and keys through a versioned step

* Good, because the schema a fresh store gets and the one an old store reaches are the same.
* Bad, because nothing can be restored once a store has booted.

### Delete the code and leave the tables and keys in place, unread

* Good, because it can be undone by restoring the code.
* Bad, because a fresh store and an old one would differ for good, which ADR-0016 forbids.
* Bad, because `reset_trial.py`, the backups and every tenant dump would keep carrying tables no
  code knows.

### Keep areas as an organising aid on `/settings` only

* Good, because a reader who sorted their sources keeps the sorting.
* Bad, because it keeps a tab, two tables and two model calls for a grouping nothing else reads.
