# cyris-promote — the digest's vote queue

The 👍/👎 buttons under each digest item write here, and `cyris` drains the
queue on its hourly tick (`adapters/promotions.py`). A vote is what marks an
article's state as a *human* decision rather than the pipeline's own, which is
also what seeds vote similarity — so this is the one Worker whose writes change
what future digests select.

```
reader clicks 👍 in a published digest
  → cyris-app POST /api/vote (attaches the bearer server-side)
       → this Worker: KV promote:<sha256(url)> = {url, vote, digest_date, ts}
  → cyris promote-sync → GET /promotions (Bearer) → accept/reject in the store
       → POST /ack clears the keys it consumed
```

Keyed by the URL's hash, so a reader changing their mind overwrites rather than
queues twice. `deep` is the vote an older published digest sends when it names
none; those pages only ever meant "I read this properly".

## Endpoints (Bearer `PROMOTE_TOKEN`)

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/promote` | Body `{url, vote, digest_date}` — `vote` is `up`, `down` or `deep` |
| GET  | `/promotions` | Everything queued (`cyris promote-sync` pulls this) |
| POST | `/ack` | Body `{"urls":[...]}` — drop the keys already applied |

## Deploy

[![Deploy to Cloudflare](https://deploy.workers.cloudflare.com/button)](https://deploy.workers.cloudflare.com/?url=https://github.com/musingfox/cyris/tree/main/workers/promote)

The button provisions its own KV namespace and rewrites the id in
`wrangler.toml`; the id committed there is a working default, not a leak — it
grants nothing without an API token scoped to that account.

By hand instead:

```bash
cd workers/promote
npx wrangler kv namespace create PROMOTIONS   # → copy the id into wrangler.toml
npx wrangler secret put PROMOTE_TOKEN
npx wrangler deploy
```

Then set the same token as `CYRIS_PROMOTE_TOKEN` on `cyris-app`, and point the
app at this Worker with `CYRIS_PROMOTE_WORKER_URL`. Leave either unset and the
digest simply renders without vote buttons.

**The token is not a secret in the usual sense**, but it is no longer rendered
into published pages either: votes go through the app's same-origin
`/api/vote`, which attaches the bearer server-side. Keep it distinct from
`CYRIS_WORKER_TOKEN`, which the rss and newsletter Workers accept.
