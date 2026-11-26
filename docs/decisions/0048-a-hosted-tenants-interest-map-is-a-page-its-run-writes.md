---
status: accepted
date: 2026-11-26
decision-makers: ""
---

# A hosted tenant's interest map is a page its run writes

## Context and Problem Statement

On a local or cloud deployment the interest map is drawn on each request: the `ui` container's
`/interest` (`TriageServer`) reads the live holds in `suppressions`, the voted `stored_articles` and
their `article_tags`, builds the graph (`domain/interest_graph.py`), lays it out and renders
`interest.html.j2` (`service_layer/interest_page.py`). The archive links it from a `.gated` line,
which the pages' capability probe opens for a signed-in reader.

A hosted tenant had no map. The hosted Worker runs no Python and starts a container only for a run
(ADR-0032), so ADR-0040 had the vote probe answer `settings: false`, which kept the gated controls
closed, and the archive's link, an absolute `/interest`, named no page a tenant's reader could open.
Milestone M6 of the edition-parity plan is to let a tenant's signed-in reader open the map at
`/t/<tenant>/interest` and to open the archive's Interest link on hosted.

What the map draws changes only at a run: a hosted vote is queued by the Worker and applied by the
tenant's next run ([ADR-0039](0039-a-hosted-vote-is-queued-by-the-worker-and-applied-by-the-run.md)),
and holds are set by a run. Only the hold clocks move between runs, as time passes.

## Decision Drivers

* A map visit must not take a container: the containers are the tenants' run slots (`RUN_SLOTS`).
* One renderer for every edition, so the hosted map is the self-hosted map, worded from the same
  catalogs.
* The tenant's pages stay in its own Durable Object, behind its session (ADR-0029, ADR-0033).
* A map that cannot be drawn must never cost the tenant its digest.

## Considered Options

* The tenant's run renders the map and stores it in its `hosted_pages`, beside its issues.
* A container per request, as the `ui` container answers `/interest`.
* Drawing the map in the Worker, from the tenant's object.

## Decision Outcome

Chosen: the run writes the page, because it takes no container and no second renderer, and a map
drawn at a run is drawn from the state a hosted reader's votes reach.

Every hosted run that is not a preview, an early return (no articles, nothing pending) or a failed
run included, draws the map as it ends, in `run_digest`'s closing block beside the spend report,
through the optional `Deps.publish_interest`. `bootstrap.build_deps` wires it only for a hosted
tenant: `HostedPages.publish_interest` (`adapters/output/hosted_pages.py`) renders with
`render_interest_page`, `D1InterestReader` over the tenant's gateway and the run's own
`HtmlDigestWriter`, and upserts the page at `/interest.html` in the tenant's `hosted_pages`. A page
that cannot be rendered or stored is logged and kept in the run's summary as `interest_page_error`;
the run's status, its digest and its alerts are unchanged.

The hosted Worker serves it at `/t/<tenant>/interest` (`INTEREST_PAGE` in
`workers/app/src/hosted_pages.js`) as it serves the tenant's issues: to a live session of that
tenant, with the session's vote token, the vote policy and `private, no-cache`; a session of another
tenant gets the one fixed 404 every other tenant's path gets; a request with no session is sent to
`/login`. A tenant whose run has not yet written the map gets the same fixed 404, so nothing tells
an unwritten map from a missing tenant; since the run that writes the archive with the link writes
the map too, a reader meets it only when that write failed.

A tenant's archive links Interest at `/t/<tenant>/interest` (`HtmlDigestWriter`'s
`interest_path`; every other deployment keeps `/interest`). The vote probe answers `{authorized:
true, settings_link: true}`, without `settings: false`, so a signed-in tenant reader gets the whole
page every edition shows: the vote buttons, Archive, Settings, the gated controls (the archive's
Interest link and the All articles page's List and Triage views, which vote through the same vote
route) and Sign out. No server answers `settings: false` any more, and the probe no longer reads it.

The map links only what its site bar links: the brand and Archive (`index.html`, relative, so the
tenant's archive), Settings (`/t/<tenant>/settings`) and Sign out (`/logout`). It has no vote
button and no topic control, so nothing on it needs a route the hosted Worker lacks.

This amends [ADR-0040](0040-a-hosted-tenant-edits-its-settings-on-the-hosted-worker.md)'s *The
link*, whose probe answer kept the gated controls and the interest page closed, and the sentences
of ADR-0039 and [ADR-0043](0043-one-settings-page-an-edition-locks-a-key.md) that say the same.

### Consequences

* Good, because a map visit is one Durable Object read, like an issue, and starts no container.
* Good, because the hosted map is the self-hosted map: one renderer, one template, one catalog.
* Good, because the tenant's map lives where its pages live, read only by the hosted Worker
  behind the tenant's session, and leaves with the tenant's object.
* Bad, because the hold clocks are as of the run's end, not the request: a node's brightness and
  its days left are up to a schedule's interval old. The page's `as of` line names the time it was
  drawn.
* Bad, because a run whose write fails leaves the previous map in place, older than the run; the
  run's summary names the failure.
* Neutral, because a preview writes no map, as it publishes no page.

### Confirmation

`tests/test_hosted_run_wiring.py` runs two tenants' hosted runs through their gateways and reads
each tenant's map back from its own `hosted_pages`, with none of the other's tags in it, and holds a
preview that writes none and a failed write that leaves the run as it was.
`workers/app/test/hosted_login.test.js` holds the route's four answers.
`tests/test_local_pages.py` runs the pages' probe against the hosted Worker's `PROBE` and finds the
gated controls open; `tests/test_html_digest.py` holds the tenant's archive linking its map; and
`tests/test_hosted_config.py` holds `INTEREST_PAGE` to one value on both sides.

## Pros and Cons of the Options

### The tenant's run renders the map and stores it in its `hosted_pages`

* Good, because it reuses the run's renderer and the page store the run already writes.
* Good, because a hosted vote reaches the state the map draws only at a run anyway.
* Bad, because the hold clocks age between runs.

### A container per request

* Good, because the map would be drawn as of the request, as on a local or cloud deployment.
* Bad, because each visit would take one of the containers the tenants' runs queue for, and wait
  for a cold start.
* Bad, because a reader's visit would start compute the operator pays for, with no cap of its own.

### Drawing the map in the Worker

* Good, because the map would be drawn as of the request, with no container.
* Bad, because the graph, its layout and the page would be a second implementation in JavaScript,
  kept in step with the Python one by tests, as ADR-0043 retired for the settings page.
* Bad, because the Worker would read the tenant's votes and holds on every visit.
