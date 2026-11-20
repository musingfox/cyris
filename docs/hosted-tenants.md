# Hosted tenants on the trial account

This deploys the hosted Worker `cyris-hosted` (`wrangler.hosted.toml`) to the **trial** account.
It then registers two tenants, `alpha` and `bravo`, in the Worker's registry, seeds each, and
runs one digest for each.
Last, it checks that each tenant's `digest_runs` and `digests` hold only that tenant's own rows,
and that each tenant's latest page, `/t/<tenant>/`, answers 200 and is that tenant's own issue.
Why each tenant has a SQLite Durable Object of its own is
[ADR-0028](decisions/0028-one-sqlite-durable-object-per-hosted-tenant.md), and why its pages are
stored there too is [ADR-0029](decisions/0029-a-hosted-tenants-pages-live-in-its-durable-object.md).
What the hosted Worker is, and what it does not do yet, is in `docs/architecture.md` §6, *Hosted
mode is a separate Worker*. Why the tenant list is a registry Durable Object and not a line of
config is [ADR-0030](decisions/0030-the-operator-registry-is-one-sqlite-durable-object.md).
Why each tenant's RSS is polled hourly by this Worker into a buffer in the tenant's own object,
and what that costs against Cloudflare's limits, is
[ADR-0031](decisions/0031-a-hosted-tenants-rss-is-polled-by-the-hosted-worker.md).
Why each tenant's schedule and run cap live in the registry, and why every run waits in a queue
there, is [ADR-0032](decisions/0032-hosted-runs-wait-in-a-queue-the-registry-holds.md).

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
`.env.hosted`, which is gitignored and has mode 600. `secrets` sets the Worker's
`CYRIS_STORE_HOSTED_DOMAIN` from it, so you never type the domain.

## 0. Before you start

You need:

- Docker running, because wrangler builds the container image locally;
- `bun`, `python3` (3.11 or later) and `curl`;
- Workers Paid on the trial account, because the Worker runs containers.

```bash
bun install --frozen-lockfile
(cd workers/rss && bun install --frozen-lockfile)
bun x wrangler login
```

The second install is for the hosted Worker's RSS poll. It uses `workers/rss`'s feed parser, and
that parser's `fast-xml-parser` is installed only under `workers/rss`. Without it the deploy
stops with "Could not resolve fast-xml-parser".

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

The dashboard labels in this step, and the ones in steps 6, 7 and 9, were written from memory of
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
each one as a secret of `cyris-hosted` on stdin. It also sets `CYRIS_STORE_HOSTED_DOMAIN` to the
host of the `HOSTED_URL` that step 1 kept, so it refuses to start without one. Every link a
tenant's run gives points at that host. No other secret is needed. A hosted container is handed
only the model keys, the account id and the hosted domain. It gets no D1 token, no Pages project
and no Worker URL (`hostedContainerEnv`). The Worker starts no run before the hosted domain is
set.

## 4. Wait for the container application

```bash
scripts/hosted.sh containers
```

A first deploy can take several minutes before an instance can start. Wait until
`cyris-hosted-tenantcontainer` is listed and ready.

## 5. Register, seed and schedule both tenants

```bash
scripts/hosted.sh register
scripts/hosted.sh seed
scripts/hosted.sh schedule alpha 08:00,20:00 UTC
scripts/hosted.sh schedule bravo 09:00,21:00 UTC
```

`register` runs `POST /hosted/tenants/<tenant>/register` for `alpha`, then `bravo`. Each answer is
the tenant's row in the registry. A tenant registered for the first time is `"active"`. One
already registered keeps its status and its `created_at`, so running `register` again does not
resume a tenant you paused. The tenants are the two the seed names
(`workers/app/src/hosted_seed.json`).

The seed route seeds only a registered tenant: for any other it answers 404 and writes nothing,
and so do the run and report routes. `seed` runs `POST /hosted/tenants/<tenant>/seed` for `alpha`,
then `bravo`. Each seed writes three things into the tenant's own database:

- `schema.sql`, then `hosted_schema.sql`, which adds the tenant's `hosted_pages` table and its
  RSS buffer, `articles`;
- every required setting: provider `anthropic`, model `claude-haiku-5-5`, digests at 08:00 and
  20:00 UTC, language `en`, and no notification target;
