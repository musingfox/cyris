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
there, is [ADR-0032](decisions/0032-hosted-runs-wait-in-a-queue-the-registry-holds.md). Why a
tenant makes one digest a day unless you allow it two, and why the seed spreads tenants over the
hours, is [ADR-0042](decisions/0042-a-hosted-tenant-makes-one-digest-a-day-unless-allowed-two.md).
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
- `CYRIS_HOSTED_OPERATORS`: who the operator routes admit, comma separated. Give the step-2
  service token's Client ID, and your own Access email only if you also sign in with a browser.
  The Worker refuses a valid Access sign-in of anyone else.
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
  domain you onboard to Email Sending in step 0. Use a subdomain that only sends, so its bounce MX
  and DMARC stay apart from the readers' domain: readcyris.com sends from
  `signin@mail.readcyris.com`, and its `privacy@`, `support@` and `security@` addresses
  (`workers/app/src/hosted_legal.json`) are Email Routing rules on the apex.

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

Every route call below asks for the Access service token's id and secret. When
`CYRIS_ACCESS_CLIENT_ID` and `CYRIS_ACCESS_CLIENT_SECRET` are both set, the script takes them
from the environment instead, as `cyris --tenant` does. Bitwarden Agent Access can put them there
for one shell, approved once, with the token stored as a login item whose username is the id and
whose password is the secret:

```bash
aac run --domain <item-host> --env CYRIS_ACCESS_CLIENT_ID=username --env CYRIS_ACCESS_CLIENT_SECRET=password -- bash
```

Log in as a user who can see the trial account: that login is the credential every wrangler call of the script
acts as, since none of them reads `.env`.

## 1. Deploy

The Worker binds the R2 bucket its daily backup writes to (`BACKUP` in `wrangler.hosted.toml`),
and a deploy that binds a bucket the account does not have fails. Create it once, on the trial
account, before the first deploy (step 11 sets its lifecycle rule):

```bash
CLOUDFLARE_ACCOUNT_ID=<trial account id> bun x wrangler r2 bucket create cyris-hosted-backup --config wrangler.hosted.toml --env-file /dev/null
```

```bash
scripts/hosted.sh deploy
```

This runs `wrangler deploy --config wrangler.hosted.toml --env-file /dev/null`, after you
confirm the account. It deploys the Worker, its three Durable Object classes
(`TenantContainer`, `TenantDb` and `TenantRegistry`), its `MAIL` Email Sending binding and the
container application `cyris-hosted-tenantcontainer` and its `BACKUP` R2 binding. It never reads `.env`. A first deploy's registry is empty, so
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
   403, and so does every path that is not `/hosted/`, `/t/`, the sign-in or `/api/vote`. The
   vote route takes neither the JWT nor the session cookie, only the vote token a page served to
   a signed-in reader carries, and reaches no page and no operator route (ADR-0039).
2. In Zero Trust > Access > Service credentials *(label unverified)*, create a service token, or
   reuse the trial's. Note its Client ID and Client Secret. The secret is shown once.
3. In the step-1 application, add a policy whose action is Service Auth *(label unverified)* and
   that includes that token. Add no other identity to this application, and no policy that admits
   a group, a domain or everyone. It is the operator's alone; readers sign in at `/login`
   (step 10), never through Access.
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

