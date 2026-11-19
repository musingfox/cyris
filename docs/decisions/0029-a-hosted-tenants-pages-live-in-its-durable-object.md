---
status: accepted
date: 2026-11-18
decision-makers: ""
---

# A hosted tenant's pages live in its Durable Object, and the hosted Worker serves them

## Context and Problem Statement

A self-hosted run publishes its pages to one Cloudflare Pages project
([ADR-0004](0004-publish-over-pages-rest-without-r2.md)), and the app Worker proxies that project
to readers ([ADR-0010](0010-the-digest-stays-static-behind-the-app-worker.md)). Until this
decision a hosted tenant's run published nothing: its issue stayed in its `digests` table
([ADR-0028](0028-one-sqlite-durable-object-per-hosted-tenant.md)).

Hosted mode runs many tenants from one Worker. Pointing their runs at the one Pages project would
push every tenant's manifest into the same site, where each full-snapshot deploy would fight the
others. The pages are rendered in the container with Jinja (`adapters/output/html_digest.py`),
which the Worker cannot run. Where a hosted tenant's pages live, and what serves them, was
undecided.

## Decision Drivers

* A tenant's pages must be readable only through that tenant's route, and only after sign-in.
* A tenant's data should have one home, which keeps deleting, backing up, and later sharing or
  searching it simple.
