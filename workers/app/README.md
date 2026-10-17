# `cyris-app` — the Container and its door

The Worker that runs the pipeline and the only route to `/settings` and `/labels`. To deploy it, follow
[docs/install-cloudflare.md](../../docs/install-cloudflare.md); to update, roll back or
check what it runs, see [docs/operations.md](../../docs/operations.md). Its
`wrangler.toml` is at the repo root, because the image is built from the whole
repository: run wrangler from there.

## Roles

One image, started in one of two roles:

- **Hourly cron** → `CYRIS_ROLE=run`: one `cyris run --if-due` plus one
  `cyris promote-sync`, then the process exits and the instance stops billing. Which
  hours are digest hours is a D1 setting, and the Worker reads it before starting the
  container (`src/schedule.js`), so the other 22 ticks start nothing. When it cannot
  read the setting it starts the container and `--if-due` decides.
  `POST /run?period=morning|evening` starts the same role with that period, skipping
  the schedule check. `&date=YYYY-MM-DD` names the issue's date as well; it must be
  today or yesterday in the deployment's time zone (the onboarding's first issue is
  yesterday's evening before the morning hour), and only its format is checked when the
  time zone cannot be read from D1.
- **Any HTTP request** → `CYRIS_ROLE=ui`: `cyris triage-ui` on port 8766, asleep 5
  minutes after the last request (`onActivityExpired` → `stop()`).

Every other path is the archive: the Worker proxies it from
`https://<CYRIS_PROMOTE_PAGES_PROJECT>.pages.dev`, or from `DIGEST_ORIGIN` when set. It is
public unless `CYRIS_PRIVATE_ARCHIVE` is `"true"`, which answers a request without a valid
Access sign-in with `403`.
Every HTML page it serves (the archive, each issue, `/settings`) gets the D1 type size
as `<style>html:root{--type-scale:X}</style>`, read over REST at most once a minute
per isolate; `pages.dev` direct stays at 1.

The Worker forwards its secrets into the container's environment, except
`CYRIS_ACCESS_TEAM_DOMAIN`, `CYRIS_ACCESS_AUD`, `DIGEST_ORIGIN` and `CYRIS_PRIVATE_ARCHIVE`,
which only the Worker reads (`src/index.js`). An unset or empty one is left out rather than passed as the
string `undefined`.

## Auth

Cloudflare Access is the only sign-in. Why this shape:
[ADR-0015](../../docs/decisions/0015-cloudflare-access-is-the-only-sign-in-and-the-worker-checks-its-jwt.md)
(supersedes [ADR-0009](../../docs/decisions/0009-a-token-cookie-always-and-access-only-if-you-own-a-domain.md)).

1. **Access decides who.** A self-hosted Access application covers the protected paths
   (`/settings`, `/labels`, `/login`, `/run`, `/api/*`, `/static/*`) on every hostname the
   Worker answers; with `CYRIS_PRIVATE_ARCHIVE` set to `"true"`, it covers the whole hostname.
   The public archive is not an Access destination.
2. **The Worker checks the JWT.** On each protected path, and on every path when the archive is
   private, the Worker verifies the `Cf-Access-Jwt-Assertion` header with jose against the team
   domain's signing keys, checks its issuer and its audience, and answers before a byte reaches
   the container. A request without a valid JWT gets `403` with a JSON body,
   `{"error":"forbidden","detail":"..."}`, whatever its method or `Accept` header. The
   detail names the missing setting when one is unset.
3. **Two Worker settings.** `CYRIS_ACCESS_TEAM_DOMAIN` is the team domain as a host without a
   scheme, such as `yourteam.cloudflareaccess.com`. `CYRIS_ACCESS_AUD` is the Application
   Audience (AUD) tag of the Access application. With either unset, every protected path and
   the private archive answer `403`; nothing falls through to the container. Set both as in the
   [install guide](../../docs/install-cloudflare.md#3a-put-cloudflare-access-in-front-of-the-worker).

A script signs in with an Access service token, sent as `CF-Access-Client-Id` and
`CF-Access-Client-Secret`. `cyris doctor --deployment` and the deploy workflow's `verify` step
send it and read `/api/build`, at whichever hostname the Worker answers on.
