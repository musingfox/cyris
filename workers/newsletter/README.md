# cyris-newsletter — Email → RSS-style ingestion via Cloudflare

Ingests email-only newsletters (those without an RSS feed) into cyris, using a
Cloudflare **Email Worker** + **KV**, mirroring the promote loop's pull pattern so
nothing runs on the local machine and it stays cloud-portable.

```
newsletter email → cyris@<your-domain>
  → Cloudflare Email Routing rule → this Worker
       email(): postal-mime parses the mail → stores {from,subject,html,text,date} in KV
  → cyris run → GET /newsletters (Bearer) → match sender to a source (email_match)
       → parse_newsletter + fetch_newsletter_articles expand links into Articles
       → POST /ack clears the queue
```

RSS-capable newsletters (all Substacks, most Ghost/Squarespace sites via
`?format=rss`) should go through Miniflux instead — only use this for genuinely
email-only sources.

## HTTP endpoints (Bearer `NEWSLETTER_TOKEN`)

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/newsletters` | List queued newsletters (cyris pulls these) |
| POST | `/ack` | Body `{"ids":[...]}` — delete processed items from KV |

Inbound email is handled by the Worker's `email()` trigger (Email Routing), not HTTP.

## Deploy

Prereqs: a domain on your Cloudflare account (Email Routing does **not** work on
`*.workers.dev`), and `wrangler` logged in (`npx wrangler whoami`).

```bash
cd workers/newsletter
bun install                                   # postal-mime (MIME parser)

# 1. Create the KV namespace, then paste its id into wrangler.toml
npx wrangler kv namespace create NEWSLETTERS  # → copy the printed id into kv_namespaces

# 2. Deploy
npx wrangler deploy                           # prints https://cyris-newsletter.<sub>.workers.dev

# 3. Set the shared token (same value goes in cyris .env as CYRIS_NEWSLETTER_TOKEN)
TOKEN=$(openssl rand -hex 32)
printf 'NEWSLETTER_TOKEN=%s\n' "$TOKEN" > .dev.vars      # local dev
printf '%s' "$TOKEN" | npx wrangler secret put NEWSLETTER_TOKEN

# 4. Verify (no auth → 401, with auth → [])
curl -s -o /dev/null -w '%{http_code}\n' https://cyris-newsletter.<sub>.workers.dev/newsletters
curl -s -H "Authorization: Bearer $TOKEN" https://cyris-newsletter.<sub>.workers.dev/newsletters
```

## Cloudflare Email Routing (dashboard)

The `wrangler` OAuth token can't configure Email Routing, so do this in the dashboard:

1. Cloudflare → your domain → **Email → Email Routing** → **Enable** (auto-adds MX/SPF DNS).
2. **Routing rules → Create address**: `cyris@<your-domain>` → Action **Send to a Worker** → `cyris-newsletter`.

## Gmail forwarding (per newsletter)

You don't need to change the newsletter subscription — auto-forward from Gmail:

1. Gmail → Settings → **Forwarding and POP/IMAP → Add a forwarding address** →
   `cyris@<your-domain>`. Gmail emails a confirmation link there; it lands in the
   Worker queue — pull `/newsletters` to read the `mail-settings.google.com/mail/vf-...`
   link and open it (incognito, signed in as that Gmail only).
2. Create a **filter**: From the newsletter's sender → **Forward to** `cyris@<your-domain>`.

Filter auto-forwards preserve the original `From`, so `email_match` matches directly.
Manual "Forward" rewrites `From` to you; cyris falls back to the sender in the
forwarded body, so one-off manual forwards work too.

## Wire into cyris

`cyris.toml`:
```toml
[newsletter]
worker_url = "https://cyris-newsletter.<sub>.workers.dev"
# token via env: CYRIS_NEWSLETTER_TOKEN
```
`.env`: `CYRIS_NEWSLETTER_TOKEN=<same token as the Worker secret>`

`build_deps` adds the pull source to `fetch_sources` when both are set.

## Add a new email-only newsletter

1. Gmail filter: forward that sender → `cyris@<your-domain>`.
2. `sources.yaml`: add an `email_match` entry so cyris tags tier/tags:
   ```yaml
   - name: "Example Newsletter"
     type: newsletter
     email_match: "from:author@example.com"
     tier: summarize
     tags: [tech, business]
   ```
Unmatched senders are pulled but skipped (and ACKed, so they don't pile up).

## Local dev

```bash
npx wrangler dev   # uses .dev.vars; email() can be exercised with `wrangler dev` email test tooling
```
