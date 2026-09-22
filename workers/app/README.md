# `cyris-app` — the Container and its door

The Worker that runs the pipeline and the only route to `/settings`. To deploy it, follow
[docs/install-cloudflare.md](../../docs/install-cloudflare.md); to update, roll back or
check what it runs, see [docs/operations.md](../../docs/operations.md). Its
`wrangler.toml` is at the repo root, because the image is built from the whole
repository: run wrangler from there.

## Roles

One image, started in one of two roles:

- **Hourly cron** → `CYRIS_ROLE=run`: one `cyris run --if-due` plus one
  `cyris promote-sync`, then the process exits and the instance stops billing. The
  tick is unconditional; which hours are digest hours is a D1 setting.
  `POST /run?period=morning|evening` starts the same role with that period, skipping
  the schedule check.
- **Any HTTP request** → `CYRIS_ROLE=ui`: `cyris triage-ui` on port 8766, asleep 5
  minutes after the last request (`onActivityExpired` → `stop()`).

Every other path is the public archive: the Worker proxies it from
`https://<CYRIS_PROMOTE_PAGES_PROJECT>.pages.dev`, or from `DIGEST_ORIGIN` when set.
Every HTML page it serves (the archive, each issue, `/settings`) gets the D1 type size
as `<style>html:root{--type-scale:X}</style>`, read over REST at most once a minute
per isolate; `pages.dev` direct stays at 1.

The Worker forwards its secrets into the container's environment, except
`CYRIS_UI_TOKEN`, `DIGEST_ORIGIN` and `CYRIS_UI_ACCESS_HOST`, which only the Worker
reads (`src/index.js`). An unset or empty one is left out rather than passed as the
string `undefined`.

## Auth

One layer always, a second if you own a domain.

1. **The `CYRIS_UI_TOKEN` secret**, checked in `src/router.js` before anything reaches
   the container. `/login` takes the token and sets an HttpOnly cookie,
   `cyris_session`, holding its SHA-256, compared in constant time. A request without
   the cookie gets the login form (browser) or `401` (anything else). `/login` refuses
   to start a session if the secret is shorter than 32 characters. Revocation is
   rotating `CYRIS_UI_TOKEN`; every outstanding cookie dies at once.
2. **Cloudflare Access**, optional, on a hostname you own. Access cannot protect
   `workers.dev`. With `CYRIS_UI_ACCESS_HOST` set to that hostname, `/api/vote` on it
   trusts Access instead of the cookie, so a reader who already passed Access does not
   log in again. On every other hostname the cookie is required. The setup order
   matters; it is in the
   [install guide](../../docs/install-cloudflare.md#optional-cloudflare-access-on-your-own-domain).

A fork on `*.workers.dev` is a complete install with the cookie alone. Cyris does not
validate the Access JWT itself.

Protected paths are `/settings`, `/login`, `/run`, `/api/*` and `/static/*`. Behind
Access they 302 to `cloudflareaccess.com` rather than 401, so a script against an
Access hostname needs an Access service token. Nothing in cyris calls its own UI.
