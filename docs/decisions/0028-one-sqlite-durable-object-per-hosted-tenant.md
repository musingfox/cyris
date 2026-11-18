---
status: accepted
date: 2026-11-18
decision-makers: ""
---

# One SQLite Durable Object per hosted tenant

## Context and Problem Statement

A hosted deployment runs many readers' digests from one Worker and one container image. Each
reader, a tenant, needs every table `schema.sql` creates, and no tenant may read or write another
tenant's rows. A deployment today keeps those tables in one D1 database. Its container reaches
that database over the D1 REST API with an account token (`D1Client`), and the app Worker makes
two REST reads of its own (the schedule in `schedule.js`, the type size in `type_scale.js`).

Where each tenant's tables live, and how a tenant's run container reaches them, was undecided.
The choice is hard to reverse once tenants hold data. It also decides which account-wide limit
caps the number of tenants.

## Decision Drivers

* Isolation: a tenant's run must reach its own tables only. Its container must hold no credential
  that reaches another tenant's tables.
* The core must not change. Every store talks through `D1Queryable` (`adapters/store/d1.py`), so
  the swap has to happen behind that seam (`docs/architecture.md` §8).
* A run makes about 123 queries, so the run pays the store's latency about 123 times.
* Account-wide limits must leave room for more than a handful of tenants.
* Adding a tenant should not need a new binding or a redeploy. The registry that later replaces
  the hard-coded tenant list (`hosted-tenant-registry`) should be a data change only.

## Considered Options

* One SQLite Durable Object per tenant, reached through a gateway the Worker intercepts
* D1 over the REST API, as a deployment does today
* One D1 database per tenant, bound to the hosted Worker

## Decision Outcome

Chosen option: "one SQLite Durable Object per tenant, reached through a gateway the Worker
intercepts". It is the only option that meets every driver. The Worker enforces isolation, so the
container holds no credential. No account-wide REST limit applies, so the tenant count is not
capped by one. A Durable Object named by the tenant's id exists the first time it is addressed, so
adding a tenant needs no binding.

How it is built:

* `TenantDb` (`workers/app/src/tenant_db.js`) is a SQLite-backed Durable Object class. A tenant's
  object is `TENANT_DB.getByName(<tenant id>)`. Its `query(sql, params)` takes the arguments
  `D1Queryable.query` takes and returns `{rows, changes}`. `changes` is the difference between two
  `total_changes()` readings taken around the statement, which is the rows-affected figure D1
  reports. It is not `rowsWritten`, which also counts index writes. Code that counts tables skips
  the internal tables, whose names start with `_`.
* The hosted Worker is a separate deployment with a config file of its own,
  `wrangler.hosted.toml`: Worker `cyris-hosted`, entry `workers/app/src/hosted.js`. It is not an
  environment of `wrangler.toml`, because a second environment there would make every production
  deploy warn that none was named. `wrangler.toml`, which a push to main deploys (ADR-0024), is
  unchanged, and its dry run differs only in the size of the bundle its shared modules make.
* A tenant's run container is the Durable Object `TENANT_RUN.getByName(<tenant id>)`. Before every
  `start()`, it registers `interceptOutboundHttp("db.cyris", ...)` to the `TenantDbGateway`
  entrypoint, with `props.tenant` set to its own name. It also starts the container with
  `CYRIS_STORE_TENANT_ID` set to that name. The gateway answers only
  `POST /t/<props.tenant>/query` and refuses any other tenant with 403.
* In the container, `bootstrap.build_d1_client` returns `GatewayD1Client` whenever the store
  carries a tenant id. `load_effective_config`, `doctor` and the CLI all go through
  `build_d1_client`. `GatewayD1Client` posts each query to `http://db.cyris/t/<tenant>/query` and
  keeps `D1Client`'s retry rules (`post_with_retries`).
* The hosted Worker reads the schedule from each tenant's Durable Object directly, not over REST.
  When it cannot read a tenant's schedule it starts nothing for that tenant: the container would
  reach the same object and fail the same way.
* Every hosted object is created with the location hint `wnam`, beside the container's only region
  (`WNAM`). Only an object's first `get()` honours a hint, and the operator's seed request may be
  the first, from anywhere.
* A hosted start that fails with "There is no container instance that can be provided to this
  Durable Object" is retried with backoff for up to 90 seconds.

### Evidence

A throwaway probe Worker, kept outside this repository, measured these facts on the trial
account on 2026-11-18:

* The whole path works with `enableInternet: true`: from the container to
  `http://db.cyris/t/<tenant>/query`, through the app Worker's
  `ctx.container.interceptOutboundHttp()`, to a `WorkerEntrypoint` gateway, to
  `TENANT_DB.getByName(tenant)`. The interception must be registered before `start()`. It
  survives a restart of the Durable Object that owns it. A gateway whose `props` are set on the
  Worker side refuses other tenants with 403.
* Latency per query: reads have a median of 42 ms and writes a median of 66 ms. Almost all of it is
  the Worker-to-Durable-Object RPC. At about 123 queries per run, that is roughly 5 to 8 seconds of
  store time per run.
* Limits: one SQL statement is at most 100 KB, so values always go in bound parameters. One value
  is at most 8 MiB. An RPC argument or return value is at most 32 MiB. The whole `schema.sql`
  (16.6 KB) runs as one `exec`.