This asks for `CYRIS_ACCESS_TEAM_DOMAIN`, `CYRIS_ACCESS_AUD`, `CYRIS_HOSTED_OPERATORS`,
`ANTHROPIC_API_KEY`, `CYRIS_HOSTED_OPERATOR_WEBHOOK` and `CYRIS_HOSTED_MAIL_FROM`, and sets each one as a secret of `cyris-hosted` on stdin.
`CYRIS_HOSTED_OPERATORS` is the list the Worker checks every operator request's Access identity against
([ADR-0037](decisions/0037-the-hosted-operator-routes-admit-only-named-identities.md)): an entry
with an `@` is an Access email, and any other is a service token's Client ID, which Access signs
into the token as `common_name`. Until it is set, every operator route answers 403 naming it, and
a valid sign-in of an identity it does not name answers 403 too. It is not secret either. The webhook is read without echo, because its URL is its credential, and
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
scripts/hosted.sh schedule alpha 08:00 UTC
scripts/hosted.sh schedule bravo 09:00 UTC
```

The two `schedule` lines are optional: the seed already gives each tenant an hour of its own.
Run them to choose the hours yourself.

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
- every required setting: provider `anthropic`, model `claude-haiku-5-5`, one digest a day in
  UTC, language `en`, and no notification target;
- four RSS feeds from the onboarding catalog. No feed is shared between the two tenants
  (`workers/app/src/hosted_seed.json`).

For a tenant the registry holds no schedule for, the seed also sets one in the registry: one
digest hour in UTC, taken from a hash of the tenant id over every hour but 04:00 to 06:00, the
backup's and its check's (`spreadHour` in `workers/app/src/hosted_seed.js`). Each tenant always
gets the same hour, and tenants seeded together get different hours most of the time: `alpha`
gets 08:00 and `bravo` 00:00. The tick starts that tenant's runs from then on. A tenant that has
a schedule keeps it, and the seed writes that schedule into its settings instead.

Each answer names the tenant, how many settings it wrote, its sources, how many tables it has and
its `schedule`. Seeding again writes the same rows back and leaves other rows alone.

To stop a tenant's runs without deleting anything, register it again as paused. The script has
no subcommand for it; send `POST /hosted/tenants/<tenant>/register?status=paused` with the
service token's two headers, and `?status=active` to resume it. Only a named status changes a
registered tenant's status. The tick skips a paused tenant,
and its run route answers 409. Its other routes still answer, and it still takes mail at its
newsletter address (step 9).

`schedule TENANT TIMES TIMEZONE [DAILY_RUN_CAP [MAX_DIGEST_TIMES]]` runs
`POST /hosted/tenants/<tenant>/schedule?times=...&timezone=...` for one tenant. It sets the hours
the hourly tick wakes that tenant at, the zone they are hours of, and, when you name them, its
daily run cap and how many digest hours it may have. The registry holds all four, and the tick
reads them nowhere else: the schedule copied into the tenant's own database is not what wakes it.
`TIMES` is one whole hour, such as `07:00`. Its digest is stored as the morning issue, whatever
the hour. Two hours, such as `07:00,19:00`, a morning and an evening issue, are taken only from a
tenant whose maximum is 2. `TIMEZONE` is an IANA zone name such as `Asia/Taipei`, never an offset
such as `+08:00`, which the container cannot read and the route refuses. The cap is the most runs
the tenant may start in any 24 hours, from 1 to 24. A new tenant's cap is 3: one hour, its tick
and two catch-ups. The maximum is 1 or 2, and a new tenant's is 1. To allow a tenant two digests
a day, name both hours, a cap of 6 and the maximum together:

```bash
scripts/hosted.sh schedule alpha 07:00,19:00 Asia/Taipei 6 2
```

Scheduling a tenant again without naming a cap or a maximum keeps the ones it has. Two hours for
a tenant whose maximum is 1 are refused, and so is a maximum of 1 named with two hours. The answer is the tenant's registry row,
and `"settings": true` once the route has copied the hours and the zone into the tenant's own
settings. `false` means the copy could not be written, and the Worker logs why
(`schedule copy not written`). For a tenant not yet seeded that is expected: its seed writes the
copy instead. A schedule the registry does not take answers 400 with the reason. Until a
tenant has a schedule, from the seed or this route, the tick polls its feeds and queues no run,
and its run route answers 409. Give each tenant different hours to see each one woken at its own.

On a deployment whose tenants were registered before the registry held schedules, run `schedule`
for each tenant after you deploy. Until you do, the tick polls its feeds and queues none of its
runs.

On a deployment whose tenants were scheduled at two hours before the registry held a maximum,
each tenant gets a maximum of 1 when you deploy. The tick then runs only the earlier of its two
hours, as the morning issue, and its cap stays the one it had (6 unless you set another). Run
`schedule` with one hour to make the registry match, or with both hours, `6` and `2` to keep
two digests a day.

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
tenant's zone. The registry starts the queued runs, at most `max_instances` (4) at once and one
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
No tenant's own channel gets an alert: a tenant's run pins its notify targets blank, whatever its
settings rows name (ADR-0040), until a card gives tenants mail.
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
issue`, and the same for `bravo`, is the milestone. The reports and the pages are tenants'
content, so the script deletes its temporary directory when it ends, however it ends. It prints
the SHA-256 of each page it fetched, as `<hash> <tenant>`. Keep those lines with the result.

