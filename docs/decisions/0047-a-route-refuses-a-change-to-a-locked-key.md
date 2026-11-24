---
status: accepted
date: 2026-11-24
decision-makers: "the owner"
---

# A write route refuses a change to a locked key, and an edition names its limits

## Context and Problem Statement

[ADR-0043](0043-one-settings-page-an-edition-locks-a-key.md) had every write route refuse a write
naming a locked key. `POST /api/settings/values` takes any set of keys, so it refused only the ones
a save named and left the rest writable. The other routes each save several keys from one body:
the model route a provider and a model, vote similarity four keys, mail a recipient and a sender.
TriageServer refused such a route whole when any one of its keys was locked, so a lock on the
embedder also took away vote similarity's switch, and a lock on the sender took away the recipient.
The page sends a route's every key, the locked ones as they are held.

The hosted shared page also had to save the schedule and the article cap the old hosted page saves
(milestone M3 of the edition-parity plan), and a hosted tenant's most digest times and article cap
are not a self-hosted server's. The page knew neither.

## Decision Drivers

* A lock on one key leaves the others writable, on every route alike.
* What a reader may not decide still holds whatever the page sends (ADR-0043).
* An edition's limits are its server's, not the page's: one page serves both editions.

## Considered Options

* A route refuses a write that changes a locked key's value; a locked key sent as it is held is no
  change.
* Split each multi-key route so that every key has a route of its own.
* Keep refusing the whole route, and leave the page to disable every control of a route with a
  locked key.

## Decision Outcome

Chosen: the first, because it keeps the routes and their probes as they are and gives every route
the rule `/api/settings/values` already keeps. TriageServer's `_refuse_locked_changes` compares
the value a route would store for each key it saves with the value the server holds, before any
probe or store call: the model route decides first that no provider stores no model, so choosing
`none` cannot clear a locked model, and every other route stores what it compares, or the same
value its rule reads it as;
the hosted Worker keeps the same rule on the one multi-key route it answers,
`POST /api/settings/schedule` (`changedLock` in `workers/app/src/hosted_shared_settings.js`).
`/api/settings/values` still refuses any locked key a save names, changed or not: the page never
sends one there.

`GET /api/settings` answers `limits`, additively: `digest_times_min` and `digest_times_max`, the
fewest and the most digest times a day the reader's schedule holds, and
`max_articles_per_digest_max`, the article cap's most or `null` for none. TriageServer answers its
own, exactly two times and no cap; the hosted Worker answers one time at least, the tenant's
registry maximum at most, and its policy's cap. The page shows the hours as two selects and
disables each one past `digest_times_max`, with the `plan-limit` line a locked key carries, and
never sends it; each one past `digest_times_min` offers "No second digest", which is not sent, so a
tenant allowed two may keep one, as the hosted page lets it; the article cap's input takes the most
as its `max`. That is the control-level lock ADR-0043's *Two
hour controls* deferred until the schedule left its stub. It is not a key lock: the schedule stays
open, and `locked` does not name it.

### Consequences

* Good, because a lock is as narrow as the key it names on every route, and the shared cases hold
  both servers to it (`route_locks` in `workers/app/test/shared_settings_cases.json`).
* Good, because the hosted page's caps reach the shared page from the server, and a self-hosted
  page shows no cap it does not have.
* Bad, because a value spelled differently from the one held (`8:00` for `08:00`) counts as a
  change, so a locked key sent so is refused though it would store the same value.
* Neutral, because `limits` is additive: a client that ignores it is unchanged.

### Confirmation

`tests/test_shared_settings_cases.py` and `workers/app/test/hosted_shared_settings.test.js` run the
shared cases' `route_locks`, `schedules` and `editions.*.limits`; `scripts/settings_probe.py`'s
`second-hour-locked-by-plan`, `second-hour-left-as-no-second-digest`,
`second-hour-dropped-to-no-second-digest`, `hours-are-whole-hour-selects` and
`article-cap-takes-the-editions-most` check the page in Chromium, each with its sabotage.

## More Information

Amends ADR-0043's contract sentence "every write route refuses a write naming a locked key" to a
write changing one, and settles its *Two hour controls* lock on the second hour.
