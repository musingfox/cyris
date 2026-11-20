# Hosted tenants on the trial account

This deploys the hosted Worker `cyris-hosted` (`wrangler.hosted.toml`) to the **trial** account.
It then registers two tenants, `alpha` and `bravo`, in the Worker's registry, seeds and schedules
each, and queues one digest for each.
It checks that each tenant's `digest_runs` and `digests` hold only that tenant's own rows, and
that each tenant's latest page, the one `/t/<tenant>/` serves its readers, answers 200 and is that
tenant's own issue. Then it gives each tenant a newsletter address and invites a reader.
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
Why a failed run is reported to you, the operator, on a Discord webhook of your own, and why the
reader is never shown its error, is
[ADR-0035](decisions/0035-a-hosted-runs-alerts-go-to-the-operator-through-the-hosted-worker.md).
Why each tenant's newsletters arrive at a random address of its own, into its own object, is
[ADR-0034](decisions/0034-a-hosted-tenants-newsletters-arrive-at-a-random-address.md).
Step 10 invites a reader, who signs in with a link mailed to them and reads only their own
tenant's digest. Why readers sign in that way and not with Access, and why Access stays in front
of the operator's routes only, is
[ADR-0033](decisions/0033-hosted-readers-sign-in-with-a-mailed-link.md).

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
- `CYRIS_HOSTED_OPERATOR_WEBHOOK`: a Discord webhook URL of your own (Server Settings >
  Integrations > Webhooks, *label unverified*). A tenant's failed run is posted there with the
  tenant's id. The run posts its own failures with the raw error: it raised, it fetched nothing
  while a feed failed, or it stopped before its digest began. The Worker posts a start that
  failed, a container that exited non-zero or was killed before it reported, and a queued run
  whose container never reported its stop. Step 7 lists what is not posted.
- `CF-Access-Client-Id` and `CF-Access-Client-Secret`: a service token the step-2 application
  allows. The seed, run and check steps sign in with it.
- `CYRIS_HOSTED_MAIL_FROM`: the address the readers' sign-in mail is sent from. It must be on the
  domain you onboard to Email Sending in step 0.

The secrets are read without echo each time, and they are never written down. They reach
wrangler and curl on stdin, never on a command line. The one value the script keeps is the
Worker's URL, `HOSTED_URL`, which `deploy` reads from wrangler's output. It goes in
`.env.hosted`, which is gitignored and has mode 600. `secrets` sets the Worker's
`CYRIS_STORE_HOSTED_DOMAIN` from it, so you never type the domain.

## 0. Before you start

You need:

- Docker running, because wrangler builds the container image locally;
- `bun`, `python3` (3.11 or later) and `curl`;
- Workers Paid on the trial account, because the Worker runs containers. Email Sending to an
  address the account has not verified needs it too;