A tenant that has no `ok` run yet may still be running, or waiting in the queue: its report's
`runs` says which. Check again a few minutes later. A run that ended in `error` has its reason in
your webhook's alert and in that tenant's Workers Logs. A run the registry recorded as `failed`
has its reason in `runs`. A `lost` row is a run whose container never reported its stop within
20 minutes; the row above it is the same issue, queued again.

The hourly tick also queues each active tenant's run at the hour step 5 scheduled. A check
after that hour counts two runs, this step's and the tick's, and each is still that tenant's own.
To see each tenant woken at its own hour, run `check` after both tenants' hours have passed. In each report, every
`digest_runs` row's `finished_at` falls a few minutes after one of that tenant's hours, and its
`runs` row for the hour has `origin` `tick` and state `done`. A tenant with one hour has one such
row a day, of period `morning`.

### Reading the pages in a browser

The step-2 Access application must admit no identity but the operator's: the service token, and
your own email if you sign in with a browser. Never add a reader, a tester or a group to it, and
never add one to read a tenant's pages. Access no longer opens `/t/`. The Worker also refuses any
sign-in `CYRIS_HOSTED_OPERATORS` does not name (step 3), but the list is a second line, not a
reason to widen the first. An email you add to the list must be one the application admits
already; adding a reader's email to either would give that reader every tenant's data and every
operator route. To read a tenant's pages in a browser, sign in as its reader (step 10). To read one
as the operator, use `GET /hosted/tenants/<tenant>/page`, which serves the same sandboxed page.

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

### Each tenant's AI spend, and its monthly allowance

The operator pays for every model call, so each tenant has a monthly AI allowance, 2.25 USD
unless you set another. Every run reports what it spent to the registry when it ends, failed or
not. A run that starts with its tenant's month already spent calls no model. It makes the plain
list, and its page says the allowance is used up. Months are UTC, and every allowance comes back
at 00:00 UTC on the first. An allowance of 0 turns the tenant's AI off: its page says so, and
does not promise the next month. Why it works this way is
[ADR-0038](decisions/0038-a-hosted-tenants-ai-spend-is-metered-in-the-registry.md).

After step 7, list every tenant's month:

```bash
scripts/hosted.sh spend
scripts/hosted.sh spend 2026-11
```

Each tenant has one entry: its `budget_usd` (`budget_set` is false while it is the default),
`cost_usd` (the sum of `llm_usd`, `embedding_usd` and `clef_usd`), its tokens and `runs`, the
number of runs that reported. A Haiku 5.5 run of about 150 articles costs about 0.017 USD.
`unpriced_tokens` above 0 means a tenant runs a model this release has no rate card for. Those
tokens are charged at the dearest rate card it has, so that tenant's allowance runs out sooner
than its model's real price would make it; add the model's rate to `_PRICES_PER_MTOK` in
`src/cyris/domain/models.py`.

To see a spent month stop the AI, set one tenant's allowance to 0 and run both tenants:

```bash
scripts/hosted.sh budget alpha 0
scripts/hosted.sh run evening
scripts/hosted.sh check
```

Once both runs are `done`, alpha's latest page opens with "AI is turned off for this site" and
has no scores or summaries. A tenant whose allowance is above 0 and spent opens with "This
month's AI allowance is used up" instead. Bravo's page is a normal issue. Alpha's report has a `spend`
entry, its month, with `"spent": true`. Then give alpha its allowance back:

```bash
scripts/hosted.sh budget alpha 2.25
```

A run killed before it could report, for example by running out of memory, is missing from its
month. The run object's alert in your webhook says it stopped.

