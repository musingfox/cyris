---
status: accepted
date: 2026-11-22
decision-makers: ""
---

# A hosted tenant edits its settings and sources on a page the hosted Worker renders

## Context and Problem Statement

A hosted tenant's settings and sources were the operator's alone: the seed wrote them
(`hosted_seed.js`), the schedule route set the hours in the registry (ADR-0032), and the
newsletter-source route added a sender (ADR-0034). A signed-in reader had no Settings page at all:
the link on a tenant's page was an absolute `/settings`, which on the hosted Worker is an operator
route and answers a reader 403, so the probe hid it (ADR-0039).

The card is to let the tenant's signed-in reader change its own settings and its RSS sources, and
have its next run use them. Three things were open: where the page and its routes run, which keys a
tenant may write and how a value is checked, and what keeps a tenant-named feed URL from reaching
something it should not, at the save and at every poll.

## Decision Drivers

* The hosted deployment has two container slots (`max_instances` in `wrangler.hosted.toml`), each
  start costs up to 28 s (`container.js`), and they are what the tenants' runs queue for
  (ADR-0032). A settings visit must not take one.
* A tenant page's scripts run sandboxed with no origin (ADR-0033), so a script injected into a
  digest reaches no cookie and no route. The settings page must not weaken that for the digest
  pages.
* What the tenant may not decide (the model, the notify targets, the run cap, the AI allowance)
  must hold whatever the tenant's database says: the run container can write any of its own rows
  through its gateway (ADR-0028).
* One rule per value: a value the page takes must be one the run takes.
* A tenant now names URLs the hosted Worker fetches every hour, from inside Cloudflare's network.

## Considered Options

* A: the hosted Worker renders its own small settings page and JSON routes over the tenant's
  Durable Object
* B: start a TriageServer container per settings session and proxy the self-hosted `/settings`
* C: the run renders a settings page into the tenant's site, beside its digest pages

## Decision Outcome

Chosen option: "A", because it needs no container, keeps every digest page sandboxed, and is the
only option whose routes B and C would each need anyway.

**Where.** `GET /t/<tenant>/settings`, `GET|POST /t/<tenant>/api/settings`, `GET|POST
/t/<tenant>/api/sources` and `POST /t/<tenant>/api/sources/delete` (`hosted_settings.js`) answer a
live session whose tenant is the path's, behind the door `/t/` already had (a fixed 404 for any
other tenant, `/login` for none). The tenant written is always the session's: no body field, query
or header names one. Each route calls the tenant's own object through narrow methods,
`TenantDb.settings`, `saveSettings`, `sources`, `saveSource` and `deleteSource`, never `query`.
The self-hosted `/settings` (TriageServer, `settings.js`, `settings.html.j2`) does not change.

**What a tenant writes.** `workers/app/src/hosted_settings_policy.json` lists the eight keys a
tenant writes: `digest.output_language`, `digest.style_prompt`, `digest.type_scale`,
`digest.max_articles_per_digest`, `general.digest_window_hours`, `digest.rank_by_preference`,
`digest.summarize_follow` and `routing.score_threshold`. Each is held to the bounds config.py puts
on the field, taken as JSON's own types (a number, a boolean), and the article cap to 1..400; a
save with any other key is refused whole, 400, with a catalog message saying the key is the
operator's or no setting at all. `TenantDb.saveSettings` refuses a key outside the list itself, so
no caller of the object writes the provider through it. The digest schedule, two whole hours and
an IANA zone, is written only through the registry's `setSchedule`, naming no run cap so the
operator's is kept, and then copied into the tenant's settings as the operator's route copies it.
`workers/app/test/hosted_settings_cases.json` is run by vitest and by pytest, which holds each
accepted value valid by `validate_setting` and the policy cap, and each refused one invalid or not
a tenant's key.

**What the run takes regardless.** The run reads the same policy file (the image copies it beside
`src/`) and, as its settings load, pins the provider and model to the policy's (`anthropic`,
`claude-haiku-5-5`), blanks `notify.discord_webhook_url`, `notify.email_from` and
`notify.email_to`, and clamps the article cap to 400 (`pin_tenant_settings`,
`adapters/hosted.py`). The run cap and the AI allowance live in the registry, which no tenant
route writes.

**Sources.** A source a tenant adds is RSS: a name, a URL and a lane. The URL must be http or
https, on no port or 80 or 443, with no user name or password, naming a domain and not an IP
literal (in any spelling the URL parser reads as one), not `localhost` or a `.localhost`, `.local`,
`.internal` or `.arpa` name, not the hosted Worker's own host and not any `workers.dev` name, and
at most 2048 characters. A tenant holds at most 30 sources, RSS and newsletter counted together;
the count and the write are one synchronous turn of the tenant's object (`saveSource`), so two
saves at once cannot both take the last place. A source of either type may be removed. The URL is
not fetched at the save.

