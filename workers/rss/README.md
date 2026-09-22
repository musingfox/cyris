# cyris-rss — hourly feed buffer on Cloudflare

Polls every RSS/Atom feed once an hour into **D1**, so the 24h digest window has
something to read. The feed list is the app's D1 `sources` table, which `/settings`
and `cyris sources push` write, and nothing else. This Worker therefore shares the
app's database, and it creates only its own `articles` table: against a D1 the app has
never reached, every poll fails with `could not read sources from D1: … no such
table`, and with the table present but empty it logs `sources table is empty`.

Part of the Cloudflare install; the whole order is in
[docs/install-cloudflare.md](../../docs/install-cloudflare.md#optional-workers).

```
cron 0 * * * *
  → scheduled(): fetch every feed, 4 at a time → parse → INSERT OR IGNORE → prune >8d
  → cyris run → GET /articles?after=&before= (Bearer) → CloudflareRssSource
       → Articles → ArticleStore (dedups by URL again, harmlessly)
```

**Needs Workers Paid.** `wrangler.toml` raises the CPU ceiling to 300s because on
Free the scheduled handler died with *Exceeded CPU Limit* on every tick — parsing
~50 feeds of XML is far past what Free allows.

## Why this exists

A feed publishes only its current snapshot, and a busy one holds 2–4 hours of it, so
fetching at digest time misses much of a 24h window. Hourly polling into a retention
buffer closes that gap.

## Endpoints (Bearer `RSS_TOKEN`)

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/articles?after=&before=&limit=` | Read a window (ISO8601). Idempotent — no ack |
| POST | `/poll` | Trigger a poll manually (same code path as cron) |
| GET  | `/stats` | Row count and the oldest/newest `published_at` |

There is deliberately **no ack endpoint**: this is a retention buffer, not a
queue. Deleting on read would defeat its purpose and lose a batch whenever a
digest crashes. Rows age out after 8 days, matching the ArticleStore's dedup scan.

## Deploy

Deploy it after the app has booted once against its D1 (so the tables exist) and holds
at least one source.

```bash
cd workers/rss
bun install

# 1. Point wrangler.toml at the app's D1: set both database_name and database_id
#    under [[d1_databases]] to the database whose UUID is the app's
#    CYRIS_STORE_DATABASE_ID. The committed values belong to another account.

# 2. Deploy, then set the bearer. It becomes the app's CYRIS_WORKER_TOKEN, the
#    token the rss and newsletter Workers share (promote has its own). Secrets
#    cannot be read back, so keep this value: the app and newsletter need it too.
#    If newsletter already has one, export CYRIS_WORKER_TOKEN=<it> first.
TOKEN=${CYRIS_WORKER_TOKEN:-$(openssl rand -hex 32)}; echo "$TOKEN"
bunx wrangler deploy
printf '%s' "$TOKEN" | bunx wrangler secret put RSS_TOKEN
```

Then, from the repo root, point the app at it:

```bash
bunx wrangler secret put CYRIS_RSS_WORKER_URL --env-file /dev/null   # https://cyris-rss.<subdomain>.workers.dev
bunx wrangler secret put CYRIS_WORKER_TOKEN --env-file /dev/null     # same value as RSS_TOKEN
```

Both must be set, or the app keeps polling feeds directly. Check the buffer with
`POST /poll` and then `GET /stats`, each with `Authorization: Bearer <RSS_TOKEN>`.

The Deploy to Cloudflare button below provisions a **fresh** D1 database and writes
its id into the `wrangler.toml` of the repository it clones for you. Edit
`database_name` and `database_id` there to the app's database and push, so Workers
Builds redeploys it; then set `RSS_TOKEN` and the two app secrets as above.

[![Deploy to Cloudflare](https://deploy.workers.cloudflare.com/button)](https://deploy.workers.cloudflare.com/?url=https://github.com/musingfox/cyris/tree/main/workers/rss)

For a local install that uses the `json` store, the app side is `[rss] worker_url` in
`cyris.toml` plus `CYRIS_WORKER_TOKEN` in `.env`; the feed list still has to be in D1
(`cyris sources push`).

## Local development

```bash
# The local D1 starts empty: apply the app's schema first, or /poll fails with
# "no such table: sources"
npx wrangler d1 execute DB --local --file ../../src/cyris/adapters/store/schema.sql
npx wrangler dev --local --port 8799 --var RSS_TOKEN:devtoken

curl -X POST -H 'Authorization: Bearer devtoken' localhost:8799/poll
curl -H 'Authorization: Bearer devtoken' localhost:8799/stats
```

`bun run test` covers the parser (RSS 2.0, Atom, tracking-param stripping, date
normalisation) without needing the Workers runtime.

## Notes

- URLs are stripped of `utm_*`/`fbclid`-style params before insert, mirroring
  `cyris/adapters/fetch/email_parser.py`. The URL is D1's primary key, so an
  unstripped one would store the same article twice.
- Entries older than the retention window are dropped *before* insert. Blogs keep
  months of history in their feeds; inserting and then pruning those burned ~1.5k
  writes per tick against D1's daily quota.
- `published_at` is normalised to ISO8601 UTC at write time so the window query is
  an ordered string comparison.
- Email-only newsletters do **not** belong here — they arrive via
  `workers/newsletter`.
- **Substack rate-limits Cloudflare's egress.** 8 of the 9 Substack feeds returned
  HTTP 429 on the first cloud poll; concurrency was dropped from 10 to 4 and some
  still fail. Because the buffer accumulates, a 429'd feed usually lands on a later
  tick — but a persistently blocked one would silently vanish from the digest.
  Watch `/stats` for Substack names whose article count stays at zero across ticks.