A run that cannot reach the ledger still runs, with its AI, and sends your webhook one "AI spend
ledger unreachable" alert. Its body says whether the run could not read its allowance, so it was
not held to it, or could not add its spend, so the month's total is short by the `ai_spend` in
its `digest_runs.summary`. Alerts like this on every run mean no tenant is held to its allowance
until the ledger answers.

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
4. Run `scripts/hosted.sh check` before either tenant's next run. Whatever it judges, it prints
   each tenant's report as it reads it (and keeps no copy, step 8), and each report's
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
address, and Gmail's confirmation reaches a tenant only through you. Both wait for a later
card: the tenant's settings page (step 10) adds and removes RSS sources only.

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
7. In a private window, with no session, `HOSTED_URL/privacy` and `HOSTED_URL/terms` open: the
   privacy policy and terms, the Traditional Chinese text first and the English after it. The
   sign-in page links both, and the invitation ends with both links on `HOSTED_URL`.

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

The vote buttons show on a page served to a signed-in reader, and a vote lands in that
tenant's own `hosted_votes` table and no one else's, through a token the Worker put in the page
(ADR-0039): a page is sandboxed, so its scripts have no cookie and no origin of their own
(ADR-0033). The token ends 12 hours after the page view that made it, so a page left open longer
shows the signed-out message at its next vote and a reload mends it. The vote is applied by the
tenant's next run, not at once, and a reloaded page does not show a past vote (its scripts have no
storage). To check a trial tenant, vote once as its reader on a digest page, then read the tenant's report
as the operator (`GET /hosted/tenants/alpha/report` with the service token): its tables list
`hosted_votes` with one row, and after the tenant's next run, which applies the vote and deletes
the row, zero rows. *Unverified in a browser:* that the page's `fetch` passes the route's preflight and
the buttons appear; look in the browser's console for a blocked request if they do not.

### The privacy policy and terms

`/privacy` and `/terms` are the Worker's own pages, read by anyone before signing in
(`workers/app/src/hosted_legal.js`). The Traditional Chinese text is the authoritative one, and the
English a translation. What they say is what this deployment does: what it keeps of a reader, that it
is kept on Cloudflare, that Anthropic receives the articles' text for the summaries, that a failed run's
alert reaches your Discord webhook, that you may look at a tenant only for support, an incident or
abuse, and that an export lives 30 days in R2. So a change to any of those changes the pages: the
wording is in the UI catalogs (`privacy.*`, `terms.*`, `legal.*`, each with its zh-Hant entry), and the
contacts, the date, the retention periods and your deadlines are in `workers/app/src/hosted_legal.json`.
Change its `updated` with the text.

The pages also make promises that only you keep; nothing in the code does it for you:

- **One reader to a tenant.** Invite one address to each tenant. The registry would take a second
  one, but the policy says each digest has one reader.
- **A tenant id is not the reader.** The policy says what stays after a deletion is kept under the
  digest's id and holds no email address, so never name a tenant after its reader's name or address.
- **A change that affects readers is mailed first.** Before a change to either page that touches a
  reader's rights or obligations, or how their data is collected, processed or used (what is
  collected, who receives it), takes effect, mail it to every reader
  at least `notice_days` (7) days ahead. Publish every change with a new `updated`; a wording fix
  that changes no one's rights needs no mail.
- **An incident is mailed.** Once you learn that a reader's data was stolen, altered,
  damaged, lost or leaked, mail that reader at the address they sign in with.
- **Requests are answered in time.** A request to see or copy data is answered within 15 days and
  one to complete, correct or stop within 30 (`answer_days`). Either may be extended once by as
  many days again, by mailing the reader the reason. A deletion is done within `deletion_days`
  (30) and is never extended.
- **Deletion without a request.** When you end a reader's access, delete their data as below
  within `deletion_days` (30). Do the same for a reader who has not signed in for
  `inactive_months` (12), after mailing them a reminder `inactive_reminder_days` (30) before the
  months are up. One sign-in starts the count again. The registry does not keep a last sign-in
  time: a session row lasts 14 days, and a spent link's row goes at the next ask after its 15 minutes. So until a
  card records it, run `scripts/hosted.sh members TENANT` at least every 14 days and note the date
  for each address with a live session. The latest noted date is that reader's last sign-in, at
  most 14 days earlier than the real one, which the 30 days absorb.