* Serving a page must not wake a container (ADR-0010's reason).
* The self-hosted deployment must keep publishing to Pages as it does.
* `service_layer/` and `domain/` stay unchanged (`docs/architecture.md` §8).
* A Durable Object statement is at most 100 KB, and a raw page is about 105 KB. One value is at
  most 8 MiB.

## Considered Options

* Store the rendered pages in the tenant's Durable Object, and serve them from the hosted Worker
* One Pages project per tenant
* Render each page in the Worker on request

## Decision Outcome

Chosen option: "store the rendered pages in the tenant's Durable Object", decided by the human on
2026-11-18. The cost is small. An issue is about 45 KB and a raw page about 105 KB, so 100
tenants for a year is about 11 GB, about US$1.2 a month of Durable Object storage. The data then
has one home. The other two options each need something hosted mode lacks: an account-level
resource per tenant, or Jinja in the Worker.

How it is built:

* `HostedPages` (`src/cyris/adapters/output/hosted_pages.py`) fills the seam the Pages publisher
  fills: `publish_site`, `page_serves` and `site_filenames`. `bootstrap.build_deps` wires it
  whenever the store carries a tenant id, whatever `html_output` and `promote` say, and wires no
  Pages publisher. A hosted run renders its pages as every run does and writes each one through
  the gateway (ADR-0028) into the tenant's `hosted_pages` table. The body is always a bound
  parameter, never part of the statement. The digest page is written last, so its raw page and
  the archive are in the table before it is. A write that changes no row fails the publish, and
  so does a file that is not HTML or SVG, or bytes that are not UTF-8.
* `hosted_pages` is created by `src/cyris/adapters/store/hosted_schema.sql`, a file of its own and
  not part of `schema.sql`. A self-hosted D1 or a local install's file therefore never gains a
  table it does not use. Two paths apply the file, as two apply `schema.sql`: the operator's seed
  (`workers/app/src/hosted_seed.js`) and a hosted container's first load
  (`bootstrap.load_effective_config`). So a tenant seeded before this change gets the table on
  its next run.
* The hosted Worker serves a tenant's site under `/t/<tenant>/` (`workers/app/src/hosted_pages.js`)
  after the Access check every hosted request passes (ADR-0015). Until tenant sessions land, the
  one identity Access lets in is the operator's. The paths mirror the Pages layout.
  `/t/<tenant>/` is the latest issue: the most recently saved issue in `digests` whose digest page
  the site holds, which is also the issue the archive leads with. `/t/<tenant>/<date>-<period>`
  is one issue, `/t/<tenant>/<date>-<period>-raw` is its raw page, and `.html` names are taken
  too. `/t/<tenant>/index.html` is the archive, and the site's assets sit beside it.
  `/t/<tenant>` redirects to the slashed form, so a page's relative links stay inside the
  tenant's site. A route reads only the object named by its own tenant, so no path can return
  another tenant's page.
* The reader's type size ([ADR-0013](0013-reader-type-size-is-injected-by-the-worker.md)) carries
  over unchanged. Each tenant's `digest.type_scale` is read from its own object and injected by
  the same `sized` that the default Worker uses. A stored page has no validators to drop, and it
  is served `Cache-Control: private, no-cache`.
* The digest link is `https://<hosted domain>/t/<tenant>/<date>-<period>`. The hosted domain is
  one grade-B setting, `CYRIS_STORE_HOSTED_DOMAIN` (`[store] hosted_domain`). The operator sets it
  on the hosted Worker once (`scripts/hosted.sh secrets`, from the URL `deploy` kept). The Worker
  hands it to every tenant container, and it does not start a run without it. In the container,
  a tenant id needs it and it needs a tenant id. `bootstrap` answers `run_digest`'s Worker-domain
  lookup with the tenant's site root, `<domain>/t/<tenant>`, so `run_digest` builds the link
  unchanged. The mail's raw-page and archive links keep the digest link's path, so they stay in
  the tenant's site too.

What hosted mode replaces of the earlier decisions:

* Of ADR-0004, hosted mode replaces the destination: no Pages project, no Pages REST call, and no
  `pages_manifest` or `pages_deploy_receipt` rows. ADR-0004's reason for keeping only the file
  list does not apply to a tenant, because the tenant's own object holds the bytes themselves.
  The archive guards (the live index check and the shortfall tolerance) exist to stop a wrong
  database from deploying over a live site. A tenant's site has no separate store that could
  disagree with its database, so hosted mode has no such guard.
* Of ADR-0010, hosted mode keeps the reason, that a page view must not wake a container, and
  replaces the means. The Worker reads a stored page from a Durable Object instead of proxying
  Pages. It also replaces the stance on access: a tenant's pages are never public, because every
  hosted path sits behind sign-in.
* The self-hosted deployment is unchanged. `wrangler.toml`, `index.js` and the Pages publisher
  wiring for a store without a tenant id are as before, and a self-hosted D1 has no
  `hosted_pages` table.

### Consequences

* Good, because each tenant's pages, issues and rows live in one object. Deleting the tenant's
  object deletes all of them, and a later backup or export has one place to read.
* Good, because a page view costs one Worker request and one or two Durable Object reads. It
  never wakes a container, and no shared Pages project or per-tenant account resource is needed.
* Good, because `digests` still holds each issue's `DigestContent`, so a page can be rendered
  again from it. A run already republishes any issue its site lacks (`_republish_missing`), and a
  tenant's issues stored before this change reach its site on its next run.
* Good, because `service_layer/` and `domain/` did not change. The page store is an adapter
  behind the publisher seam.
* Bad, because the run's link building reuses the Worker-domain lookup to carry a path
  (`<domain>/t/<tenant>`). A later change to `_worker_domain` must keep the value opaque.
* Bad, because the pages' absolute links (`/settings`, `/login`, `/api/vote`, `/interest`) reach
  the hosted Worker, which has no such routes yet and answers 404. The vote buttons stay hidden,
  because their probe of `/api/vote` fails. The mail's `/settings` link would also point at the
  tenant's site. The mail is not sent until operator alerts and tenant mail land.
* Bad, because a page is about 45 to 105 KB of Durable Object storage per issue, about 11 GB for
  100 tenants over a year, against nothing for the bytes a Pages project holds.
* Neutral, because a value is at most 8 MiB. A page that large would fail its own write, and that
  run's status would be `publish_failed`. No page this system renders comes close.

### Confirmation

* `tests/test_hosted_pages.py` holds `HostedPages` to the seam. It checks that each page is
  written with its type, that the digest page is written last, that a page over 100 KB is bound
  rather than written into the statement, and that `hosted_pages` is not in `schema.sql`.
* `tests/test_hosted_run_wiring.py` checks the run itself. A hosted run is wired to its own site
  and to no Pages publisher, and a self-hosted one still publishes to its Pages project. A hosted
  run writes its digest and raw page into the tenant's table and sends no request outside, and
  its links and its notifications' links point at the hosted domain. A page over 100 KB round
  trips through the gateway, and an issue the site lacks is republished into it.
* `workers/app/test/hosted_pages.test.js` checks the serving side. The latest issue, an older one
  and a raw page answer 200 to an operator, and every path answers 403 without a sign-in. No
  tenant's route returns another tenant's page. A page over 100 KB is served whole, and the
  tenant's type size is injected.
* `tests/test_hosted_config.py` holds the site path and the domain's name to one shape on both
  sides. `tests/test_residency_covers_schema.py` holds `hosted_pages` to its §4 row.
* The human's check on the trial account is `docs/hosted-tenants.md` step 7. It fetches each
  tenant's `/t/<tenant>/` and checks that the page answers 200, that it names the tenant's newest
  issue, and that it is the page the tenant's own database holds.

## Pros and Cons of the Options

### Store the rendered pages in the tenant's Durable Object

* Good, because the data has one home, and isolation is ADR-0028's: one object per tenant.
* Good, because the pages are rendered once, by the code that renders them today.
* Bad, because pages take Durable Object storage, about US$1.2 a month for 100 tenants over a
  year.

### One Pages project per tenant

* Good, because the self-hosted publisher and its guards would run as they are.
* Bad, because every tenant would be an account-level project created through the REST API,
  which ADR-0028 avoided for tenants' databases: 1,200 requests per 5 minutes across the account.
* Bad, because a project is public at `<name>.pages.dev`, whatever the Worker in front of it
  checks.
* Bad, because a tenant's data would live in two homes, its object and its project.

### Render each page in the Worker on request

* Good, because only `digests` would be stored, and every page would follow the current
  templates.
* Bad, because the Worker cannot run Jinja. The templates would have to be ported to JavaScript
  and kept in step, or each page view would wake the container, which is what ADR-0010 avoids.

## More Information

* `docs/architecture.md` §4 has the row for `hosted_pages`. §5 grades the hosted domain and the
  site path, and §6 describes hosted serving.
* `docs/hosted-tenants.md` is the trial runbook. Its check step now fetches each tenant's latest
  page.
* Later cards: tenant sessions put a per-tenant sign-in in front of `/t/<tenant>/`. Operator
  alerts and tenant mail send the notifications this decision only links.
