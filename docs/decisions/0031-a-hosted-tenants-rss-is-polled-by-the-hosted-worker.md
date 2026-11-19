---
status: accepted
date: 2026-11-19
decision-makers: ""
---

# A hosted tenant's RSS is polled hourly by the hosted Worker into the tenant's own Durable Object

## Context and Problem Statement

A self-hosted deployment's RSS arrives through `workers/rss`. That Worker polls the feeds in one
D1's `sources` every hour and writes what it finds into that D1's `articles`, and a run reads a
window of it ([ADR-0001](0001-rss-arrives-through-an-hourly-buffer.md)). A hosted tenant has no D1.
Its tables are its own SQLite Durable Object
([ADR-0028](0028-one-sqlite-durable-object-per-hosted-tenant.md)), and until now its run polled
its feeds directly at digest time. ADR-0001 measured that path missing 141 of 317 articles.

If the tenants shared one buffer, each run would read every tenant's rows. A feed the run does
not know is mapped as skim (`rss_worker_source.py`, `source.lane if source else Lane.SKIM`), so
one tenant's feeds would reach another tenant's digest.

On 2026-11-18 the human decided that each tenant's feeds are polled hourly into that tenant's own
buffer, following ADR-0001. One feed is fetched once per tenant that has it. A shared feed pool
stays deferred. On 2026-11-19 the human decided where the poll runs.

## Decision Drivers

* Only the hosted Worker may reach the operator's registry, which holds the tenant list
  ([ADR-0030](0030-the-operator-registry-is-one-sqlite-durable-object.md)).
* A tenant's rows stay in the tenant's own object, and no container holds a binding.
* The self-hosted `workers/rss` keeps its behavior, and the parsing and buffer rules are shared,
  not copied.
* A run started in a tick must read that hour's entries.
* For 30 tenants of 4 feeds each, and for 30 tenants of 50 feeds each, the design must stay under
  Cloudflare's CPU, wall-time and subrequest limits.

## Considered Options

* The hosted Worker's own hourly `scheduled()`. It asks each active tenant's object to poll, and
  the object does the fetching.
* The same `scheduled()`, fetching and parsing every tenant's feeds in the tick's own invocation.
* A separate poll Worker, as `workers/rss` is for a self-hosted install.
* A Durable Object alarm per tenant, which the tick only sets.

## Decision Outcome

Chosen option: "the hosted Worker's own hourly `scheduled()`, asking each active tenant's object
to poll". The human decided this on 2026-11-19. Only the app Worker may reach the registry, so a
separate Worker is ruled out. Within that Worker, handing each poll to the tenant's own object
puts the feed requests and the parsing in the object's invocation, which has budgets of its own.
The tick itself is left with one call per tenant.

How it is built:

* `workers/rss/src/buffer.js` holds the poll's rules: fetch four at a time with a 20-second
  timeout, drop entries the feed dates past 8 days, `INSERT OR IGNORE` stamped with the poll's
  start, prune by that stamp, then upsert `feed_health`. The code moved there from
  `workers/rss/src/index.js` unchanged. `index.js` hands it `env.DB`.
* `TenantDb.pollRss()` (`workers/app/src/tenant_buffer.js`) reads the tenant's `rss` sources with
  `workers/rss`'s own `loadFeeds`. It applies `hosted_schema.sql`, then calls `pollFeeds` over a
  D1-shaped handle on the object's storage. A batch runs in `transactionSync`, as D1 runs a
  batch. Reading the sources first means a tenant that was never seeded fails before any table is
  created for it. Applying the schema on every poll gives a tenant seeded before this change its
  `articles` at its next tick.
* `hosted_schema.sql` creates `articles` with the columns and indexes `workers/rss` gives it.
  `tests/test_store_parity.py` holds the two equal.
* The tick reads the active tenants from the registry. For each tenant, all at once, it polls,
  then plans, then starts that tenant's run if one is due. A row is stamped when its poll starts,
  before the run's window closes, so a run started in the tick reads the hour's entries. A failed
  poll is logged, and its tenant's run still starts on what the buffer already holds. A paused or
  unregistered tenant is neither polled nor started.