**At every poll.** The same check holds each hop of each feed: the hosted poll fetches with
`redirect: "manual"`, follows at most 5 redirects, checks every hop's URL before asking it, reads a
body only to 5 MiB (`feed_check.py`'s `MAX_BYTES`), and polls at most 30 feeds per tenant, the
rest skipped and logged. This is `pollFeeds`' `held` option in `workers/rss/src/buffer.js`, which
only the hosted poll passes; the self-hosted rss Worker fetches as before.

**CSRF.** Every POST is refused 403 unless its `Origin` is present and equals the request's own
origin, and no `Sec-Fetch-Site` names another site (`sameOrigin`, `hosted_session.js`): no Origin,
the sandboxed pages' `null`, and any other origin are all refused. No form token: the session
cookie is `SameSite=Lax`, so only a top-level form from another site carries it on a POST, and a
browser sends that form's own Origin.

**The page.** It is the Worker's own, not a page a run wrote, so it is not sandboxed: its policy is
`default-src 'none'`, one inline script allowed by its SHA-256, `connect-src 'self'`, `form-action
'self'`, `base-uri 'none'`, `frame-ancestors 'none'`. It holds no text a feed or a model wrote, only
the tenant's own values, each escaped. Its words are catalog keys (ADR-0027), formatted with the
login page's MessageFormat path, in the catalog the tenant's `digest.output_language` reaches by
language and script (`catalogTag`, held to `i18n.resolve` by the shared cases).

**The link.** A hosted run renders its pages with the Settings link at `/t/<tenant>/settings`
(`HtmlDigestWriter`'s `settings_path`); every other page keeps `/settings`. The mail already linked
the site's own `/settings`, which for a tenant is the same page. The vote probe answers `settings:
false, settings_link: true`: the link shows, and the archive link, the gated controls and the
interest page, which the hosted Worker does not serve, stay closed.

### Consequences

* Good, because a settings visit starts no container, and a save is one or two Durable Object calls.
* Good, because a digest page keeps its sandbox and `connect-src` exactly as ADR-0033 and ADR-0039
  left them.
* Good, because the eight keys and every cap are in one file both the Worker and the run read, and
  one case file holds the two sides' rules together.
* Good, because a stored feed URL, however it was written, and every redirect it answers, is held
  to the same rule at every poll.
* Bad, because the hosted page is a second settings page beside the self-hosted one, smaller and
  written in the Worker; a key added to the list needs its control here too.
* Bad, because a public name that resolves to a private address passes the check: there is no DNS
  lookup at the save or the poll. Cloudflare's own egress does not reach a tenant's private
  network, which bounds it.
* Bad, because a tenant's reader alerts reach no channel of the tenant's: its notify targets are
  pinned blank until a card gives tenants mail.
* Neutral, because a value given as a string (`"5"`) is refused, which `validate_setting` would
  coerce; the page sends JSON types, and the stricter rule keeps one answer per value.

### Confirmation

* `workers/app/test/hosted_settings.test.js` drives `hosted.js` over the real registry and tenant
  objects: the page's policy and its one hashed script; its catalog by output language; a save to
  the session's tenant and to no other; each forbidden key, a cap over 400 and every value rule;
  the schedule through the registry with the run cap kept; each Origin refusal; the body bound;
  source writes, URL refusals, the cap with newsletters counted, two concurrent saves at the cap,
  and removal.
* `workers/app/test/tenant_buffer.test.js` holds the poll: manual redirects, each hop refused before
  it is asked, the hop limit, the byte limit, the feed cap. `workers/rss/test/held.test.js` holds
  the rss Worker's default unchanged.
* `tests/test_hosted_settings_policy.py` runs the shared cases against `validate_setting`,
  `SourceConfig` and `i18n.resolve`, and holds the policy's caps to `feed_check.py`'s, the seed's
  model to the policy's, the page's tokens to the spec's and the image's copy of the file to the
  path the run reads.
* `tests/test_hosted_settings_pin.py` loads a hosted tenant whose rows name another provider, a
  webhook and an article cap of 4000, and gets the policy's.
* `tests/test_html_digest.py`, `tests/test_email_digest.py`, `tests/test_local_pages.py` and
  `tests/test_bootstrap_language.py` hold the Settings link in each mode and the probe's split.

## Pros and Cons of the Options

### A: the hosted Worker renders the page and routes

* Good, because no container, and the routes read and write one object.
* Good, because the page can be unsandboxed without touching any page a run wrote.
* Bad, because a second, smaller settings page to keep worded and styled.

### B: a TriageServer container per settings session

* Good, because the self-hosted page, its validation and its wording would be reused whole.
* Bad, because each session takes one of the two container slots the tenants' runs queue for, and
  waits up to 28 s for a cold start.
* Bad, because TriageServer's own routes that call a model (area suggestions, feed checks) would
  run unmetered by the tenant's allowance (ADR-0038), and its notify, model and onboarding routes
  would each have to be closed for a tenant.

### C: the run renders a settings page into the tenant's site

* Good, because the run already renders every page in the tenant's language.
* Bad, because a page the run wrote must stay sandboxed (`hosted_pages.js`): its script has no
  origin, so it could not post a save with the reader's session.
* Bad, because the page would show the values of the last run, stale after a save until the next
  one.
* Bad, because it would still need A's routes to save anything.

## More Information

* `docs/architecture.md` §4 has the routes' writes to the tenant's `settings` and `sources`, §5 the
  policy file and the poll's hold.
* Not in this decision: mail for a tenant's digest or alerts; caps an operator can change; a live
  fetch of a feed when it is saved; DNS-based private-address checks; the onboarding fetches in
  `bootstrap.py`.
* Unverified here: that a browser posts the page's fetch with the `Origin` the routes require, and
  the page's look on a real device. That needs a deployed hosted Worker; the operator should save
  once as a trial tenant's reader (`docs/hosted-tenants.md`, step 10).