* After `destroy()`, an immediate `start()` with `max_instances` reached fails with "There is no
  container instance that can be provided to this Durable Object, try again later" for about
  10 seconds.
* Intercepted calls are not counted as Worker invocations.

### Consequences

* Good, because isolation is the Worker's to enforce. Each gateway is bound to one tenant by
  `props`, which the container cannot set, and the container holds no store credential.
* Good, because store queries cross no account-wide REST limit, and intercepted calls are not
  Worker invocations.
* Good, because every store runs unchanged over `D1Queryable`. `service_layer/` and `domain/` do
  not change.
* Good, because a tenant's database is created when it is first addressed, with no binding and no
  deploy.
* Bad, because the run pays 42 to 66 ms per query in sequence, about 5 to 8 seconds per run. D1
  over REST from the container's region was not measured against it.
* Bad, because the D1 tools no longer reach a tenant's tables. `wrangler d1`, D1's export, and the
  trial seed and reset scripts cannot touch them. Only the hosted Worker's operator routes reach a
  tenant's rows.
* Bad, because `sqlite_master` lists the Durable Object's internal tables. Every count of tables
  has to skip names that start with `_`.
* Bad, because tenants share the container application's `max_instances`. A start that lands while
  every instance is held waits up to 90 seconds, and then fails that start.
* Neutral, because a hosted tenant drains no shared queue. Its container gets none of the rss,
  promote or newsletter Worker URLs. Its RSS is polled directly at run time. It has no newsletters
  and no vote sync until those Workers are per tenant.
* Neutral, because until later cards add per-tenant serving and operator alerts, a hosted run
  publishes nothing and sends no Discord message and no mail. The finished issue stays in that
  tenant's `digests` table.

### Confirmation

* `tests/test_tenant_gateway.py` holds `GatewayD1Client` to the `D1Queryable` contract and to
  `D1Client`'s retry rules.
* `tests/test_hosted_run_wiring.py` checks three things. A container that carries a tenant id gets
  the gateway. A hosted run is wired to no queue, page or channel. A hosted run keeps its issue and
  sends no request outside.
* `workers/app/test/tenant_db.test.js` checks that the gateway refuses another tenant with 403. It
  also checks that `changes` counts rows rather than index writes, and that the table count skips
  `_` tables.
* `workers/app/test/hosted.test.js` checks that a hosted container gets none of the three Worker
  URLs. It also checks that interception is registered before `start()`, and that a missing
  instance is retried within 90 seconds.
* `tests/test_hosted_config.py` holds `wrangler.hosted.toml` apart from `wrangler.toml`. It also
  holds the seed's settings valid and its sources fetchable.
* `tests/test_hosted_script.py` and `tests/test_hosted_check.py` cover the runbook's driver. It
  confirms the account by name and sends every secret on stdin. Its verdict tells two tenants'
  rows apart.
* The human's check on the trial account is `docs/hosted-tenants.md`. Two tenants are seeded and
  run once each. Each tenant's `digest_runs` and `digests` must hold only its own rows.

## Pros and Cons of the Options

### One SQLite Durable Object per tenant, through an intercepted gateway

* Good, because the Worker binds each gateway to one tenant, so the container needs no credential.
* Good, because a new tenant is a new object name, not a new binding.
* Good, because no account-wide REST limit applies, and the calls are not Worker invocations.
* Bad, because each query is a Worker-to-Durable-Object RPC, 42 ms to read and 66 ms to write.
* Bad, because the tables are outside every D1 tool.

### D1 over the REST API

* Good, because it is how every deployment works today. `D1Client` and the Worker's REST reads
  need no change.
* Bad, because the Cloudflare API allows 1,200 requests per 5 minutes across the account. At about
  123 queries a run, that fits about 9 tenants' runs in one window, before Pages publishing and the
  Worker's own reads take their share.
* Bad, because the container must hold an account token that can reach every database on the
  account. Isolation would then rest on which database id the container is given.

### One D1 database per tenant, bound to the hosted Worker

* Good, because each tenant's tables are a D1 database, which the D1 tools reach.
* Bad, because bindings are declared in the Worker's config. Each new tenant would be a config
  change and a deploy, and the gateway would carry every tenant's binding.
* Bad, because reaching a database without a binding means the REST API again, with the limit
  above.

## More Information

* `docs/architecture.md` §4 has the row for a hosted tenant's database. §5 grades the tenant id,
  the tenant list, the gateway host, the location hint and the start retry.
* The hard-coded tenant list was replaced by the operator registry, one SQLite Durable Object
  ([ADR-0030](0030-the-operator-registry-is-one-sqlite-durable-object.md)). `CYRIS_HOSTED_TENANTS`
  and its §5 row are gone; a tenant is registered with `POST /hosted/tenants/<tenant>/register`.
* `docs/hosted-tenants.md` is the procedure. It deploys the hosted Worker to the trial account,
  seeds two tenants, runs each once, and checks each tenant's rows.
* [ADR-0029](0029-a-hosted-tenants-pages-live-in-its-durable-object.md) ends the "publishes nothing"
  consequence above: a hosted run stores its pages in its tenant's Durable Object, and the hosted
  Worker serves them at `/t/<tenant>/`.