- **The beta ends with notice.** Mail every reader at least `end_notice_days` (30) days before
  the beta ends, and send a copy to each reader who asks until it ends. Only an urgent case, such
  as a security incident or a legal requirement, may stop it at once, and then you mail the
  readers afterwards.
- **No other provider without notice.** Before you add a second backup destination or a second model provider's,
  change the policy and mail the readers as above: the policy names only Cloudflare, Anthropic and
  Discord.

A reader asks for a copy or a deletion at the privacy address, since there is no account page yet.
Answer only a request mailed from the address the reader signs in with. For a copy, read the
tenant's latest export as 11.2 does and send the file. For a deletion, within 30 days:
`scripts/hosted.sh revoke TENANT EMAIL` takes the address off the list, which ends its sessions and
deletes its sign-in links and asks. Then empty the tenant's own database: nothing deletes a tenant
yet (11.3), so do it through `POST /hosted/tenants/<tenant>/query` (step 8, *Reading or changing
one tenant from the CLI*). Revoke its newsletter address (`POST /hosted/tenants/<tenant>/revoke-address`,
as `scripts/hosted.sh address revoke` sends it), and pause the tenant so no tick polls it again. The
registry keeps the tenant's row, its `runs` and its AI spend under its id, which the policy lists as
what remains. The deleted rows leave the backups as each day's export expires: 30 days after upload
by 11.1's rule, and R2 removes an expired object typically within 24 hours, so about 31 days.

### The reader's settings page

A signed-in reader's pages show a Settings link to `HOSTED_URL/t/<tenant>/settings`, which serves
the settings page a local or cloud deployment serves at `/settings`, the same page and script,
rendered into the committed `workers/app/src/shared_settings.build.json` that step 1 deploys, with
the keys the hosted policy keeps from a tenant locked
([ADR-0043](decisions/0043-one-settings-page-an-edition-locks-a-key.md)); no container starts for
it (ADR-0040). There the reader changes the tenant's output language, style, type size, articles
per run (at most 400), window, vote order, follow summaries, Top story score, publish hour and time
zone, and adds or removes RSS feeds, 30 sources at most with newsletters counted. The page offers a
second publish hour only to a tenant you allowed two: for a tenant allowed one, Evening is disabled
with `Not included in your plan.`
([ADR-0047](decisions/0047-a-route-refuses-a-change-to-a-locked-key.md)). The model, the notify
targets, the run cap, the number of digest hours and the AI allowance stay yours: their controls are
disabled with the line `Set by the service's operator.`, the routes refuse a change to them, and the
run pins the model and blanks the notify targets whatever the tenant's rows say. A save applies from
the tenant's next run, and the hours and zone from the next tick, since they go to the registry as
your schedule route's do; a save names no run cap or maximum, so yours are kept. Tracking is empty
and read-only. Its site bar shows Archive, Settings and Sign out to a signed-in reader, as the
digest, raw and archive pages' does; Sign out ends the reader's session at `/logout`.

To check it on a trial tenant, after a deploy that built the page:

1. Sign in as alpha's reader (above), then open Settings from one of alpha's pages, or
   `HOSTED_URL/t/alpha/settings`. The page opens on Model, in the language of alpha's output
   language, and its site bar shows Archive, which opens alpha's archive, Settings, marked as the
   page you are on, and Sign out. Sign out opens `/logout`; press its button and the same address
   sends you to the sign-in.
2. Under Provider, `anthropic` is the one choice, selected and disabled; Model reads
   `claude-haiku-5-5` and is disabled. Each has the line `Set by the service's operator.` under its
   name, no line promises a call to the provider, and Save stays disabled. `anthropic` reads `key
   ready` only if `ANTHROPIC_API_KEY` is set on the Worker (`scripts/hosted.sh secrets`).
3. Open Digest. Morning is a select of whole hours showing alpha's publish hour; for a tenant allowed
   one, Evening is disabled with `Not included in your plan.`. Choose another morning hour and
   another output language, such as `繁體中文 (Traditional Chinese)`, and press Save: the notice
   names the hour, effective next tick, and the saved value, effective next run.