- four RSS feeds from the onboarding catalog. No feed is shared between the two tenants
  (`workers/app/src/hosted_seed.json`).

Each answer names the tenant, how many settings it wrote, its sources and how many tables it
has. Seeding again writes the same rows back and leaves other rows alone.

To stop a tenant's runs without deleting anything, register it again as paused. The script has
no subcommand for it; send `POST /hosted/tenants/<tenant>/register?status=paused` with the
service token's two headers, and `?status=active` to resume it. Only a named status changes a
registered tenant's status. The tick skips a paused tenant,
and its run route answers 409. Its seed and report routes still answer.

`schedule TENANT TIMES TIMEZONE [DAILY_RUN_CAP]` runs
`POST /hosted/tenants/<tenant>/schedule?times=...&timezone=...` for one tenant. It sets the hours
the hourly tick wakes that tenant at, the zone they are hours of, and, when you name one, its
daily run cap. The registry holds all three, and the tick reads them nowhere else: the schedule
the seed wrote into the tenant's own database is not what wakes it. `TIMES` is two whole hours,
such as `07:00,19:00`. `TIMEZONE` is an IANA zone name such as `Asia/Taipei`, never an offset
such as `+08:00`, which the container cannot read and the route refuses. The cap is the most runs
the tenant may start in any 24 hours, from 1 to 24. A new tenant's cap is 6, and scheduling a
tenant again without naming one keeps the cap it has. The answer is the tenant's registry row,
and `"settings": true` once the route has copied the hours and the zone into the tenant's own
settings. `false` means the copy could not be written, and the Worker logs why
(`schedule copy not written`). For a tenant not yet seeded that is expected: its seed writes the
copy instead. A schedule the registry does not take answers 400 with the reason. Until a
tenant has a schedule, the tick polls its feeds and queues no run, and its run route answers
409. Give each tenant different hours to see each one woken at its own.

On a deployment whose tenants were registered before the registry held schedules, run `schedule`
for each tenant after you deploy. Until you do, the tick polls its feeds and queues none of its
runs.

## 6. Poll each tenant's feeds, and measure a tick

Every hour, on the hour, the Worker's tick polls each active tenant's `rss` sources into that
tenant's own `articles`. It does this before it decides whether that tenant's digest is due, so
a run started in that tick reads that hour's entries. A paused tenant is not polled. To poll now,
without waiting for the hour:

```bash
scripts/hosted.sh poll
```

This runs `POST /hosted/tenants/<tenant>/poll` for each tenant. Each answer is that one poll:
`feeds`, `fetches` (the feed requests it made), `entries`, `fresh` (the entries dated inside the
8-day window), `written` (the new rows), and `failures`, one line per feed that failed. A tenant
whose poll failed answers 502 with the reason. A paused tenant answers 409.

To measure one real tick, run `scripts/hosted.sh tail` and wait for the top of an hour.
Then read these lines in Workers Logs (Workers & Pages > `cyris-hosted` > Logs, *label
unverified*):

- one `hosted tick` line from the scheduled invocation, with `tenants`, `polled`,
  `failed_polls`, `feeds`, `feed_fetches` (the feed requests the tenants' polls made),
  `max_tenant_fetches` (the most any one tenant's poll made), `written`, `queued` (the runs it
  queued), `object_calls` (the Durable Object calls the tick made itself) and `wall_ms`;
- one `rss poll` line per tenant, with its own counts;
- the CPU time and wall time of each invocation, which Workers Logs puts in the invocation log.
  That covers the scheduled invocation and each tenant's `TenantDb` invocation, where the
  fetching and parsing happen. `wrangler tail` may show the same, *unverified*.

The scheduled invocation's subrequests are its `object_calls`, if Cloudflare counts a Durable
Object call as one. Each tenant's poll invocation spends its `fetches`, plus any redirect a feed
answers with. The limits these are measured against, and the arithmetic for 30 tenants, are in
ADR-0031.

## 7. Run one digest for each

```bash
scripts/hosted.sh run morning
```

This runs `POST /hosted/tenants/<tenant>/run?period=morning` for each tenant. Each answer is
HTTP 202 with `{"queued": ...}`, the run's row in the registry's queue, dated today in the
tenant's zone. The registry starts the queued runs, at most `max_instances` (2) at once and one
per tenant. Each tenant's container starts with the gateway to that tenant's database registered
before `start()`. A start that gets no container instance waits in the queue and is started
again, up to four times; after that, the run is recorded as `failed`. A run of an issue that is
already queued or running answers 409, and a tenant past its daily cap answers 429; neither
queues anything.

Each run reads its RSS from the tenant's own buffer, `articles`, which step 6 and the hourly tick
fill. It does not poll its feeds itself. A run started before any poll finds the buffer empty and
has nothing to digest, which is why step 6 comes first. Each run writes its issue into the
tenant's `digests`, and its digest page and raw page into the tenant's own `hosted_pages`. No
Pages project is touched. Runs notify nobody yet. Allow about five minutes. To watch the Worker side of it:

```bash
scripts/hosted.sh tail
```

Stop it with Ctrl-C. The containers' own output is in Workers Logs (Workers & Pages >
`cyris-hosted` > Logs, *label unverified*). The queue logs `run queued`, `run started`,
`run done`, `run failed`, `run lost` and `run queued again` lines, each with the tenant, the
period and the date.

