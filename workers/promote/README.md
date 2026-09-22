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

Part of the Cloudflare install; the whole order is in
[docs/install-cloudflare.md](../../docs/install-cloudflare.md#optional-workers). It
works on the Free plan, but votes reach it only through the app Worker, which needs
Workers Paid.

```bash
cd workers/promote
bunx wrangler kv namespace create PROMOTIONS   # → replace the id in wrangler.toml;
                                               #   the committed one is another account's
bunx wrangler deploy
bunx wrangler secret put PROMOTE_TOKEN          # openssl rand -hex 32
```

Then, from the repo root, point the app at it:

```bash
bunx wrangler secret put CYRIS_PROMOTE_WORKER_URL --env-file /dev/null   # https://cyris-promote.<subdomain>.workers.dev
bunx wrangler secret put CYRIS_PROMOTE_TOKEN --env-file /dev/null        # same value as PROMOTE_TOKEN
```

Keep the token distinct from `CYRIS_WORKER_TOKEN`, which the rss and newsletter
Workers share. It stays server-side: votes go through the app's same-origin
`/api/vote`, which attaches the bearer.

The vote buttons appear for any reader logged in on the app's hostname, whether or
not this Worker is wired. Without `CYRIS_PROMOTE_WORKER_URL` a click is refused with
503, and with the URL but no `CYRIS_PROMOTE_TOKEN` the promote Worker answers 401.
Readers following Discord links reach the app's hostname only when
`CYRIS_PROMOTE_CUSTOM_DOMAIN` names it; on `pages.dev` there are no buttons.

The Deploy to Cloudflare button provisions its own KV namespace; set `PROMOTE_TOKEN`
and the two app secrets afterwards as above.

[![Deploy to Cloudflare](https://deploy.workers.cloudflare.com/button)](https://deploy.workers.cloudflare.com/?url=https://github.com/musingfox/cyris/tree/main/workers/promote)