4. Reload. The page is now worded in that language, and the selects show what was saved.
5. As the operator, read it back: `POST /hosted/tenants/alpha/query` with `{"sql": "SELECT value
   FROM settings WHERE key = 'digest.output_language'"}` answers `"zh-Hant"`, and the same query for
   `general.digest_schedule` answers the new hour, the registry's schedule copied into the tenant.
6. Open Pipeline: Articles per run refuses a value past 400.
7. Open Sources and add one feed: alpha's report as the operator lists it in its `sources`. A feed
   whose host is an IP literal (in any spelling the URL parser reads as an address), `localhost`, a
   `.localhost`, `.local`, `.internal` or `.arpa` name, this Worker's host or any `workers.dev` name
   is refused at the save, and a feed that redirects to one fails its poll with `refused` in the
   `feed failed` line. Nothing looks a name up in DNS, so a public name that resolves to a private
   address is not refused: the gap
   [ADR-0040](decisions/0040-a-hosted-tenant-edits-its-settings-on-the-hosted-worker.md) records
   under its consequences.
8. Set the language and the hour back.

A save refused with `This save was refused: it did not come from this settings page.` means the
browser sent the save without this Worker's `Origin`; the browser's console names any script or
style the page's policy refused.

## 11. Back up every tenant daily, and drill a restore

Once a day, in the hourly tick of 04:00 UTC (`BACKUP_HOUR_UTC`) and after that tick's polls and
queued runs, the Worker exports every registered tenant, paused ones too, and then the registry.
Each export goes to the R2 bucket `cyris-hosted-backup`
([ADR-0041](decisions/0041-the-hosted-databases-are-exported-daily-to-r2-and-b2.md),
[ADR-0045](decisions/0045-the-hosted-backup-is-r2-only.md)). That bucket is in the Cloudflare
account, so no copy outlives the account itself; where a second copy goes is left to a separate
evaluation:

```
<YYYY-MM-DD>/<tenant>.jsonl.gz
<YYYY-MM-DD>/_registry.jsonl.gz
```

Each file is a gzip of JSON lines, every table of that database with its rows, indexes, triggers
and views. Each step that fails is posted to the operator's webhook with the tenant (or
`_registry`) in front and the step in the headline: the export or the R2 write. The next
tenant's export goes on. A backup whose R2 writes all succeed posts nothing. A tenant whose
export is over 32 MiB compressed, roughly 100 MB of data, fails its export with an alert every
day until the dump streams (ADR-0041).

A backup that never ran, because its tick was skipped, or that was cut off by the cron's
15-minute limit, sends none of those alerts. So the tick two hours later, at 06:00 UTC
(`BACKUP_CHECK_HOURS`), looks for that day's `_registry` export in R2. The registry is exported
last, so its key is there only if the backup reached its end. If it is missing, one alert,
`every export` in front, says the backup is not confirmed. Run `scripts/hosted.sh backup`
(11.2) to make that day's exports. The same alert follows a day whose registry export failed on
its own, already alerted at 04:00.

### 11.1 Create the bucket and its retention (once)

Retention is not in code. The bucket deletes old exports by a rule of its own. 30 days is
assumed below, and the privacy policy tells readers that period (`backup_days` in
`workers/app/src/hosted_legal.json`, which `tests/test_hosted_config.py` holds to the R2 command
below): a different period changes both, and the policy's date.

