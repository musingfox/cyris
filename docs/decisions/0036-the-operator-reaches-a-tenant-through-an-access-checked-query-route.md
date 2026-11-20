---
status: accepted
date: 2026-11-20
decision-makers: ""
---

# The operator reaches a hosted tenant through an Access-checked query route

## Context and Problem Statement

A hosted tenant's tables are its own SQLite Durable Object
([ADR-0028](0028-one-sqlite-durable-object-per-hosted-tenant.md)). Only two things reach one:
the tenant's run container, through a gateway the hosted Worker intercepts at `http://db.cyris`,
and the hosted Worker's own operator routes (seed, run, poll, report). No D1 tool reaches it.

So the operator's tools reach no tenant. `cyris` commands, `cyris doctor` and the scripts open
their store with `bootstrap.build_d1_client`, which gives a self-hosted machine D1 over REST or
a local file. Inside a run container it gives `GatewayD1Client`, whose host resolves only there.
The spend meter, the tenant backup and the beta launch gate all need the operator to read one
tenant, and sometimes to write one.

How the operator's process reaches one named tenant, with what credential, and how a command
names the tenant, was undecided.

## Decision Drivers

* The core must not change. Every store talks through `D1Queryable`, so the operator's way in
  has to answer that seam, as `GatewayD1Client` does.
* Isolation stays the Worker's to enforce. A run container must gain no way to name a tenant,
  and no credential may reach a tenant without the operator's sign-in.
* Without the operator's credential, naming a tenant must be refused, before any byte of the
  tenant's data is read or written.
* Every `cyris` command, not one, should take a tenant. Changing each command's signature would
  touch every entrypoint.
* No new kind of secret. The operator already has an Access service token (ADR-0015) that the
  hosted runbook signs every route in with.

## Considered Options

* An Access-checked query route on the hosted Worker, and a client of `D1Queryable` that calls it
* A report or export route per question the operator asks
* Running the operator's commands inside the tenant's run container
* The query route, admitting only an Access JWT whose `common_name` is one named service token

## Decision Outcome

Chosen option: "an Access-checked query route on the hosted Worker, and a client of
`D1Queryable` that calls it", because it gives every existing command and store the tenant
unchanged, through the seam they already use, and it adds no credential.

How it is built:

* The hosted Worker answers `POST /hosted/tenants/<tenant>/query` with a body of
  `{sql, params}` (`workers/app/src/hosted_router.js`). It runs the same Access JWT check as
  every hosted route, then refuses a tenant the registry does not hold with 404, before any
  object is reached. A paused tenant is still answered, as its seed and report routes are. The
  answer comes from `answerQuery` (`workers/app/src/tenant_db.js`), which the run's gateway now
  answers through too. So the operator reads `{rows, changes}`, and D1's error shape, exactly as
  the tenant's run does.
* `OperatorD1Client` (`adapters/store/tenant_gateway.py`) posts each query to that route on
  `https://<CYRIS_STORE_HOSTED_DOMAIN>`, with `CF-Access-Client-Id` and
  `CF-Access-Client-Secret` from `CYRIS_ACCESS_CLIENT_ID` and `CYRIS_ACCESS_CLIENT_SECRET`. It
  keeps `GatewayD1Client`'s reading of the answer and `D1Client`'s retry rules. Without both
  values it sends nothing. It follows no redirect, since Access's Location is its login page. A
  redirect, a 401, a 403 or a 404 is raised at once with its reason and never retried.
* `cyris --tenant <id>`, an option of the `cyris` command itself, sets `CYRIS_OPERATOR_TENANT`
  for the process. Every command loads its config through the environment, so one option reaches
  them all. `StoreConfig` turns it into the tenant id, with `operator` set and the backend `d1`.
  The backend is forced because the operator's own file names the operator's own store, not the
  tenant's. `bootstrap.build_d1_client` then returns `OperatorD1Client`. With the hosted domain
  and the tenant id set, `build_deps` wires the hosted path as it does in a run container.
* The name is refused where it would be ambiguous or forged. `--tenant` takes only a tenant id
  (`config.TENANT_ID`). An empty value is refused at the option, not ignored, because ignoring
  it would run the command on the operator's own store. A config file may not set
  `[store] operator`. `CYRIS_OPERATOR_TENANT` beside `CYRIS_STORE_TENANT_ID` stops the load,
  because a run container is its own tenant's. A run container never has the name:
  `hostedContainerEnv` is an allow-list.
* `cyris --tenant <id> doctor` reports on that tenant alone (`_tenant_checks`): its sources,
  settings, model, store, feed health and last run, read through the route. It skips the checks
  of the operator's own machine, its files, Workers, publishing, channels and keys, because the
  hosted Worker wires a tenant's run and the operator's machine does not.

