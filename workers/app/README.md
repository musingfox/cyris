# `cyris-app` — the Container and its door

The pipeline itself, and the only route to the triage deck and `/settings`.
Replaces the Mac mini's `docker compose` + supercronic (§7 M5).

- **Hourly cron** → `CYRIS_ROLE=run`: one `cyris run --if-due` plus one
  `cyris promote-sync`, then the process exits and the instance stops billing.
  The tick is unconditional; which hours are digest hours is a D1 setting.
- **Any HTTP request** → `CYRIS_ROLE=ui`: `cyris triage-ui` on port 8766,
  asleep 5 minutes after the last request (`onActivityExpired` → `stop()`).

## Auth

One layer always, a second if you own a domain.

1. **The `CYRIS_UI_TOKEN` secret**, checked in `src/router.js` before anything
   reaches the container. `/login` takes the token and sets an HttpOnly cookie
   holding its SHA-256, compared in constant time. A request without the cookie
   gets the form (browser) or `401` (anything else). Generate one with
   `openssl rand -hex 32` — `/login` refuses to mint a session if the secret is
   shorter than 32 characters. Revocation is rotate `CYRIS_UI_TOKEN`; every
   outstanding cookie dies at once.
2. **Cloudflare Access**, optional, on a hostname you own. Dashboard only, and
   manual on purpose: the domain and the policy are grade-B deployment identity
   (`docs/architecture.md` §5). Access cannot protect `workers.dev`. Set
   `CYRIS_UI_ACCESS_HOST` to that hostname so `/api/vote` on it stays Access-only
   (a reader who already passed Access does not log in again). On every other
   hostname the cookie is required.

A fork on `*.workers.dev` is a complete install: cookie only. A domain is an
optional second layer.

### Putting Access in front (optional)

**Access cannot protect a `workers.dev` URL.** An Access application takes "a
domain from an active zone in your Cloudflare account, or a custom hostname via
Cloudflare for SaaS" — and `workers.dev` is neither.

This repo's root `wrangler.toml` has `workers_dev = true` and no `routes`, so
`wrangler deploy` does not need a domain. Attach a custom domain from the
Cloudflare dashboard (Workers → your worker → Settings → Domains & Routes) or
the API. **Known cost:** routing then lives in two places — this file for
`workers_dev`, the dashboard for the domain — so the Wrangler config is no
longer the sole source of truth for hostnames.

1. Attach the custom domain from the dashboard. The zone must already be active.
2. Zero Trust → Access → Applications → Add an application → **Self-hosted**.
   Set the domain to the hostname you attached, policy Action **Allow**, rule
   **Emails** → your address. Applications are deny-by-default.
3. Check from a browser you are not logged in with: a request carrying a valid
   `cyris_session` cookie still 302s to the Access login. The cookie cannot walk
   past Access.
4. **Only now** set `CYRIS_UI_ACCESS_HOST` to that hostname. The flag makes
   `/api/vote` on it skip the cookie check and trust Access instead, so setting
   it before step 3's 302 receipt leaves those writes open to anyone.

**Consequence worth knowing before you script against an Access host.** Those
paths 302 to `cloudflareaccess.com` rather than 401. Scripting needs an Access
**service token** (`CF-Access-Client-Id` / `CF-Access-Client-Secret` headers,
plus a Service Auth policy). Nothing automated needs this today: the pipeline
talks to D1 and the three Workers directly and never to its own UI.

Cyris does not validate the Access JWT itself. A third copy of layer 1 is not a
third layer.

## Deploy

Containers need Workers Paid, and the image is built locally by `wrangler`, so
Docker must be running. **Run wrangler from the repo root**, where this Worker's
`wrangler.toml` and `package.json` live — a Deploy to Cloudflare button treats
the directory it points at as the whole repository, and this Worker needs the
Dockerfile and the Python package outside `workers/app/`.

**Pass `--env-file /dev/null` every time.** Wrangler loads the cwd's `.env`, and
the repo root's holds a `CLOUDFLARE_API_TOKEN` that silently overrides your OAuth
login without falling back when it expires. The receipt: `wrangler whoami` from
the root reports an *Account API Token*; the same command with
`--env-file /dev/null` reports the *OAuth Token*.

```sh
bun install                       # or npm install

# CYRIS_UI_TOKEN: openssl rand -hex 32
# DIGEST_ORIGIN: https://<your-pages-project>.pages.dev
# CYRIS_UI_ACCESS_HOST: only after Access is verified blocking (step 4 above)
for s in CYRIS_UI_TOKEN DIGEST_ORIGIN \
         CLOUDFLARE_ACCOUNT_ID CLOUDFLARE_API_TOKEN \
         CLOUDFLARE_EMBEDDING_API_TOKEN CYRIS_WORKER_TOKEN \
         CYRIS_PROMOTE_TOKEN CYRIS_PROMOTE_WORKER_URL CYRIS_DISCORD_WEBHOOK_URL \
         ANTHROPIC_API_KEY GEMINI_API_KEY OPENAI_API_KEY; do
  bunx wrangler secret put "$s" --env-file /dev/null
done

bunx wrangler deploy --env-file /dev/null   # builds ./Dockerfile, pushes, deploys
```

Grade-B identity is the same names the Python process already reads
(`CYRIS_STORE_BACKEND`, `CYRIS_STORE_DATABASE_ID`, `CYRIS_HTML_OUTPUT_ENABLED`,
`CYRIS_PROMOTE_PUBLISH_ENABLED`, `CYRIS_PROMOTE_PAGES_PROJECT`,
`CYRIS_PROMOTE_CUSTOM_DOMAIN`, `CYRIS_PROMOTE_WORKER_URL`,
`CYRIS_NEWSLETTER_WORKER_URL`, `CYRIS_RSS_WORKER_URL`) plus two Worker-only
keys: `DIGEST_ORIGIN` (the Pages origin this Worker proxies) and
`CYRIS_UI_ACCESS_HOST` (the hostname Access is bound to; unset = cookie-only).
Set each as a Worker secret or in `[vars]` on your own fork. Leave a name unset
or empty and the Worker omits it from the container env rather than forwarding
the string `undefined`. This repo does not ship values for them.

On a domainless deploy, set `CYRIS_PROMOTE_CUSTOM_DOMAIN` to your
`*.workers.dev` hostname so Discord links land where `/api/vote` exists. If it
stays empty, readers follow `pages.dev` and the vote probe 404s.

The image is built from the repo root. Settings and sources are read from D1;
changing a *setting* is a write on `/settings`. Worker URLs and the Pages
project can come from the bindings above without a rebuild.

The image must be `linux/amd64`. On an Apple Silicon machine that is emulation,
so the first build is slow.

## Cutting over

The Mac mini and this Worker run the same pipeline against the same D1 and the
same Pages project. **Two schedulers publishing one manifest is the failure
mode**, so stop the local one in the same sitting:

```sh
docker compose down       # in the repo root, on the Mac mini
```

Verify the cloud side first: `POST /run` (authenticated) fires the tick by hand
and should advance `pages_manifest`.