Create the bucket (step 1) and give it a rule that deletes every object 30 days after upload
([object lifecycles](https://developers.cloudflare.com/r2/buckets/object-lifecycles/)):

```bash
CLOUDFLARE_ACCOUNT_ID=<trial account id> bun x wrangler r2 bucket lifecycle add cyris-hosted-backup expire-exports --expire-days 30 --config wrangler.hosted.toml --env-file /dev/null
```

The rule's prefix is left empty, so it covers every date's folder. The flags are the
[Wrangler R2 reference](https://developers.cloudflare.com/workers/wrangler/commands/r2/)'s; the
command was not run when this was written, so check the rule with
`bun x wrangler r2 bucket lifecycle list cyris-hosted-backup` afterwards. In the dashboard, the same rule
is R2 > `cyris-hosted-backup` > Settings > Object Lifecycle Rules (*label unverified*).

### 11.2 Run a backup now, and read it

```bash
scripts/hosted.sh backup
```

This runs today's backup at once, as the 04:00 tick would (`POST /hosted/backup`). It answers
`{date, exports, r2, bytes, failed}`: `exports` counts every tenant and the registry, and
`r2` should equal it. `failed` names each failed step, which the webhook was also told
of. Check the R2 copy, and read it back into an ordinary SQLite file on your machine:

```bash
CLOUDFLARE_ACCOUNT_ID=<trial account id> bun x wrangler r2 object get cyris-hosted-backup/<YYYY-MM-DD>/alpha.jsonl.gz --file alpha.jsonl.gz --remote --config wrangler.hosted.toml --env-file /dev/null
python3 scripts/backup_to_sqlite.py alpha.jsonl.gz alpha.sqlite3
```

The script prints each table's row count. These are a tenant's data: delete the local files when you are done.

### 11.3 Drill a restore into a new tenant id

```bash
scripts/hosted.sh restore <YYYY-MM-DD> alpha alpha-restored
scripts/hosted.sh register alpha-restored paused
```

`restore DATE TENANT NEW_TENANT` loads TENANT's R2 export of that UTC day into NEW_TENANT
(`POST /hosted/tenants/<new>/restore?date=D&from=T`). It refuses with 409 an id the registry
already holds, of any status, or whose database already holds a table. It refuses with 400 an
id no tenant may take, and with 404 a day with no export; in each case it writes nothing. The
answer lists each table with the rows it restored. The restore is one transaction, so a restore
that fails leaves the new id empty, and running it again is safe. Registering the id paused lets
the operator's routes read it while no tick polls or runs it.

To check the drill, run `scripts/hosted.sh check` right after 11.2's `backup`, before the next
hour's tick polls `alpha` again. Its report for `alpha` lists `tables`, each with its row count.
The restore's `restored` must list the same tables with the same counts. To compare rows rather
than counts, `cyris --tenant alpha-restored` (step 8, *Reading or changing one tenant from the
CLI*) reads the registered, paused id the way `cyris --tenant alpha` reads `alpha`. To replace a lost tenant, invite its readers to the new id (step 10) and
resume it with `register alpha-restored active`. Give it a schedule (step 5) and a newsletter
address too: neither is in a tenant's own export, since both are the registry's. To retire a
drill's id, pause it and leave it, as nothing deletes a tenant yet.

### 11.4 The registry: by hand

No route restores the registry: it makes its own tables as it starts, so a restore would collide
with them. Its export is the record of what to re-enter. Read it with `backup_to_sqlite.py` as in
11.2, from `<YYYY-MM-DD>/_registry.jsonl.gz`. Then use the existing routes, from the
`tenants`, `members` and `inbound_addresses` tables:

- `register` each tenant with its status;
- `schedule` each one with its `digest_times`, `timezone`, `daily_run_cap` and
  `max_digest_times`, and `budget`
  each `monthly_ai_budget_usd` that is set;
- `invite` each member again, which mails each reader a new link.

What cannot be re-entered: sessions, sign-in links, the vote key and spend. Readers sign in
again, open pages get a new vote token, and a month's spend starts over. A tenant's newsletter
address cannot be set either, only rotated to a new one, so each newsletter's sender must be
told the new address (step 9).

## 12. Tear down (when you are done)

```bash
scripts/hosted.sh delete
```

This asks for the account and a `yes`, then deletes `cyris-hosted` and its Durable Objects,
tenant databases and the registry included. Afterwards, check the dashboard's Containers page (*label
unverified*) for a leftover `cyris-hosted-tenantcontainer` application, and delete the Access
application from step 2. The sending domain stays onboarded to Email Sending until you remove it
there. The backup bucket is kept: delete `cyris-hosted-backup` in R2
(`bun x wrangler r2 bucket delete cyris-hosted-backup --config wrangler.hosted.toml --env-file /dev/null` once it is empty)
yourself, when you no longer want the exports.
