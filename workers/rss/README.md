# cyris-rss — hourly feed buffer on Cloudflare

Polls every RSS/Atom feed once an hour into **D1**, so the 24h digest window has
something to read. The feed list is the `sources` table the app writes, and
nothing else: an empty table polls no feed and logs `sources table is empty`
until a source is added on `/settings` or with `cyris sources push`.

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

A feed snapshot holds far less than a day for high-volume sources — measured
2026-08-06:

| feed | items | span |
|------|-------|------|
| 中央社 財經 | 20 | 2h23m |
| 中央社 國際 | 20 | 4h27m |
| The Verge | 10 | 3h01m |

Fetching at digest time therefore misses most of the window. A same-window URL
diff against Miniflux lost 141 of 317 articles, all from these sources. Hourly
polling into a retention buffer is what closes that gap.

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

[![Deploy to Cloudflare](https://deploy.workers.cloudflare.com/button)](https://deploy.workers.cloudflare.com/?url=https://github.com/musingfox/cyris/tree/main/workers/rss)

The button provisions a **fresh** D1 database and rewrites the id in
`wrangler.toml`. Repoint it at the app's database afterwards, or this Worker
reads an empty `sources` table and polls nothing. By hand:

```bash
cd workers/rss
bun install

# 1. Point wrangler.toml at the SAME D1 database the app uses — paste the id you
#    put in the app's CYRIS_STORE_DATABASE_ID. This Worker reads the `sources`
#    table the app writes, so a database of its own means it reads an empty table
#    and polls nothing. If you have not created one yet:
npx wrangler d1 create cyris               # → copy database_id into wrangler.toml
#    The `articles` table needs no setup step: the Worker creates it on its
#    first poll or request (src/index.js, `SCHEMA`), which is also what a
#    Deploy to Cloudflare button relies on. Note the button provisions a *fresh*
#    database and rewrites the id, so a button deploy needs this repointed after.

# 2. Auth token (same value goes into cyris's .env as CYRIS_WORKER_TOKEN,
#    the one bearer all three Workers accept)
npx wrangler secret put RSS_TOKEN

npx wrangler deploy
```

Then in `cyris.toml`:

```toml
[rss]
worker_url = "https://cyris-rss.<subdomain>.workers.dev"
# Token via env: CYRIS_WORKER_TOKEN
```

## Local development

```bash
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
