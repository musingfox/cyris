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
Step 9 invites a reader, who signs in with a link mailed to them and reads only their own
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
- for step 9, a domain on Cloudflare DNS in the trial account, onboarded to Email Sending
  (Email > Email Sending > Onboard Domain, *label unverified*). Onboarding adds SPF, DKIM, DMARC
  and bounce MX records to the domain. Until a domain is onboarded, the sign-in mail reaches only
  the account's verified destination addresses, and a new account starts with a daily quota that
  Cloudflare does not publish. Workers Paid includes 3,000 mails a month
  ([limits](https://developers.cloudflare.com/email-service/platform/limits/),
  [pricing](https://developers.cloudflare.com/email-service/platform/pricing/));
- for step 9, an inbox outside the account that is not one of its verified destination
  addresses, to check that a link reaches a stranger.

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
(`TenantContainer`, `TenantDb` and `TenantRegistry`), its `MAIL` Email Sending binding and the
container application `cyris-hosted-tenantcontainer`. It never reads `.env`. A first deploy's registry is empty, so
until step 5 the Worker has no tenant: the hourly tick starts nothing, and every tenant route
answers 404.

Until step 3 sets the Access secrets, every operator request to the Worker is answered with 403.

## 2. Put Access in front of the operator's routes (dashboard)

The Worker checks the Access JWT itself on every operator route, `/hosted/...` (ADR-0015), so
Access must sign those requests in before the Worker sees them. A tenant's pages, `/t/...`, and
the sign-in, `/login` and `/logout`, take a reader's session instead and no Access JWT
(ADR-0033), so Access must not stand in front of them: a reader would meet Access's own login.

The dashboard labels in this step, and the ones in steps 0, 6, 7, 9 and 10, were written from
memory of the dashboard and have not been checked against it. Each is marked *(label
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
`CYRIS_HOSTED_MAIL_FROM`, and sets each one as a secret of `cyris-hosted` on stdin. The sender is
not secret; it is set the same way so that `wrangler.hosted.toml` names no deployment's domain. It also sets `CYRIS_STORE_HOSTED_DOMAIN` to the
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

## 5. Register and seed both tenants

```bash
scripts/hosted.sh register
scripts/hosted.sh seed
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
  `max_tenant_fetches` (the most any one tenant's poll made), `written`, `object_calls` (the
  Durable Object calls the tick made itself) and `wall_ms`;
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

This runs `POST /hosted/tenants/<tenant>/run?period=morning` for each tenant. Each tenant's
container starts with the gateway to that tenant's database registered before `start()`. A
start made while both instances are held is retried for up to 90 seconds. Each answer is
`{"started": true, "tenant": ...}`, or `"started": false` when that tenant's run is already up.

Each run reads its RSS from the tenant's own buffer, `articles`, which step 6 and the hourly tick
fill. It does not poll its feeds itself. A run started before any poll finds the buffer empty and
has nothing to digest, which is why step 6 comes first. Each run writes its issue into the
tenant's `digests`, and its digest page and raw page into the tenant's own `hosted_pages`. No
Pages project is touched. Runs notify nobody yet. Allow about five minutes. To watch the Worker side of it:

```bash
scripts/hosted.sh tail
```

Stop it with Ctrl-C. The containers' own output is in Workers Logs (Workers & Pages >
`cyris-hosted` > Logs, *label unverified*).

## 8. Check that each tenant holds only its own rows

```bash
scripts/hosted.sh check
```

This reads `GET /hosted/tenants/<tenant>/report` for each tenant, then fetches that tenant's
latest page with the same service token, through `GET /hosted/tenants/<tenant>/page`. That route
answers the page `/t/<tenant>/` serves the tenant's readers. `/t/` itself takes a reader's session,
which the service token is not. Each report has the tenant's
tables with their row counts, its sources, the sources of its stored articles, its
`digest_runs`, its `digests`, and the path and SHA-256 of the page its site root serves.
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

A tenant that has no `ok` run yet may still be running. Check again a few minutes later. A run
that ended in `error` has its reason in that tenant's Workers Logs.

The hourly tick also runs each active registered tenant at 08:00 and 20:00 UTC. A check after one of those hours
counts two runs, and each is still that tenant's own.

Do not add a reader, or yourself, to the step-2 Access application to read a tenant's pages.
Access no longer opens `/t/`, and any identity that application admits can call every operator
route. To read a tenant's pages in a browser, sign in as its reader (step 9).

## 9. Invite a reader and check the sign-in

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
`HOSTED_URL/login`; at most one link a minute, and five a day, is mailed to one address, and
at most thirty an hour to everyone, invitations included. When a reader says no link came, look
in the Worker's logs for `sign-in mail held`: it names which cap was reached. An invite past the
hourly cap answers its refusal in `mail`; invite again after the hour.

The vote buttons and the Settings link stay hidden on a tenant's pages, because the hosted Worker
has no `/api/vote` or `/settings` yet, and each page is served sandboxed, so its scripts run with
no origin of their own (ADR-0033).

## 10. Tear down (when you are done)

```bash
scripts/hosted.sh delete
```

This asks for the account and a `yes`, then deletes `cyris-hosted` and its Durable Objects,
tenant databases and the registry included. Afterwards, check the dashboard's Containers page (*label
unverified*) for a leftover `cyris-hosted-tenantcontainer` application, and delete the Access
application from step 2. The sending domain stays onboarded to Email Sending until you remove it
there.
