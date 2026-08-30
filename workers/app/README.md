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
   them here would tie the repo to one account. Zero Trust → Access →
   Applications → Add → Self-hosted, hostname = this Worker's route, policy =
   Allow / Emails / your address.
2. **The `CYRIS_UI_TOKEN` secret**, checked in `src/index.js` before anything
   reaches the container. `/login` takes the token and sets an HttpOnly cookie
   holding its SHA-256. A request without the cookie gets the form (browser) or
   `401` (anything else).

Layer 2 deploys with the Worker, so a route that has not been put behind Access
yet is still not an open write surface.

## Deploy

Containers need Workers Paid, and the image is built locally by `wrangler`, so
Docker must be running. **Run wrangler from this directory** — from the repo
root it reads `.env` and an expired `CLOUDFLARE_API_TOKEN` there silently
overrides your OAuth login.

```sh
bun install                       # or npm install

# Every secret the container needs. CYRIS_UI_TOKEN is the login above; the rest
# are the same values .env holds for a local run.
for s in CYRIS_UI_TOKEN CLOUDFLARE_ACCOUNT_ID CLOUDFLARE_API_TOKEN \
         CLOUDFLARE_EMBEDDING_API_TOKEN CYRIS_WORKER_TOKEN \
         CYRIS_DISCORD_WEBHOOK_URL \
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
