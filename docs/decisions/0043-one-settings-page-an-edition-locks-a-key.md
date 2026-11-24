---
status: accepted
date: 2026-11-24
decision-makers: ""
---

# One settings page for every edition; an edition locks a key

## Context and Problem Statement

Cyris had two settings pages. A local or cloud deployment's is TriageServer's `/settings`
(`settings.html.j2`, `static/settings.js`), which reads and writes one JSON contract, `GET
/api/settings` and its write routes. A hosted tenant's is a smaller page the hosted Worker renders
itself, with routes of its own over the tenant's Durable Object
([ADR-0040](0040-a-hosted-tenant-edits-its-settings-on-the-hosted-worker.md)), because a
TriageServer container per settings visit would take one of the slots the tenants' runs queue for.
ADR-0040 recorded the cost: a second page to keep worded and styled, so a key added to the hosted
list needs its control written twice.

The edition-parity plan's first milestone (M1, approved by the product owner on 2026-11-24) is to
prove the other way end to end: one settings page for every edition, where an edition locks the keys
its reader may not change, and the hosted Worker serves that page and its contract with no
container. ADR-0040's rejection of a container per visit stands. Open were how a Worker that runs no
Python serves a page Jinja renders, what the contract says about a lock and who enforces it, where
the hosted Worker's locks come from, and what M1 leaves as a stub.

## Decision Drivers

* No container per settings visit (ADR-0040).
* One page, one script, one contract: a key's control is written once, for every edition.
* What a reader may not decide holds whatever the page sends: a lock is the server's, and the page
  only shows it.
* A lock has exactly two reasons, `operator-set` and `plan-limit` (the product owner), each a UI
  catalog key (ADR-0027).
* The page is not sandboxed, as ADR-0040's is not, so its policy must still let no script in but
  its own.
* The hosted page ADR-0040 built keeps working, unchanged, until M3 replaces it.

## Considered Options

* A: render `settings.html.j2` at build time, once per catalog, into the Worker, with its `/static/`
  files
