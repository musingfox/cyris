---
status: accepted
date: 2026-11-18
decision-makers: ""
---

# The operator registry is one SQLite Durable Object

## Context and Problem Statement

The hosted Worker `cyris-hosted` (ADR-0028) keeps each tenant's tables in a SQLite Durable Object
of the tenant's own. The deployment's own data had no home. That data is the tenant list, the
invite list, login sessions, the map from a newsletter's random inbound address to its tenant,
and each tenant's monthly spend rollup. The tenant list was a stub, `CYRIS_HOSTED_TENANTS` in
`wrangler.hosted.toml`'s `[vars]`, so adding a tenant was a commit and a deploy. The scheduling,
login, newsletter, budget, backup, onboarding and delete cards all need this data.

Where the operator's data lives, and how the Worker reads the tenant list from it, was undecided.

## Decision Drivers

* Sessions and invites need strong consistency. A revoked session must stop at once, and an
  invite must be redeemed once.
* Adding or pausing a tenant must be a write, not a deploy.
* The data should share a backup path with the tenant databases, rather than open a second one.
* Isolation: no tenant's run container may reach the deployment's data.
* The hourly tick reads the tenant list every hour, and every operator route reads it, so one read
  must be cheap.

## Considered Options

* One SQLite Durable Object, the registry
* Workers KV
* One D1 database bound to the hosted Worker
* Keep the list in `[vars]`, and put the rest elsewhere later

## Decision Outcome

Chosen option: "one SQLite Durable Object, the registry", because it is the only option that is
strongly consistent, needs no deploy to add a tenant, and is the same technology as the tenant
databases. The human decided this on 2026-11-18.

How it is built:

* `TenantRegistry` (`workers/app/src/tenant_registry.js`) is a SQLite-backed Durable Object class,
  bound as `REGISTRY` in `wrangler.hosted.toml` only. Its migration is `v3`. `wrangler.toml` does
  not change.
* There is one registry, `REGISTRY.getByName("cyris:registry")`, created with the tenant objects'
  location hint `wnam`. The name is outside the tenant id pattern, so no tenant id can name it.
* Its schema is its own, `REGISTRY_SCHEMA`, not `schema.sql`. The object runs it on every start;
  every statement is `CREATE ... IF NOT EXISTS`. The tables are `tenants` (id, status,
  `digest_times`, created and updated times), `invites`, `sessions`, `inbound_addresses` and
  `spend_rollup`. Only `tenants` has a reader and a writer. The others are created empty, so the
  cards that fill them add code and no new home for state.
* A tenant's status is `active` or `paused` (`tenant_status.js`). Only an active tenant is
  scheduled or started.
* The operator registers a tenant with `POST /hosted/tenants/<tenant>/register[?status=S]`, behind
  the Access check (ADR-0015). A named status is set whether or not the tenant was registered.
  With no status named, an absent tenant is registered as active and a registered one keeps its
  status, so re-running the runbook's `register` never resumes a paused tenant. Either way the
  row keeps its `created_at`. An id outside the tenant pattern, or an unknown status, is answered
  400 and writes nothing.
* The seed route requires a registered tenant. It does not register one. So do the run and report
  routes: each answers 404 for a tenant the registry does not hold. The run route answers 409 for
  a paused one. Registering is one deliberate step, and a mistyped tenant in a seed creates no
  database.
* The hourly tick reads the active tenants from the registry. It still reads each tenant's
  schedule from the tenant's own `settings`. `digest_times` waits for the scheduling card.
* `TenantContainer.startRun` asks the registry again before it registers the gateway and starts
  the container. It refuses a tenant that is not active and registered, whichever path asked.
* `TenantDbGateway` never names the registry. It answers 403 to every request except
  `POST /t/<its own tenant>/query`. Before this change, a request of another shape was answered
  404.

### Consequences

* Good, because sessions and invites get a strongly consistent store. KV's eventual consistency
  would let a revoked session live on at other locations.
* Good, because a tenant is now a registry row. Adding one, pausing one, or resuming one needs no
  commit and no deploy.
* Good, because the registry is a SQLite object like the tenant databases, so the backup card
  backs up both the same way.
* Good, because isolation holds by construction. A container holds no binding, and the one piece
  of Worker code it can reach, the gateway, answers one tenant's queries and nothing else.
* Bad, because one object serialises every request to it. The tick reads it once an hour, and each
  operator request reads it once, which is far below one object's limit. Login traffic, a later
  card, is the load to watch.
* Bad, because every tenant route and every start now makes one more Durable Object RPC.
* Bad, because no D1 tool reaches the registry, as no D1 tool reaches a tenant database
  (ADR-0028). Only the hosted Worker's routes do.
* Bad, because a first deploy has an empty registry. Until the operator registers a tenant, the
  tick starts nothing and every tenant route answers 404. The runbook registers both tenants
  before it seeds them.
* Neutral, because the schema has no version table yet. The first card that changes an existing
  registry table adds versioned steps, as ADR-0016 does for `schema.sql`.

### Confirmation

* `workers/app/test/tenant_registry.test.js` checks that the registry creates its own five tables
  and none of `schema.sql`'s. It checks registration and refusals, and that the registry is one
  object under a fixed name, hinted like the tenants. It drives `hosted.js` itself, and checks two
  things. The tick starts only the registry's active tenants and reads no other tenant's database.
  Seed, run and report refuse an unregistered tenant before reaching any of its objects.
* `workers/app/test/hosted.test.js` checks that the run container refuses to start an unregistered
  or paused tenant, before it registers any interception. It also checks the register route, and
  the 409 for a paused tenant's run.
* `workers/app/test/tenant_db.test.js` checks that the gateway answers 403 to requests aimed at the
  registry: its name as a tenant, `/registry/...` paths, and other shapes. It also checks that the
  gateway never gets the registry, even though the registry binding is in its env.
* `tests/test_hosted_config.py` checks that the registry is bound only in `wrangler.hosted.toml`,
  and that the file lists no tenant. `tests/test_hosted_script.py` checks that `scripts/hosted.sh
  register` registers both tenants before the runbook seeds them.
* The human's check on the trial account is `docs/hosted-tenants.md`.

## Pros and Cons of the Options

### One SQLite Durable Object, the registry

* Good, because it is strongly consistent and transactional.
* Good, because it is the tenant databases' technology, with their backup path and their tooling.
* Good, because the Worker reaches it through a binding that no container holds.
* Bad, because one object handles every request in sequence.

### Workers KV

* Good, because reads are cheap and cached at the edge.
* Bad, because it is eventually consistent. A write can take up to 60 seconds to reach other
  locations, so a revoked session or a used invite could still be accepted.
* Bad, because it has no queries or transactions. A spend rollup and an invite redemption are
  read-modify-write.

### One D1 database bound to the hosted Worker

* Good, because the D1 tools reach it.
* Bad, because it is a second technology beside the tenant objects, with its own backup path.
* Bad, because it is one more binding and database to provision per hosted deployment.

### Keep the list in `[vars]`

* Good, because it needs no code.
* Bad, because each tenant is a commit and a deploy, which ADR-0028's drivers already ruled out
  for the registry.
* Bad, because the rest of the operator's data still needs a home.

## More Information

* `docs/architecture.md` §4 has the registry's row. §5 grades the registry's name and the tenant
  statuses, and no longer lists `CYRIS_HOSTED_TENANTS`.
* `docs/hosted-tenants.md` registers and seeds `alpha` and `bravo`, then runs and checks each.
* ADR-0028's tenant list is the stub this replaces.