- for step 10, a domain on Cloudflare DNS in the trial account, onboarded to Email Sending
  (Email > Email Sending > Onboard Domain, *label unverified*). Onboarding adds SPF, DKIM, DMARC
  and bounce MX records to the domain. Until a domain is onboarded, the sign-in mail reaches only
  the account's verified destination addresses, and a new account starts with a daily quota that
  Cloudflare does not publish. Workers Paid includes 3,000 mails a month
  ([limits](https://developers.cloudflare.com/email-service/platform/limits/),
  [pricing](https://developers.cloudflare.com/email-service/platform/pricing/));
- for step 10, an inbox outside the account that is not one of its verified destination
  addresses, to check that a link reaches a stranger.

```bash
bun install --frozen-lockfile
(cd workers/rss && bun install --frozen-lockfile)
(cd workers/newsletter && bun install --frozen-lockfile)
bun x wrangler login
```

The second install is for the hosted Worker's RSS poll. It uses `workers/rss`'s feed parser, and
that parser's `fast-xml-parser` is installed only under `workers/rss`. Without it the deploy
stops with "Could not resolve fast-xml-parser". The third is for its newsletter handler, which
parses mail with `workers/newsletter`'s `postal-mime`; without it the deploy stops with "Could
not resolve postal-mime".

Log in as a user who can see the trial account: that login is the credential every wrangler call of the script
acts as, since none of them reads `.env`.

## 1. Deploy

```bash
scripts/hosted.sh deploy
```

This runs `wrangler deploy --config wrangler.hosted.toml --env-file /dev/null`, after you
confirm the account. It deploys the Worker, its three Durable Object classes
(`TenantContainer`, `TenantDb` and `TenantRegistry`), its `MAIL` Email Sending binding and the
container application `cyris-hosted-tenantcontainer`. It never reads `.env`. A first deploy's registry is empty, so
until step 5 the Worker has no tenant: the hourly tick queues nothing, and every tenant route
answers 404.

Until step 3 sets the Access secrets, every operator request to the Worker is answered with 403.

## 2. Put Access in front of the operator's routes (dashboard)

The Worker checks the Access JWT itself on every operator route, `/hosted/...` (ADR-0015), so
Access must sign those requests in before the Worker sees them. A tenant's pages, `/t/...`, and
the sign-in, `/login` and `/logout`, take a reader's session instead and no Access JWT
(ADR-0033), so Access must not stand in front of them: a reader would meet Access's own login.

The dashboard labels in this step, and the ones in steps 0, 6, 7, 9, 10 and 11, were written
from memory of the dashboard and have not been checked against it. Each is marked *(label
unverified)*. What each step has to achieve is what counts; find it under whatever name the
dashboard now uses.

1. In Zero Trust > Access > Applications *(label unverified)*, add a self-hosted application
   whose destination is the Worker's `workers.dev` hostname with the path `hosted`, so it covers
   `/hosted/...` and nothing else. Do not use the Worker's own "Protect this Worker behind
   Access" switch: Cloudflare's
   [Workers and Access](https://developers.cloudflare.com/workers/configuration/cloudflare-access/)
   page says it protects every domain of the Worker, `/t/` and `/login` included. A trial that
   already has that switch on turns it off, or narrows its application to the same path. Access's
   [application paths](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/app-paths/)
   page says a subpath with no rule of its own inherits its parent's, and that `hosted/*` would
   not cover `/hosted` itself; nothing the operator calls is `/hosted` alone. Note the
   application's Application Audience (AUD) Tag *(label unverified)*. A path Access leaves
   uncovered is still refused by the Worker: any `/hosted/` request without a valid JWT answers
   403, and so does every path that is neither `/hosted/`, `/t/` nor the sign-in.
2. In Zero Trust > Access > Service credentials *(label unverified)*, create a service token, or
   reuse the trial's. Note its Client ID and Client Secret. The secret is shown once.
3. In the step-1 application, add a policy whose action is Service Auth *(label unverified)* and
   that includes that token.
4. Note the team domain, `<team>.cloudflareaccess.com` (Zero Trust > Settings, *label
   unverified*).

After step 3, check that Access covers exactly the operator's routes, with `HOSTED_URL` from
`.env.hosted`. Neither request signs in:

```bash
curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\n' "$HOSTED_URL/hosted/tenants/alpha/report"
curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\n' "$HOSTED_URL/t/alpha/"
```

The first must be Access's redirect to `<team>.cloudflareaccess.com`, or a 403 from Access. The
second must be the Worker's own `302` to `HOSTED_URL/login`. If the second goes to the team
domain too, Access still covers `/t/`, and readers will meet its login instead of the sign-in.

## 3. Set the Worker's secrets

```bash
scripts/hosted.sh secrets
```

This asks for `CYRIS_ACCESS_TEAM_DOMAIN`, `CYRIS_ACCESS_AUD`, `ANTHROPIC_API_KEY` and
`CYRIS_HOSTED_OPERATOR_WEBHOOK` and `CYRIS_HOSTED_MAIL_FROM`, and sets each one as a secret of
`cyris-hosted` on stdin. The webhook is read without echo, because its URL is its credential, and
it must start with `https://`. The sender is not secret; it is set the same way so that
`wrangler.hosted.toml` names no deployment's domain. It also sets `CYRIS_STORE_HOSTED_DOMAIN` to the
host of the `HOSTED_URL` that step 1 kept, so it refuses to start without one. Every link a
tenant's run gives points at that host. No other secret is needed. A hosted container is handed
only the model keys, the account id and the hosted domain. It gets no D1 token, no Pages project,
no Worker URL and not the webhook (`hostedContainerEnv`). A run posts its alerts to the Worker,
which forwards them to the webhook. The Worker starts no run before the hosted domain is
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
and its run route answers 409. Its other routes still answer, and it still takes mail at its
newsletter address (step 9).

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
per tenant. Each tenant's container starts with two gateways registered before `start()`, one to
that tenant's database and one that carries its alerts to your webhook. A start that gets no
container instance waits in the queue and is started again, up to four times; after that, the run
is recorded as `failed` and posted to you once. A run of an issue that is
already queued or running answers 409, and a tenant past its daily cap answers 429; neither
queues anything.

Each run reads its RSS from the tenant's own buffer, `articles`, which step 6 and the hourly tick
fill. It does not poll its feeds itself. A run started before any poll finds the buffer empty and
has nothing to digest, which is why step 6 comes first. Each run writes its issue into the
tenant's `digests`, and its digest page and raw page into the tenant's own `hosted_pages`. No
Pages project is touched. A run sends no digest notification. These are posted to your webhook,
each on one line `[<tenant>] <headline>` and then its detail in a code block:

- A run that raises reads `Digest run failed: <issue> <zone>`, with the raw error.
- A run that fetches nothing while a feed failed reads `Digest run fetched nothing`.
- A run that stops at its settings, at its wiring, or at a database that answered an error reads
  `Digest run could not start`. This holds for a scheduled, catch-up or manual run.
- From the Worker, a start that failed reads `Digest run could not start`, with the period, the
  date, the run's state and starts, and the error. The queue sends it once it stops trying: at
  once for a start error such as no hosted domain, and after the fourth start that found no free
  instance, since each of the first three goes back in the queue and sends nothing.
- From the Worker, a container that exited non-zero or was killed reads `Digest run stopped
  before it reported`, with its exit code. That covers an OOM kill, or the 15-minute
  inactivity cap. It is not sent when the run already posted why it ended.
- From the Worker, a run whose container reported no stop within 20 minutes reads `Digest run lost:
  no stop report, queued again`, or `Digest run lost on its last start` after its fourth start.
  It is not sent when the run, or the Worker, already posted why that start ended.

Not posted: a tenant whose schedule the tick cannot read, which gets no run; an alert your
webhook refused, a `429` included, which leaves an `operator alert not sent` line; and a dry run.
The tenant's own channels, if its settings name any, get the run's own alerts without the error.
Allow about five minutes. To watch the Worker side of it:

```bash
scripts/hosted.sh tail
```

Stop it with Ctrl-C. The containers' own output is in Workers Logs (Workers & Pages >
`cyris-hosted` > Logs, *label unverified*). The queue logs `run queued`, `run started`,
`run done`, `run failed`, `run lost` and `run queued again` lines, each with the tenant, the
period and the date. Every line a tenant's container writes starts with
`tenant=<tenant> `, and the egress probe's JSON line has a `tenant` field. Search for
`tenant=alpha` to read one tenant's run. Each alert the Worker forwards leaves an `operator alert
sent` line, or `operator alert not sent` with its reason, naming the tenant.

## 8. Check that each tenant holds only its own rows

```bash
scripts/hosted.sh check
```

This reads `GET /hosted/tenants/<tenant>/report` for each tenant, then fetches that tenant's
latest page with the same service token, through `GET /hosted/tenants/<tenant>/page`. That route
answers the page `/t/<tenant>/` serves the tenant's readers. `/t/` itself takes a reader's session,
which the service token is not. Each report has the tenant's
tables with their row counts, its sources, the sources of its stored articles, its
`digest_runs`, its `digests`, the path and SHA-256 of the page its site root serves, and its
`runs`: the registry's last 20 runs of the tenant, each with its period, date, origin, state,
attempts, start and end times, and why it ended, and its `newsletters`, the mail queued at its
address by sender and subject (step 9).
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
your webhook's alert and in that tenant's Workers Logs. A run the registry recorded as `failed`
has its reason in `runs`. A `lost` row is a run whose container never reported its stop within
20 minutes; the row above it is the same issue, queued again.

The hourly tick also queues each active tenant's run at the hours step 5 scheduled. A check
after one of those hours counts two runs, and each is still that tenant's own. To see each tenant
woken at its own hours, run `check` after both tenants' hours have passed. In each report, every
`digest_runs` row's `finished_at` falls a few minutes after one of that tenant's hours, and its
`runs` row for the hour has `origin` `tick` and state `done`.

Do not add a reader, or yourself, to the step-2 Access application to read a tenant's pages.
Access no longer opens `/t/`, and any identity that application admits can call every operator
route. To read a tenant's pages in a browser, sign in as its reader (step 10).

### Reading or changing one tenant from the CLI (optional)

A `cyris` command that reads or changes rows can run on one tenant's database. Name the tenant
with `--tenant`, before the command. Each query the command makes goes to
`POST /hosted/tenants/<tenant>/query` on the
hosted Worker. It signs in with the same service token the script uses. Why it works this way is
[ADR-0036](decisions/0036-the-operator-reaches-a-tenant-through-an-access-checked-query-route.md).
This needs the Python environment, `uv sync`, once.

```bash
export CYRIS_STORE_HOSTED_DOMAIN="$(sed -n 's|^HOSTED_URL=https://||p' .env.hosted)"
read -r -p "CF-Access-Client-Id: " CYRIS_ACCESS_CLIENT_ID && export CYRIS_ACCESS_CLIENT_ID
read -rs -p "CF-Access-Client-Secret: " CYRIS_ACCESS_CLIENT_SECRET && echo && export CYRIS_ACCESS_CLIENT_SECRET
uv run cyris --tenant alpha doctor
uv run cyris --tenant alpha sources list
```

`doctor` reports on alpha alone: its sources, settings, model, articles, feed health and last
run. It leaves your own machine's files, Workers and keys out, because the hosted Worker wires a
tenant's run. A command that writes, such as `uv run cyris --tenant alpha articles accept <url>`,
writes into alpha's database and no other.

Seven commands are refused with `--tenant`: `run`, `regenerate`, `llm-compare`,
`articles score`, `ingest`, `promote-sync` and `triage-ui`. The hosted Worker starts and
schedules a tenant's runs and serves its pages. One of these commands, run here, would race the
hourly tick on the tenant's tables, because this machine's run lock is a local file. It would
also spend your own model keys outside the tenant's budget. Each one exits 2 with that reason
before it sends anything. To run a tenant's digest, use step 7's `scripts/hosted.sh run`, which
calls `POST /hosted/tenants/<tenant>/run`.

Export the two values in the shell, as above. A command run from the root of this checkout also
reads `.env`, but only for a name not already set. If `.env` holds production's service token and
you skip the `read` lines, that token is sent instead, and the trial's Access refuses it. The
refusals:

- an empty or malformed tenant id, such as `--tenant ""`, is refused at the option, so the
  command never falls back to your own store;
- without `CYRIS_ACCESS_CLIENT_ID` and `CYRIS_ACCESS_CLIENT_SECRET`, the command stops and names
  them, and nothing is sent;
- a token Access or the Worker refuses stops the command at the first query, with the HTTP
  status and its reason. It is never retried;
- a tenant the registry does not hold is answered 404, as on every other tenant route. A paused
  tenant is still answered.

## 9. Newsletters: the inbound domain and each tenant's address

Each tenant gets a random address, 26 characters of base32 that say nothing about the tenant.
Email Routing on an inbound domain sends every address to `cyris-hosted`, and its
`email()` handler stores each message only in the tenant whose address it came to. A message to
an address no tenant holds is dropped in silence: it is accepted and discarded, and the sender
gets no bounce. So is one to a revoked address, a subaddress, one over 5 MiB, and one to a tenant
with 200 messages waiting. No sender can tell from the answer whether an address exists. Each
tenant's run reads its own queue, ingests what a newsletter source matches, and deletes it. A
message no source matches waits 8 days for its source to be added (9.3).

### 9.1 Route an inbound domain to the Worker (dashboard)

Email Routing needs a zone on the trial account: it cannot run on `*.workers.dev`. A catch-all
takes every address of its domain, so use a domain, or a subdomain, that receives nothing else.

The facts below come from Cloudflare's Email Routing pages as they read on 2026-11-14 (*Email
Routing addresses*, sections *Catch-all rule* and *Configure rules with Wrangler*). They have not
been checked against the trial account. Each label is *(label unverified)*.

1. Email > Email Routing *(label unverified)* on the zone: turn Email Routing on, and add the MX
   and TXT records it proposes.
2. Routing rules > Catch-all address *(label unverified)*: set the action to Send to a Worker
   *(label unverified)*, choose `cyris-hosted`, and turn the rule on. Deploy the Worker first
   (step 1), since the rule names a deployed Worker.
3. Leave `wrangler.hosted.toml` alone. Wrangler can write a catch-all only on an apex domain
   (`addresses = ["*@<domain>"]`), and the rule belongs to the zone, not to the Worker.

Cloudflare's documentation says a catch-all rule exists only on an apex domain. A subdomain takes
literal rules only, one per address, and a domain holds at most 200 rules
([subdomains](https://developers.cloudflare.com/email-service/configuration/subdomains/),
[limits](https://developers.cloudflare.com/email-service/platform/limits/)). Which of the two
the service uses, a dedicated apex domain with this catch-all or a subdomain with a literal rule
per address made over the API, is the product owner's open choice (ADR-0034). Until it is made,
set this up on an apex domain kept for the trial.

### 9.2 Give each tenant an address

```bash
scripts/hosted.sh address rotate
```

This runs `POST /hosted/tenants/<tenant>/rotate-address` for `alpha`, then `bravo`. Each answer
is `{"tenant", "address", "created_at", "revoked"}`. The tenant's address is
`<address>@<the inbound domain>`. A rotation revokes every address the tenant held before, at
once. `scripts/hosted.sh address` shows each tenant's current address, and `scripts/hosted.sh
address revoke` revokes it and issues none. Only the registry holds the map from address to
tenant, and no run container can read it. The answers are printed and their files deleted, so
keep the addresses where you keep the service token.

### 9.3 Check that one issue reaches both tenants' digests, and an unknown address reaches none

1. Give both tenants a newsletter source for the mailbox you will send from. A run ingests an
   issue only for a source whose sender matches:

   ```bash
   scripts/hosted.sh newsletter-source alpha "Test Letter" you@your-mailbox.example
   scripts/hosted.sh newsletter-source bravo "Test Letter" you@your-mailbox.example
   ```

   Each runs `POST /hosted/tenants/<tenant>/newsletter-source?name=&from=`, and a fourth
   argument adds `&homepage=`. Each answer is the source row the tenant now holds: type
   `newsletter`, `email_match` `from:<address>`, lane `read`. A tenant must be seeded first
   (step 5), and a name one of its RSS sources holds is refused.
2. Send one message, from that mailbox, to alpha's address. Send the same message to bravo's
   address separately, as a publisher sends one copy to each subscriber.
3. Send a message to an address of the same domain that you made up, with 26 characters from
   `a`–`z` and `2`–`7`. No bounce comes back: an unknown address is dropped in silence, exactly
   as a delivered message is answered.
4. Run `scripts/hosted.sh check` before either tenant's next run. Whatever it judges, it keeps
   each tenant's report in the temporary directory it names, and each report's
   `newsletters` lists that tenant's queued messages by sender, subject and when they arrived.
   It never shows the body. Alpha's and bravo's each list the message once, and neither lists
   the one sent to the made-up address.
5. Run `scripts/hosted.sh run morning` (step 7), which queues each tenant's run, and once both
   runs are done (each report's `runs` shows it) run `scripts/hosted.sh check` again. Each
   tenant's report lists `Test Letter` under `stored_articles`, its run's fetch of the message,
   and its `newsletters` no longer lists it. Whether the issue page then shows it is the
   pipeline's own judgement of a `read` source, as for any article.

A tenant's run, at the hours step 5 scheduled or on `scripts/hosted.sh run`, deletes the messages its
sources matched. It leaves a message no source matches for 8 days from its arrival, then
deletes it, so a source added within those days still gets its issue. The report shows such a
message's `held_at`, when a run first left it queued. A queue of 200 makes room for new mail by
evicting the oldest held message. A sender's case does not matter: `Letter@News.example` matches
a source added as `letter@news.example`. To keep a tenant's queue
for longer, pause it (step 5). A paused tenant still takes mail.

### 9.4 Measure a Gmail filter forward's DMARC result

A reader who already gets a newsletter in Gmail can forward it with a filter instead of
subscribing again. Whether that forward passes DMARC decides whether the handler can later
require a pass, so measure it once:

1. Pause alpha, so that no run acks what arrives:
   `POST /hosted/tenants/alpha/register?status=paused` with the service token's two headers.
2. In Gmail, Settings > Forwarding and POP/IMAP > Add a forwarding address *(label unverified)*:
   enter alpha's address. Gmail sends a confirmation to it.
3. Run `scripts/hosted.sh check`. Alpha's report lists the confirmation from
   `forwarding-noreply@google.com`, and its subject carries the confirmation code. Enter that
   code in Gmail.
4. Create a filter for one newsletter you receive, with the action Forward it to *(label
   unverified)* alpha's address.
5. Run `scripts/hosted.sh tail`, and wait for the next issue of that newsletter, or have its
   sender send one. The handler logs one line, `inbound stored`, with `tenant` and `auth`: the
   `spf`, `dkim` and `dmarc` verdicts from Cloudflare's own `Authentication-Results` header.
   The handler reads only the topmost such header, and only when its authserv-id is
   `mx.cloudflare.net`. `auth: null` means Cloudflare's header was not on top.
6. Record the `dmarc` verdict, the newsletter's sending domain and the date in ADR-0034's *More
   Information*, in a docs commit. Then resume alpha with `?status=active`.

The handler logs the verdicts and does not act on them yet. Nothing a tenant can see shows its
address, and Gmail's confirmation reaches a tenant only through you. Both wait for the tenant
settings card.

## 10. Invite a reader and check the sign-in

Only the operator puts an address on a tenant's invite list, and an address belongs to one
tenant. Invite an address you can read, from step 0's external inbox:

```bash
scripts/hosted.sh invite alpha reader@example.net
```

This runs `POST /hosted/tenants/alpha/invite` with the address in a JSON body, so it is in no
URL. The answer is the address's row and `mail`: `{"sent": true, ...}` when the invitation went.
It answers 502 and names what is missing when the Worker has no `MAIL` binding, no
`CYRIS_HOSTED_MAIL_FROM` or no `CYRIS_STORE_HOSTED_DOMAIN`, or with the Email Service's error
code when the send was refused. `E_RECIPIENT_NOT_ALLOWED` there means that no sending domain is
onboarded yet. Either way the address stays on the list.

Then check each of these, and keep the result:

1. The invitation reaches the external inbox, which is not a verified address of the account.
   Look in its spam folder too. Its link is `HOSTED_URL/login/confirm#<token>`; the token after
   `#` never reaches the Worker.
2. Opening the link shows a Sign in button. Pressing it lands on `HOSTED_URL/t/alpha/`, a URL with
   no token, and shows alpha's latest issue: the page's `cyris-issue` receipt names the issue
   `scripts/hosted.sh check` passed for alpha.
3. Opening the mail's link a second time and pressing Sign in says the link has expired or has
   already been used. A link expires after 15 minutes in any case.
4. In the same browser, `HOSTED_URL/t/bravo/` answers `{"error":"not found"}`, and so does
   `HOSTED_URL/t/nobody/`.
5. `HOSTED_URL/login` with an address that is on no list answers as it does for a listed one,
   and no mail arrives.
6. In Workers Logs, open the invocation record of one `/t/alpha/` request *(label unverified)*.
   Note whether it shows the request's `Cookie` header. Cloudflare's documentation does not
   say, and ADR-0033 needs the answer: if the header is shown in full, the session token is in
   the logs.

`scripts/hosted.sh members alpha` lists alpha's addresses, each with its live session count.
`scripts/hosted.sh revoke alpha reader@example.net` takes an address off the list. Its sessions
end at their next request, and any unspent link stops working. A reader signs out at
`HOSTED_URL/logout`. A reader whose session has expired, after 14 days, asks for a new link at
`HOSTED_URL/login`. A reader's asks are capped: one address gets at most one link a minute and
three an hour, one tenant's readers together ten an hour, and everyone 500 a day. When a reader
says no link came, look in the Worker's logs for `sign-in mail held`: it names which cap was
reached. Someone else asking in a reader's name can hold that reader until an hour after the
asking stops, so re-invite them: `scripts/hosted.sh invite alpha reader@example.net` mails a link
at once, whatever the caps, and needs no revoke. A flood of one tenant holds no other tenant, but
a flood of three does: asking in the names of about 12 readers, four of each tenant, reaches the
500-a-day ceiling in about 17 hours, and then every tenant's readers are held, the log line names
`daily`, and only a re-invite mails a link (ADR-0033).

The vote buttons and the Settings link stay hidden on a tenant's pages, because the hosted Worker
has no `/api/vote` or `/settings` yet, and each page is served sandboxed, so its scripts run with
no origin of their own (ADR-0033).

## 11. Tear down (when you are done)

```bash
scripts/hosted.sh delete
```

This asks for the account and a `yes`, then deletes `cyris-hosted` and its Durable Objects,
tenant databases and the registry included. Afterwards, check the dashboard's Containers page (*label
unverified*) for a leftover `cyris-hosted-tenantcontainer` application, and delete the Access
application from step 2. The sending domain stays onboarded to Email Sending until you remove it
there.