## 8. Check that each tenant holds only its own rows

```bash
scripts/hosted.sh check
```

This reads `GET /hosted/tenants/<tenant>/report` for each tenant, then fetches that tenant's
latest page, `GET /t/<tenant>/`, with the same service token. Each report has the tenant's
tables with their row counts, its sources, the sources of its stored articles, its
`digest_runs`, its `digests`, the path and SHA-256 of the page its site root serves, and its
`runs`: the registry's last 20 runs of the tenant, each with its period, date, origin, state,
attempts, start and end times, and why it ended.
`scripts/hosted_check.py` judges the reports and the pages and prints one line per tenant. It
exits 1 when any tenant fails. A tenant passes when all of these hold:

- its database holds at least one finished, non-dry `ok` run;
- its database holds at least one issue;
- every article it stored came from one of its own sources;
- it shares no source with the other tenant;
- its latest page answered 200;
- the page's `cyris-issue` receipt names the issue the tenant saved last;
- the page's SHA-256 is the one the tenant's own database reports, and not the other tenant's.
  The type size the Worker may add to the page is left out of the hash.

`PASS alpha: 1 run(s), 1 issue(s), only its own sources, and its latest page is its own newest
issue`, and the same for `bravo`, is the milestone. The reports and the pages are kept in the
temporary directory the script names. Keep them with the result.

A tenant that has no `ok` run yet may still be running, or waiting in the queue: its report's
`runs` says which. Check again a few minutes later. A run that ended in `error` has its reason in
that tenant's Workers Logs. A run the registry recorded as `failed` has its reason in `runs`. A
`lost` row is a run whose container never reported its stop within 20 minutes; the row above it
is the same issue, queued again.

The hourly tick also queues each active tenant's run at the hours step 5 scheduled. A check
after one of those hours counts two runs, and each is still that tenant's own. To see each tenant
woken at its own hours, run `check` after both tenants' hours have passed. In each report, every
`digest_runs` row's `finished_at` falls a few minutes after one of that tenant's hours, and its
`runs` row for the hour has `origin` `tick` and state `done`.

### Reading a tenant's pages in a browser (optional)

The check signs in with the service token. To read the pages yourself, add a policy to the
step-2 Access application whose action is Allow *(label unverified)* and that includes your own
email. Then open each of these, with `HOSTED_URL` from `.env.hosted`:

- `HOSTED_URL/t/alpha/`: alpha's latest issue;
- `HOSTED_URL/t/alpha/<date>-<period>`: one issue, from the archive at
  `HOSTED_URL/t/alpha/index.html`;
- `HOSTED_URL/t/alpha/<date>-<period>-raw`: that issue's raw page.

The same paths under `/t/bravo/` are bravo's. Until tenant sessions land, Access admits only the
operator, so these are your pages to check, not a tenant's sign-in. The vote buttons and the
Settings link stay hidden, because the hosted Worker has no `/api/vote` or `/settings` yet.

## 9. Tear down (when you are done)

```bash
scripts/hosted.sh delete
```

This asks for the account and a `yes`, then deletes `cyris-hosted` and its Durable Objects,
tenant databases and the registry included. Afterwards, check the dashboard's Containers page (*label
unverified*) for a leftover `cyris-hosted-tenantcontainer` application, and delete the Access
application from step 2.