### Consequences

* Good, because every `cyris` command, and every store under it, reaches a tenant with no change
  to `service_layer/`, `domain/` or any command's signature.
* Good, because the tenant's answers are the gateway's, from one function, so a tenant read by
  the operator and by its own run cannot drift apart.
* Good, because a missing credential is refused before any request. A wrong one is refused by
  Access or the Worker before any object is reached, and the registry check keeps a mistyped
  tenant from creating an object.
* Bad, because the route takes any SQL, writes included. Anyone Access admits to `cyris-hosted`
  can read and change every registered tenant. That is today's operator alone ("Until tenant
  sessions land, Access admits the operator alone", `docs/architecture.md` §6). The tenant
  sessions card must keep this route, like every `/hosted/tenants/` route, the operator's only.
* Bad, because each query is a round trip from the operator's machine to the hosted Worker, and
  then to the object. That is slower than the run's 42 to 66 ms. A command that makes thousands
  of queries is slow from a laptop.
* Bad, because loading a tenant's config applies `schema.sql` and `hosted_schema.sql` through the
  route, as the run container's first load does. `cyris --tenant <id> doctor` therefore writes,
  as `cyris doctor` already does on D1. Those statements are `CREATE ... IF NOT EXISTS` and
  versioned steps.
* Neutral, because the scripts outside `cyris` (`scripts/prune_archive.py`) take the tenant from
  the same `CYRIS_OPERATOR_TENANT` and get no option of their own. `prune_archive` works on a
  Pages manifest, which a hosted tenant has none of.

### Confirmation

* `workers/app/test/tenant_registry.test.js` drives `hosted.js` itself. The query route reads
  and writes one registered tenant's database and no other's, and a paused tenant is still
  answered. An unregistered tenant gets 404 with no object reached. A refused statement is
  answered in D1's shape. A request with no Access JWT, a malformed one or another team's, and a
  Worker with no Access settings, are answered 403, before the registry or any tenant object is
  reached.
* `workers/app/test/tenant_db.test.js` passes unchanged, so the gateway's refusals hold after
  its answer moved into `answerQuery`.
* `tests/test_operator_tenant_access.py` checks the client. Its queries go to the tenant's route
  with both headers, and nothing is sent without both values. A refused token, a redirect and an
  unregistered tenant are named at once and not retried, and a 5xx is retried as the gateway's
  is. It checks the choice of store: the operator's tenant over a file's backend, a run
  container's tenant kept on its gateway, and both names together refused. Through the CLI it
  checks that `cyris --tenant alpha sources list` reads only alpha's rows and that
  `articles accept` writes only alpha's. `cyris --tenant alpha doctor` reports on alpha.
  Without the service token it exits 1 having sent nothing. An empty or invalid `--tenant` is
  refused at the option.

## Pros and Cons of the Options

### An Access-checked query route, and a `D1Queryable` client

* Good, because every command and store works unchanged, through the seam they already use.
* Good, because the credential is the one the runbook already holds.
* Bad, because the route is as strong as raw SQL, so who Access admits matters more than before.

### A report or export route per question

* Good, because each route can answer one question and no more, read-only.
* Bad, because every question the spend meter, the backup and the launch gate ask would be a
  Worker route and a deploy, and no existing command or store could run against a tenant.

### Running the operator's commands inside the tenant's run container

* Good, because the container already holds the gateway, so no new route is needed.
* Bad, because it needs a container start per question, and a share of the instances
  `max_instances` allows. A start made while every instance is held waits up to 90 seconds.
* Bad, because a command's output and exit code would have to come back through the Worker,
  which no route carries today.

### The query route, admitting only one named service token

* Good, because a human signed in to Access, or a later tenant, could not reach raw SQL.
* Bad, because it needs a new grade-B setting, the operator token's Client ID, and a change to
  the Access check that every hosted route and the default Worker share. That check is the
  tenant sessions card's to reshape.
* Neutral, because today Access admits the operator alone, so it would refuse no one. It is the
  option to take if tenant sign-in comes through Access rather than through sessions of its own.

## More Information

* `docs/architecture.md` §2 names the client beside the other three. §4's hosted store row names
  the route as a way in, §5 grades `CYRIS_OPERATOR_TENANT` and the route, and §6 describes it.
* `docs/hosted-tenants.md` has the procedure: it reads or changes one tenant from the CLI.
* The task card is `hosted-operator-tenant-access` in the owner's Obsidian vault. Its callers
  were `src/cyris/entrypoints/cli.py`, `scripts/prune_archive.py` and
  `src/cyris/diagnostics/doctor.py`, which reached no tenant before this change.
