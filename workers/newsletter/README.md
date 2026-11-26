# cyris-newsletter — Email → RSS-style ingestion via Cloudflare

Ingests email-only newsletters (those without an RSS feed) into cyris, using a
Cloudflare **Email Worker** + **KV**, mirroring the promote loop's pull pattern so
nothing runs on the local machine and it stays cloud-portable.

```
newsletter email → cyris@<your-domain>
  → Cloudflare Email Routing rule → this Worker
       email(): postal-mime parses the mail → stores {from,subject,html,text,date} in KV
  → cyris run → GET /newsletters (Bearer) → match sender to a source (email_match)
       → parse_newsletter + newsletter_article turn the email body into one Article
       → POST /ack clears the queue
```

RSS-capable newsletters (all Substacks, most Ghost/Squarespace sites via
`?format=rss`) are just feeds: add them as ordinary RSS sources. Use
this Worker only for genuinely email-only sources, since it is the one part of
cyris that needs a domain you control.

## HTTP endpoints (Bearer `NEWSLETTER_TOKEN`)

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/newsletters` | List every queued newsletter, past KV's 1000-key list page (cyris pulls these) |
| POST | `/ack` | Body `{"ids":[...]}` — delete processed items from KV |

Inbound email is handled by the Worker's `email()` trigger (Email Routing), not HTTP.

### Stored record format

Each newsletter in KV contains:
```json
{
  "id": "nl:<sha256>",
  "from": "sender@example.com",
  "subject": "Newsletter Title",
  "html": "...",
  "text": "...",
  "date": "2026-09-01T12:00:00.000Z",
  "headers": [
    { "key": "List-ID", "value": "<newsletter.example.com>" },
    { "key": "X-Campaignid", "value": "abc123" },
    { "key": "Return-Path" },
    { "key": "To" }
  ],
  "raw_size": 45678,
  "received_at": "2026-09-01T12:00:03.000Z"
}
```

`received_at` is when the message reached this Worker. A run acks mail it could not file
(a forward whose quoted sender it cannot read, a sender past a cap) 8 days after it, so no
message waits in KV forever.

**headers**: An array of email header objects. Each object has a `key` field; allowlisted
headers also have a `value` field. Allowlisted headers (case-insensitive):

- `archived-at` — permanent archive URL (some mailing lists)
- `list-id`, `list-post`, `list-archive`, `list-help` — mailing-list metadata
- `x-mc-user`, `x-campaignid`, `x-campaign` — Mailchimp campaign identifiers
- `feedback-id` — ESP feedback-loop identifier
- `content-type` — MIME type
- `x-mailer` — sending software

All other headers (including `return-path`, `received`, `list-unsubscribe`, `to`, `cc`,
`delivered-to`) are stored as `{ "key": "Header-Name" }` — present but value-redacted.
This preserves header presence for diagnostics without leaking routing/recipient details.

**raw_size**: The original MIME message size in bytes (`message.rawSize`), or `null` if unavailable.
Used for throughput and parsing-cost monitoring.

## Deploy

[![Deploy to Cloudflare](https://deploy.workers.cloudflare.com/button)](https://deploy.workers.cloudflare.com/?url=https://github.com/musingfox/cyris/tree/main/workers/newsletter)

Part of the Cloudflare install; the whole order is in
[docs/install-cloudflare.md](../../docs/install-cloudflare.md#optional-workers).
Email Routing still has to be set up by hand — it needs a domain you control,
and it is the one step no button can do.

Prereqs: a domain on your Cloudflare account (Email Routing does **not** work on
`*.workers.dev`), and `wrangler` logged in (`bunx wrangler whoami`).

```bash
cd workers/newsletter
bun install                                   # postal-mime (MIME parser)

# 1. Create the KV namespace, then replace the id in wrangler.toml with it
#    (the committed id belongs to another account)
bunx wrangler kv namespace create NEWSLETTERS  # → copy the printed id into kv_namespaces

# 2. Deploy
bunx wrangler deploy                           # prints https://cyris-newsletter.<sub>.workers.dev

# 3. Set the bearer. Its value is the app's CYRIS_WORKER_TOKEN, which the rss
#    and newsletter Workers share (promote has its own). Secrets cannot be read
#    back, so keep this value. If rss already has one, export
#    CYRIS_WORKER_TOKEN=<it> first; otherwise a new one is generated:
TOKEN=${CYRIS_WORKER_TOKEN:-$(openssl rand -hex 32)}; echo "$TOKEN"
printf 'NEWSLETTER_TOKEN=%s\n' "$TOKEN" > .dev.vars      # local dev
printf '%s' "$TOKEN" | bunx wrangler secret put NEWSLETTER_TOKEN

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

On Cloudflare, from the repo root, set the app's secrets:

```bash
bunx wrangler secret put CYRIS_NEWSLETTER_WORKER_URL --env-file /dev/null  # https://cyris-newsletter.<sub>.workers.dev
bunx wrangler secret put CYRIS_WORKER_TOKEN --env-file /dev/null           # same value as NEWSLETTER_TOKEN
```

The app pulls from this Worker only when both are set. For a local install, the same
two values are `[newsletter] worker_url` in `cyris.toml` and `CYRIS_WORKER_TOKEN` in
`.env`.

## Add a new email-only newsletter

1. Gmail filter: forward that sender → `cyris@<your-domain>`, or subscribe with that
   address.
2. Nothing more is needed. The next run makes a source for a sender no source names
   ([ADR-0050](../../docs/decisions/0050-mail-from-an-unknown-sender-becomes-a-source-the-reader-keeps-or-stops.md)):
   named by its address, `type: newsletter`, lane `read`, and marked *New source*
   on the digest card, where you keep or stop it. `/settings` → Sources stops or
   restores it too. Mail you forward by hand comes from your own address, so list any
   address beside *Email to* under Notifications → *Your other addresses*; cyris then
   files it by the sender the forward quotes.
3. Optional: to give the source a name, lane or tags before its first issue, add it
   yourself with `type: newsletter` and `email_match`. On Cloudflare the source list
   is D1: add it on `/settings` → Sources, or put it in `sources.yaml` and run
   `cyris sources push` (editing `sources.yaml` alone does nothing there). A local
   install reads `sources.yaml` directly.
   ```yaml
   - name: "Example Newsletter"
     type: newsletter
     email_match: "from:author@example.com"
     lane: read
     tags: [tech, business]
   ```
A stopped source's mail is ACKed unread. Mail a run cannot file yet (a forward whose
quoted sender it cannot read, a sender past the run's cap of new sources) stays queued
for 8 days from `received_at`, then is ACKed.

## Local dev

```bash
bunx wrangler dev   # uses .dev.vars; email() can be exercised with `wrangler dev` email test tooling
```
