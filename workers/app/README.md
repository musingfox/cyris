# `cyris-app` — the Container and its door

The pipeline itself, and the only route to the triage deck and `/settings`.
Replaces the Mac mini's `docker compose` + supercronic (§7 M5).

- **Hourly cron** → `CYRIS_ROLE=run`: one `cyris run --if-due` plus one
  `cyris promote-sync`, then the process exits and the instance stops billing.
  The tick is unconditional; which hours are digest hours is a D1 setting.
- **Any HTTP request** → `CYRIS_ROLE=ui`: `cyris triage-ui` on port 8766,
  asleep 5 minutes after the last request (`onActivityExpired` → `stop()`).

## Auth

Two layers, deliberately:

1. **Cloudflare Access** on the Worker's route — who you are (email policy, MFA,
   audit log). Dashboard only, and manual on purpose: the domain and the policy
   are grade-B deployment identity (`docs/architecture.md` §5), so automating
   them here would tie the repo to one account. It needs a custom
   domain: `workers.dev` cannot be put behind Access at all. This deployment is
   routed at **`digest.musingfox.me`** with `workers_dev = false`; the
   application itself is still to be created — see below.
2. **The `CYRIS_UI_TOKEN` secret**, checked in `src/index.js` before anything
   reaches the container. `/login` takes the token and sets an HttpOnly cookie
   holding its SHA-256. A request without the cookie gets the form (browser) or
   `401` (anything else).

Layer 2 deploys with the Worker, so a route that has not been put behind Access
yet is still not an open write surface.

### Putting Access in front (the part that is not `wrangler deploy`)

**Access cannot protect a `workers.dev` URL.** An Access application takes "a
domain from an active zone in your Cloudflare account, or a custom hostname via
Cloudflare for SaaS" — and `workers.dev` is neither. So the order is: give the
Worker a hostname you own, close the one you don't, then write the policy.

1. ~~**Route the Worker at your own domain.**~~ Done 2026-08-30:

   ```toml
   routes = [{ pattern = "digest.musingfox.me", custom_domain = true }]
   ```

   `wrangler deploy` created the DNS record. The zone must already be active on
   the account.

2. ~~**Turn `workers.dev` off in the same change**~~ — `workers_dev = false`,
   done in the same deploy. It is the step that is easy to skip and expensive to
   skip: Access binds to one hostname, so leaving
   `cyris-app.<subdomain>.workers.dev` reachable leaves a door Access does not
   cover, and the whole layer becomes decorative. (The token check still holds
   it, which is the point of having two layers, but a layer you believe in and
   do not have is worse than one you know you lack.) Receipt: the workers.dev
   URL answers **404**, the custom domain answers 401 unauthenticated and serves
   the deck once logged in.

3. ~~**Create the application.**~~ Done 2026-08-30, on team domain
   `musingfox.cloudflareaccess.com`. Zero Trust → Access → Applications → Add an
   application → **Self-hosted**. Name it, set the domain to
   `digest.musingfox.me`, and add a policy: Action **Allow**, rule **Emails** →
   your address. Applications are deny-by-default, so no other rule is needed.

4. ~~**Check it from a browser you are not logged in with.**~~ Verified with
   `curl`, which is stricter than a private window: a request carrying a *valid*
   `cyris_session` cookie still 302s to the Access login. Layer 2 cannot be used
   to walk past layer 1.

**Consequence worth knowing before you script against this.** Every path is
behind Access now, `/api/*` included, so a scripted client gets a 302 to
`cloudflareaccess.com` rather than a 401 — the `CYRIS_UI_TOKEN` cookie is no
help. Scripting needs an Access **service token** (`CF-Access-Client-Id` /
`CF-Access-Client-Secret` headers, plus a Service Auth policy on the
application). Nothing automated needs this today: the pipeline talks to D1 and
the three Workers directly and never to its own UI.

Cyris does not validate the Access JWT itself. It could — the
`Cf-Access-Jwt-Assertion` header is there, and Cloudflare documents verifying it
against the team JWKS — but a request only reaches the Worker after Access has
allowed it, so verifying again would only defend against someone who can already
route traffic to the origin. The two layers are already independent; a third
copy of layer 1 is not a third layer.

## Deploy

Containers need Workers Paid, and the image is built locally by `wrangler`, so
Docker must be running. **Run wrangler from this directory** — from the repo
root it reads `.env` and an expired `CLOUDFLARE_API_TOKEN` there silently
overrides your OAuth login.

```sh
bun install                       # or npm install

# Every secret the container needs. CYRIS_UI_TOKEN is the login above; the rest
# are the same values .env holds for a local run. CYRIS_PROMOTE_WORKER_URL should
# match [promote] worker_url in cyris.toml.
for s in CYRIS_UI_TOKEN CLOUDFLARE_ACCOUNT_ID CLOUDFLARE_API_TOKEN \
         CLOUDFLARE_EMBEDDING_API_TOKEN CYRIS_WORKER_TOKEN \
         CYRIS_PROMOTE_TOKEN CYRIS_PROMOTE_WORKER_URL CYRIS_DISCORD_WEBHOOK_URL \
         ANTHROPIC_API_KEY GEMINI_API_KEY OPENAI_API_KEY; do
  bunx wrangler secret put "$s"
done

bunx wrangler deploy              # builds ../../Dockerfile, pushes, deploys
```

The image is built from the repo root and includes `cyris.toml` and
`sources.yaml` — the Container mounts nothing, and those two carry the
deployment identity. Neither is a source of truth: settings and sources are read
from D1 with these as the fallback. Changing a *setting* is a write on
`/settings`; changing a worker URL or the Pages project is still a rebuild until
§7 M6.

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