* B: a page skeleton that needs no server render, worded by its script from the browser catalog
* C: a TriageServer container per settings session (ADR-0040's option B)

## Decision Outcome

Chosen option: "A", because it serves the local page and its scripts byte for byte, keeps Jinja and
the catalogs the one source of the page, needs no container, and renders two pages a build.

**The contract.** `GET /api/settings` answers `locked`, each key the edition locks with its reason,
`{"<key>": {"reason": "operator-set" | "plan-limit"}}` (`LockReason` in
`entrypoints/triage_server.py`), and `hidden_tabs`, the tabs the edition does not show, a stub until
M2 deletes Areas. Every write route refuses a write naming a locked key with 400 `{ok: false, error,
field}`, before its own rule, probe or store call; the error is `settings_api.locked`, worded with
`lock_reason.<code>`. TriageServer takes its locks as `locked`; `cyris triage-ui` passes none, so
a local page answers `locked: {}` and `hidden_tabs: []` and has no locked control.

**The page.** `settings.js` disables each locked key's controls, with one line in the small role
below the help worded from `lock_reason.<code>`, which is each control's description, as a
dependency's line is (`docs/design/ui-language.md` §4 *field*). Keys of one field locked for one
reason share the line. A locked key is never marked missing or sent as shown, and nothing on the
page reopens it. A tab `hidden_tabs` names leaves the list and its hash opens Model. Writes name
`referrerPolicy: "same-origin"`: the hosted Worker serves every answer `Referrer-Policy:
no-referrer`, under which a browser may send a POST's Origin as `null`.

**The build.** `scripts/build_shared_settings.py` renders the page per catalog with
`render_settings_page`, and writes it, each `/static/` file the page loads and each module those
import, each catalog's browser messages as TriageServer serves them, the SHA-256 of each inline
script and style, and the two parts of the contract only the Python side answers (`sent_as_shown`
and `languages`), into `workers/app/src/shared_settings.build.json`, which is committed. A hosted
deploy, vitest and pytest all import the committed file and none rebuilds it, so no run depends on
another having built it first and a deploy needs no Python; `tests/test_build_shared_settings.py`
fails when the committed file differs from what the script builds now, naming the command that
rebuilds it.

**Where, on the hosted Worker.** STUB: the page lives at `GET /t/<tenant>/settings-next`, behind
the door `/t/` already has (ADR-0033). Its JSON and files are `GET /api/settings`, `POST
/api/settings/values`, `GET /api/sources`, `/api/topics`, `/api/areas` and `GET /static/<file>`,
the paths the page names, answered to a live session for the session's own tenant: no path, body or
header names a tenant, so they sit outside `/t/<tenant>/`, whose `api/settings` is ADR-0040's route
and takes another body. A write needs the request's own Origin (`sameOrigin`, ADR-0040). The
values are the tenant's rows read through `TenantDb.settings(keys)`, which never hands out a notify
target, with the provider, the model, the notify targets and the article cap put back as
`pin_tenant_settings` puts them back, and the schedule the registry holds. A value is saved through
`TenantDb.saveSettings`, which still refuses a key outside the policy's list.

**The locks, from the policy.** `hosted_settings_policy.json`'s `shared_page` names them: a grade-D
key no tenant writes (outside `writable_settings` and the schedule) is locked for
`operator_reason`. STUB: of the keys a tenant writes, the shared page opens only `open`,
`digest.output_language`, and locks every other for `stub_reason`; both reasons are `operator-set`
in M1. So the Model tab is real, its provider and model shown as the run is pinned to them, and
locked; only the output language saves. Sources and Tracking answer read-only and empty, and Areas
answers so and is hidden.

**The page's policy.** This supersedes ADR-0040's for the shared page: `default-src 'none'`,
`script-src` and `style-src` this Worker's `<origin>/static/` and the build's hash of each of the
page's inline blocks (its probe, its layout) and nothing else inline, styles also from Google
Fonts and fonts from its static host, `img-src 'self'`, `connect-src 'self'`, `form-action
'none'`, `base-uri 'none'`, `frame-ancestors 'none'`. The `/static/` files are served `private,
no-cache` with an ETag, as TriageServer serves `/static/`.

**The probe.** The shared page carries the pages' capability probe, as every edition's does. It
holds no vote token, so on the hosted Worker the probe is refused and the page's site bar shows its
brand alone, with no Archive, Settings or sign-out link, where ADR-0040's page linked all three.
ADR-0040's sentence that the probe answers `settings: false, settings_link: true` still holds for
the digest pages, whose Settings link opens `/t/<tenant>/settings` until M3 points it here.

**Two hour controls (amends ADR-0042).** ADR-0042 had the settings page offer one hour select
unless the tenant is allowed two. The shared page always shows the two hour controls it shows on
every edition, morning and evening; on the hosted Worker the second is locked for `plan-limit` while
the tenant may have one (`max_digest_times` 1), and its one hour is saved through the same
`checkSchedule`. In M1 the stub locks the schedule whole; the lock on one control comes when the
schedule leaves the stub, and the hosted page ADR-0040 built keeps its one select until M3.

### Consequences

* Good, because a settings visit still starts no container: the page and its files are in the
  Worker's bundle, and a read or a save is one or two Durable Object calls.
* Good, because the hosted Worker serves the local page and scripts as they are, so a key's control
  is written once; once M3 replaces the hosted page, ADR-0040's second page is gone.
* Good, because a lock is enforced by the server on every write route, and both servers answer a
  refused write in the same words: `workers/app/test/shared_settings_cases.json` holds the text.
* Good, because the page's policy admits no inline script but its two hashed blocks, and no script
  from anywhere but this Worker's `/static/`.
* Bad, because a change to the template, a `/static/` script or a catalog must be committed with the
  rebuilt file, which the repository carries, as the bundle does, at about 240 KB; the gate's
  staleness test is what keeps the two together.
* Bad, because parts of TriageServer's `GET /api/settings` (what is missing, the starting values,
  the labels, a save's note) are worked out again in the Worker; the shared cases hold the two to
  one answer.
* Bad, because in M1 a tenant sees a page almost wholly locked, its Sources and Tracking empty and
  read-only, worded as a deployment with no writable table is, and no link back to its pages.
* Neutral, because a tenant has two settings pages until M3, the old one still the one its digest
  pages link.

### Confirmation

* `tests/test_settings_locks.py`: a server `cyris triage-ui` starts locks nothing and hides no tab;
  a locked key is named with its reason; each write route refuses it and stores nothing; a lock on
  one key leaves the others writable; a lock names a setting and one of the two reasons.
* `tests/test_settings_fields.py` holds `settings.js` to disabling each locked key's controls with
  its reason as their description and never marking it missing; `scripts/settings_probe.py`'s
  `model-locked-with-its-reason`, `locked-key-not-marked-missing` and
  `hidden-tab-not-listed-nor-opened` check it in Chromium, each with a sabotage that rewrites the
  answer after the check's own preload so its check fails: the model unlocked, no key locked, no
  tab hidden (`--self-test`).
* `tests/test_build_shared_settings.py`: each catalog's page is `render_settings_page`'s, each inline
  block's hash is named and no other, each file is what a bare TriageServer serves at its path,
  `sent_as_shown` and `languages` are TriageServer's, and the committed file is what the script
  builds now.
* `workers/app/test/hosted_shared_settings.test.js` drives `hosted.js` over the real registry and
  tenant objects: the page in the tenant's catalog, its policy against the hashes of its own inline
  blocks, the session check, the old page unchanged, every `/static/` file the pages load, their
  ETag, the contract's shape and locks, the pinned provider and model, no notify target read out,
  the writes, the tenant written, and the empty tabs.
* `workers/app/test/shared_settings_cases.json` is run by that suite and by
  `tests/test_shared_settings_cases.py`, against a TriageServer given the hosted locks: the answer's
  shape, `locked` on both editions, each write's status and exact answer, a refused write leaving
  the store as it was, and the pins, held to `pin_tenant_settings`.
* `tests/test_hosted_settings_policy.py` holds `shared_page` to opening only keys a tenant writes,
  for the two reasons, and hiding only tabs the page has.
* Unverified here: the page in a browser on the hosted Worker, and its save's Origin. The trial check
  is `docs/hosted-tenants.md` step 10, *The shared settings page*.

## Pros and Cons of the Options

### A: render at build time

* Good, because the page, its scripts and its words are the local edition's, byte for byte.
* Good, because the page's inline blocks are known at build time, so the policy names their hashes.
* Bad, because the build needs Python, and a change to the template, a script or a catalog reaches
  the hosted Worker only with its next deploy, as any change to the Worker does.

### B: a page skeleton that needs no server render

* Good, because the Worker would serve one static file and the build would need no Python.
* Bad, because every word the template renders, about 120 `t()` calls, would move into the browser
  catalog and be set by script, and the page would show nothing until its catalog loads.
* Bad, because the local page would have to become that skeleton too, or the two would part again:
  a rewrite of the page rather than its reuse.

### C: a TriageServer container per settings session

* Bad, for ADR-0040's reasons: each visit takes a slot the runs queue for and waits a cold start,
  and TriageServer's model-calling routes would run unmetered.

## More Information

* Supersedes in part [ADR-0040](0040-a-hosted-tenant-edits-its-settings-on-the-hosted-worker.md):
  its page, that page's policy and its probe sentence, for the shared page, as above. ADR-0040's
  routes, its policy file's writable keys, its CSRF rule, its sources and the poll's hold stand.
* Amends [ADR-0042](0042-a-hosted-tenant-makes-one-digest-a-day-unless-allowed-two.md) lines 68-69:
  the settings page always shows two hour controls, the second locked for `plan-limit` while the
  tenant may have one.
* Not in this decision: deleting Areas (M2, which ends `hidden_tabs`); replacing the hosted page,
  pointing the digest pages' Settings link here, opening the other keys a tenant writes, and
  Sources (M3).
* `docs/architecture.md` §4 has the routes' reads and writes of the tenant's `settings`, §5 the
  `shared_page` policy and the build, §6 the routes; `docs/hosted-tenants.md` step 10 has the trial
  check.