* A hosted run's RSS source is `TenantRssSource`. It runs the rss Worker's `GET /articles` query
  on the tenant's own `articles` through the run's gateway, and maps the rows with
  `CloudflareRssSource`'s own function. A failed read raises, so the source lands in
  `failed_sources`.
* The tick logs one `hosted tick` line with `tenants`, `polled`, `failed_polls`, `feeds`,
  `feed_fetches`, `max_tenant_fetches`, `written`, `object_calls` and `wall_ms`. Each tenant's
  poll logs `rss poll` with its own counts. `POST /hosted/tenants/<tenant>/poll`
  (`scripts/hosted.sh poll`) runs one tenant's poll on request.

### The limits, and the arithmetic for 30 tenants

The limits below were read on 2026-11-19 from
[Workers limits](https://developers.cloudflare.com/workers/platform/limits/) and
[Durable Objects limits](https://developers.cloudflare.com/durable-objects/platform/limits/):

* A Cron Trigger gets 15 minutes of CPU when it fires every hour or less often, and 15 minutes of
  wall time ("Scheduled Workers have a maximum wall time of 15 minutes per invocation").
* An invocation gets 10,000 subrequests on Workers Paid (configurable up to 10M). A subrequest
  is "any request a Worker makes using the Fetch API or to Cloudflare services like R2, KV, or
  D1". The page does not say whether a Durable Object call is one, so the arithmetic counts each
  one as if it were.
* An invocation can have six connections waiting for response headers at once.
* A Durable Object request gets 30 seconds of CPU by default, and "each incoming HTTP request or
  WebSocket message resets the remaining available CPU time". `limits.cpu_ms` raises it to 5
  minutes. Its wall time is unlimited while the caller stays connected, and it may have six
  outgoing connections at once.
* An isolate has 128 MB. The Durable Objects
  [in-memory state](https://developers.cloudflare.com/durable-objects/reference/in-memory-state/)
  page says "Memory is measured per isolate (which can host multiple Durable Objects), not per
  object".

The tick's own invocation makes 1 registry call. Per tenant it makes 1 poll call, at most 2
schedule reads and at most 1 start, so at most `1 + 4 × 30 = 121` calls for 30 tenants, whatever
their feed counts. It fetches no feed and parses nothing.

Each tenant's poll runs in that tenant's object's invocation:

* Subrequests: one fetch per feed, so 4 or 50, plus any redirect a feed answers with. If these
  were counted against the tick instead, the totals would be `121 + 30 × 4 = 241` and
  `121 + 30 × 50 = 1,621`. Both are under 10,000.
* Connections: four feeds are fetched at a time, which is under six.
* CPU: `parseFeed` on a 270 KB feed of 50 items took 1.3 ms in Node 24, warm, on the
  development machine. At ten times that for a cold isolate, 50 feeds cost about 0.65 seconds,
  and 4 feeds about 0.05 seconds, plus the SQLite writes. Both are far under the 30-second
  default. `limits.cpu_ms` is left unset until a measurement says otherwise.
* Wall time: at most `ceil(F / 4) × 20` seconds of fetching, plus the writes. That is 20 seconds
  for 4 feeds and 260 seconds for 50.

The tick polls every tenant at once and waits for the slowest. Its wall time is at most the
slowest poll, plus a start's 90-second retry, plus the reads. That is about 2 minutes for 4 feeds
and about 6 minutes for 50, under 15. Suppose the six-connection limit also holds the tick to six
object calls in flight. Then 30 tenants poll in five waves. For 4 feeds that is at most
`5 × 20 = 100` seconds. For 50 feeds it is about `5 × 26 = 130` seconds when each batch of four
answers in about 2 seconds. It reaches `5 × 260 = 1,300` seconds, over 15 minutes, only if every
feed of every tenant times out. The tick's `wall_ms` is the number to watch for that.

### Consequences

* Good, because a hosted run reads an hourly buffer, so the 141-of-317 loss of a digest-time poll
  no longer applies to it.
* Good, because each tenant's buffer is in its own object. A run reads only its own feeds'
  entries, and a feed it does not know still maps as it does on the self-hosted path.
* Good, because one tenant's failing poll is logged and leaves every other tenant's poll and
  start alone.
* Good, because `workers/rss` and the hosted poll run the same code for fetching, retention,
  `INSERT OR IGNORE`, the prune and `feed_health`.
* Bad, because a feed two tenants share is fetched twice an hour. A shared pool is deferred.
* Bad, because a tenant's run start now waits for that tenant's poll, up to `ceil(F / 4) × 20`
  seconds after the hour.
* Bad, because the hosted Worker's bundle now includes `fast-xml-parser`. It resolves from
  `workers/rss/node_modules`, so a hosted deploy needs `bun install` in `workers/rss` first, as CI
  and `scripts/check.sh` already run it.
* Bad, because one poll holds every entry it read until it writes them, as `workers/rss` does,
  and objects that share an isolate share its 128 MB. A 50-feed poll holds roughly its feeds'
  total size. How many objects one isolate hosts at once was not measured.
* Neutral, because a hosted tenant's onboarding vote cards are unchanged. They still poll the
  picked feeds directly with `RssSource`, and the hosted Worker serves no wizard yet.

### Confirmation

* `workers/app/test/tenant_buffer.test.js` drives `hosted.js`'s `scheduled()` over real SQLite.
  It checks that two tenants with different feeds each get only their own entries. It checks
  that two tenants sharing a feed both get its entries, with one fetch each. A paused or
  unregistered tenant is not polled. A failing tenant leaves the other's buffer filled and its
  run started. `feed_health` is kept per tenant. A run started in the tick sees the hour's
  entries, and a tenant seeded before the buffer gets its table. Retention, `INSERT OR IGNORE`
  and the prune hold, and the tick logs its measurement line.
* `workers/app/test/tenant_registry.test.js` checks the poll route: 404 for an unregistered
  tenant, 409 for a paused one, and 502 with the reason for a failed poll.
* `tests/test_tenant_rss_source.py` checks that a hosted run reads its window from its own object
  and maps rows exactly as `CloudflareRssSource` does. A failed read lands in `failed_sources`.
* `tests/test_hosted_run_wiring.py` checks that a hosted run's RSS edge is `TenantRssSource`
  whatever rss Worker URL is set, and that onboarding is unchanged. `tests/test_rss_config.py`
  checks that a self-hosted store still gets `CloudflareRssSource` or `RssSource`.
* `workers/rss/test/` passes unchanged.
* On the trial account, the human runs `scripts/hosted.sh poll` and reads the `hosted tick` line
  and the invocations' CPU time in Workers Logs (`docs/hosted-tenants.md`).

## Pros and Cons of the Options

### The hosted Worker's `scheduled()`, each tenant's object polling

* Good, because the registry stays reachable from the hosted Worker alone.
* Good, because the feed requests and the parsing use each object's own CPU and subrequest
  budget, so the tick's own cost grows by one call per tenant.
* Bad, because the tick waits for its slowest tenant's poll.

### The same `scheduled()`, fetching in the tick itself

* Good, because no object does any fetching.
* Bad, because every tenant's parsing shares one invocation's CPU, and every write is an object
  call. At 30 × 50 feeds that is 1,500 fetches, and tens of thousands of rows a first poll would
  write in batches, all in one invocation.

### A separate poll Worker

* Good, because it mirrors `workers/rss`.
* Bad, because it would need the registry, which only the app Worker may reach. The human ruled
  it out on 2026-11-19.

### A Durable Object alarm per tenant

* Good, because each poll would get its own 15-minute invocation, and the tick would only set
  alarms.
* Bad, because the run start would then have to move into the alarm too, so that a run follows
  its poll. That moves the tick's planning into the tenant's object. An alarm that throws is
  retried, which would repeat a start the tick now makes once.

## More Information

* `docs/architecture.md` §3.1 and §6 describe the hosted poll. §4 has the row for a hosted
  tenant's buffer.
* `docs/hosted-tenants.md` is the procedure, including the poll and the measurement.
* ADR-0028's "Its RSS is polled directly at run time" is superseded by this record.
