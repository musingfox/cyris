# Hosted tenants on the trial account

This deploys the hosted Worker `cyris-hosted` (`wrangler.hosted.toml`) to the **trial** account.
It then registers two tenants, `alpha` and `bravo`, in the Worker's registry, seeds each, and
runs one digest for each.
Last, it checks that each tenant's `digest_runs` and `digests` hold only that tenant's own rows.
Why each tenant has a SQLite Durable Object of its own is
[ADR-0028](decisions/0028-one-sqlite-durable-object-per-hosted-tenant.md). What the hosted Worker
is, and what it does not do yet, is in `docs/architecture.md` §6, *Hosted mode is a separate
Worker*. Why the tenant list is a registry Durable Object and not a line of config is
[ADR-0030](decisions/0030-the-operator-registry-is-one-sqlite-durable-object.md).

`cyris-hosted` is a separate Worker from `cyris-app`. Nothing here deploys or changes
`wrangler.toml`, which a push to main deploys to production (ADR-0024).

Every command runs, as pasted, from the root of a checkout of this repository. `scripts/hosted.sh` asks for each value by name:

- `ACCOUNT_ID`: the trial account's Account ID (dashboard > Account home > Account ID). You
  type it for every command that runs wrangler. It is never stored, and it is never read from
  your environment. Before `deploy`, `secrets` and `delete`, the script prints the id and the
  account name `wrangler whoami` gives for it. It does nothing until you type `yes`.
- `CYRIS_ACCESS_TEAM_DOMAIN` and `CYRIS_ACCESS_AUD`: the Access team domain, and the AUD tag of
  the Access application from step 2.
- `ANTHROPIC_API_KEY`: the key the tenants' runs call `claude-haiku-5-5` with. The seed fixes
  that model for both tenants.
- `CF-Access-Client-Id` and `CF-Access-Client-Secret`: a service token the step-2 application
  allows. The seed, run and check steps sign in with it.

The secrets are read without echo each time, and they are never written down. They reach
wrangler and curl on stdin, never on a command line. The one value the script keeps is the
Worker's URL, `HOSTED_URL`, which `deploy` reads from wrangler's output. It goes in
`.env.hosted`, which is gitignored and has mode 600.

## 0. Before you start

You need:

- Docker running, because wrangler builds the container image locally;
- `bun`, `python3` (3.11 or later) and `curl`;
- Workers Paid on the trial account, because the Worker runs containers.

```bash
bun install --frozen-lockfile
bun x wrangler login
```

Log in as a user who can see the trial account: that login is the credential every wrangler call of the script
acts as, since none of them reads `.env`.

## 1. Deploy

```bash
scripts/hosted.sh deploy
```

This runs `wrangler deploy --config wrangler.hosted.toml --env-file /dev/null`, after you
confirm the account. It deploys the Worker, its three Durable Object classes
(`TenantContainer`, `TenantDb` and `TenantRegistry`) and the container application
`cyris-hosted-tenantcontainer`. It never reads `.env`. A first deploy's registry is empty, so
until step 5 the Worker has no tenant: the hourly tick starts nothing, and every tenant route
answers 404.

Until step 3 sets the Access secrets, every request to the Worker is answered with 403.

## 2. Put Access in front of it (dashboard)

The Worker checks the Access JWT itself (ADR-0015), so Access must sign a request in before the
Worker sees it.

The dashboard labels in this step, and the two in steps 6 and 8, were written from memory of
the dashboard and have not been checked against it. Each is marked *(label unverified)*. What
each step has to achieve is what counts; find it under whatever name the dashboard now uses.

1. Workers & Pages > `cyris-hosted` > Settings > Domains & Routes *(label unverified)*: turn on
   Cloudflare Access for the `workers.dev` route. This creates an Access application for that
   hostname. Open it from there and note its Application Audience (AUD) Tag *(label
   unverified)*.
2. In Zero Trust > Access > Service credentials *(label unverified)*, create a service token, or
   reuse the trial's. Note its Client ID and Client Secret. The secret is shown once.
3. In the step-1 application, add a policy whose action is Service Auth *(label unverified)* and
   that includes that token.
4. Note the team domain, `<team>.cloudflareaccess.com` (Zero Trust > Settings, *label
   unverified*).

## 3. Set the Worker's secrets

```bash
scripts/hosted.sh secrets
```

This asks for `CYRIS_ACCESS_TEAM_DOMAIN`, `CYRIS_ACCESS_AUD` and `ANTHROPIC_API_KEY`, and sets
each one as a secret of `cyris-hosted` on stdin. No other secret is needed. A hosted container is
handed only the model keys and the account id; it gets no D1 token, no Pages project and no
Worker URL (`hostedContainerEnv`).

## 4. Wait for the container application

```bash
scripts/hosted.sh containers
```

A first deploy can take several minutes before an instance can start. Wait until
`cyris-hosted-tenantcontainer` is listed and ready.

## 5. Register and seed both tenants

```bash
scripts/hosted.sh register
scripts/hosted.sh seed
```

`register` runs `POST /hosted/tenants/<tenant>/register` for `alpha`, then `bravo`. Each answer is
the tenant's row in the registry, with `"status": "active"`. Registering again leaves the
tenant active and keeps its `created_at`. The tenants are the two the seed names
(`workers/app/src/hosted_seed.json`).

The seed route seeds only a registered tenant: for any other it answers 404 and writes nothing,
and so do the run and report routes. `seed` runs `POST /hosted/tenants/<tenant>/seed` for `alpha`,
then `bravo`. Each seed writes three things into the tenant's own database:

- `schema.sql`;
- every required setting: provider `anthropic`, model `claude-haiku-5-5`, digests at 08:00 and
  20:00 UTC, language `en`, and no notification target;
- four RSS feeds from the onboarding catalog. No feed is shared between the two tenants
  (`workers/app/src/hosted_seed.json`).

Each answer names the tenant, how many settings it wrote, its sources and how many tables it
has. Seeding again writes the same rows back and leaves other rows alone.

To stop a tenant's runs without deleting anything, register it again as paused. The script has
no subcommand for it; send `POST /hosted/tenants/<tenant>/register?status=paused` with the
service token's two headers, and `?status=active` to resume it. The tick skips a paused tenant,
and its run route answers 409. Its seed and report routes still answer.

## 6. Run one digest for each

```bash
scripts/hosted.sh run morning
```

This runs `POST /hosted/tenants/<tenant>/run?period=morning` for each tenant. Each tenant's
container starts with the gateway to that tenant's database registered before `start()`. A
start made while both instances are held is retried for up to 90 seconds. Each answer is
`{"started": true, "tenant": ...}`, or `"started": false` when that tenant's run is already up.

Runs poll their feeds directly. They do not publish and do not notify: the finished issue stays
in the tenant's `digests` table. Allow about five minutes. To watch the Worker side of it:

```bash
scripts/hosted.sh tail
```

Stop it with Ctrl-C. The containers' own output is in Workers Logs (Workers & Pages >
`cyris-hosted` > Logs, *label unverified*).

## 7. Check that each tenant holds only its own rows

```bash
scripts/hosted.sh check
```

This reads `GET /hosted/tenants/<tenant>/report` for each tenant. Each report has the tenant's
tables with their row counts, its sources, the sources of its stored articles, its
`digest_runs` and its `digests`. `scripts/hosted_check.py` judges the reports and prints one line
per tenant. It exits 1 when any tenant fails. A tenant passes when all four of these hold:

- its database holds at least one finished, non-dry `ok` run;
- its database holds at least one issue;
- every article it stored came from one of its own sources;
- it shares no source with the other tenant.

`PASS alpha: 1 run(s), 1 issue(s), only its own sources`, and the same for `bravo`, is the
milestone. The reports are kept in the temporary directory the script names. Keep them with the
result.

A tenant that has no `ok` run yet may still be running. Check again a few minutes later. A run
that ended in `error` has its reason in that tenant's Workers Logs.

The hourly tick also runs each active registered tenant at 08:00 and 20:00 UTC. A check after one of those hours
counts two runs, and each is still that tenant's own.

## 8. Tear down (when you are done)

```bash
scripts/hosted.sh delete
```

This asks for the account and a `yes`, then deletes `cyris-hosted` and its Durable Objects,
tenant databases and the registry included. Afterwards, check the dashboard's Containers page (*label
unverified*) for a leftover `cyris-hosted-tenantcontainer` application, and delete the Access
application from step 2.
