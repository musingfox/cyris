# Installing cyris on Cloudflare

The deployment is one Worker, `cyris-app`, fronting a Container. An hourly Cron Trigger
starts the pipeline on the two publish hours, and it runs once and exits; any HTTP request wakes the `/settings`
server, which sleeps again after five idle minutes. State lives in D1, and the digest
is published to Cloudflare Pages. Three more Workers are optional and are added at the
end.

Every step below is for a fresh fork. If you already run cyris locally, do the steps
in [Moving to Cloudflare](install-local.md#moving-to-cloudflare-later) first.

These steps have been verified on one Cloudflare account only, the maintainer's, which
already held resources from earlier versions; nobody has yet run them from scratch on a
new account. If a step fails on yours, an issue naming the step and the error is the
fastest way to get it fixed.

## Prerequisites

- **Workers Paid** on your Cloudflare account (US$5/mo). Containers are not available on
  the Free plan, so upgrade before the first deploy. What one deployment actually
  spends is in [hosting-and-cost.md](hosting-and-cost.md).
- **Docker running.** `wrangler deploy` builds the image from `./Dockerfile` on your
  machine. The image must be `linux/amd64`; on Apple Silicon that is emulation, so the
  first build is slow.
- **bun** (or npm) for wrangler. Python and uv are not needed to deploy, only to run
  the CLI later.
- An LLM API key (Anthropic, Gemini, OpenAI or a Workers AI token), or none if you
  choose plain excerpts.

## Run wrangler from the repo root, always with `--env-file /dev/null`

The app's `wrangler.toml` is at the repo root, because the image is built from the
whole repository. Wrangler also loads the `.env` in the directory it runs from. If you
ever create a `.env` there for the CLI, its `CLOUDFLARE_API_TOKEN` silently replaces
your `wrangler login` session, and wrangler does not fall back when that token lacks a
permission or expires. Every wrangler command in this guide therefore carries
`--env-file /dev/null`; keep it even if you have no `.env` yet.

```sh
git clone https://github.com/<you>/cyris.git && cd cyris
bun install
bunx wrangler login
```

## 1. Create the D1 database

```sh
bunx wrangler d1 create cyris --env-file /dev/null
```

Note the database UUID it prints. The container reaches D1 over the REST API rather
than through a binding, so nothing needs adding to `wrangler.toml`; ignore the binding
snippet wrangler prints. The tables are created the first time cyris reaches the
database.

## 2. Choose a Pages project name

The digest is published to a Pages project that the first publish creates. The code
assumes the project is served at `https://<name>.pages.dev`, and Pages gives a name
that is already taken a randomly suffixed subdomain instead. Pick a name nobody holds:
`https://<name>.pages.dev` should fail to load or show a Cloudflare 404 today. A
common name such as `cyris` is likely taken.

## 3. Create an API token

In the dashboard, My Profile → API Tokens → Create Token → Custom token, with these
account permissions:

- **D1 → Edit**: the store, the settings and the source list, including creating
  the tables.
- **Cloudflare Pages → Edit**: creating the project and uploading each digest.
- **Workers Scripts → Read**: listing the custom domains attached to `cyris-app`, so
  the Discord digest link names one. Without it, the link falls back to `pages.dev`
  and `cyris doctor` warns.
- **Email Sending → Edit**: mailing each digest, needed only if you set an email
  address in step 6.

Note your account id as well. Workers AI takes a separate token (step 4), needed only
if you use it.

## 3a. Put Cloudflare Access in front of the Worker

Access is the only sign-in. The Worker answers `403` to every protected path without a
valid Access JWT, so the Access application comes first: its AUD tag and your team domain
go into the secrets file in step 4, and a deploy before they are set leaves `/settings`
answering `403`.

1. Zero Trust → Access → Applications → Add an application → **Self-hosted**. The
   application domains are every hostname the Worker answers on: `cyris-app.<subdomain>.workers.dev`,
   and your custom domain if you set one. Destinations are `/settings`, `/labels`, `/login`, `/run`, `/api/*`
   and `/static/*` on that hostname. If you set `CYRIS_PRIVATE_ARCHIVE=true`, make the
   destination the whole hostname instead, so the archive sits behind the same sign-in.
2. Add two policies to the application. An **Allow** policy with the one-time PIN method and
   your address, for readers. A **Service Auth** policy for scripts, which needs a service
   token: Zero Trust → Access → Service Auth → Create Service Token.
3. Copy the application's **Application Audience (AUD) tag** from its overview into
   `CYRIS_ACCESS_AUD`. Set `CYRIS_ACCESS_TEAM_DOMAIN` to your team domain as a host without a
   scheme, such as `yourteam.cloudflareaccess.com`. The Worker uses the value as given: with
   `https://` or a trailing slash in it, every sign-in is refused.
4. Check from a browser you are not logged in with: `/settings` must redirect to the Access
   login, not show the page.

To revoke a script's access, delete its service token in the Zero Trust dashboard (Access →
Service Auth). Its two values, `CYRIS_ACCESS_CLIENT_ID` and `CYRIS_ACCESS_CLIENT_SECRET`, are
secrets: keep them in your local `.env`, which the CLI reads, and nowhere else.

## 4. Write the secrets file

Write the secrets into a file that git ignores. `.gitignore` covers `.env.*`, so
`.env.cloud` in the repo root is safe:

```sh
# .env.cloud — only the names you are setting; a blank line uploads an empty value
CYRIS_ACCESS_TEAM_DOMAIN=<your team domain, e.g. yourteam.cloudflareaccess.com>
CYRIS_ACCESS_AUD=<the Application Audience tag from step 3a>
CYRIS_STORE_DATABASE_ID=<the UUID from step 1>
CLOUDFLARE_ACCOUNT_ID=<your account id>
CLOUDFLARE_API_TOKEN=<the token from step 3>
CYRIS_PROMOTE_PAGES_PROJECT=<the name from step 2>
ANTHROPIC_API_KEY=<your key, if you use Anthropic>
```

Do not upload a copy of `.env.example` with blanks: an empty `CYRIS_ACCESS_TEAM_DOMAIN`
answers `403` on every protected path until it is set.

The six required names:

| Name | What it is |
|---|---|
| `CYRIS_ACCESS_TEAM_DOMAIN` | Your Access team domain, a host without a scheme (step 3a) |
| `CYRIS_ACCESS_AUD` | The Application Audience tag of the Access application (step 3a) |
| `CYRIS_STORE_DATABASE_ID` | The D1 UUID |
| `CLOUDFLARE_ACCOUNT_ID` | Your account id |
| `CLOUDFLARE_API_TOKEN` | The D1 + Pages token |
| `CYRIS_PROMOTE_PAGES_PROJECT` | The Pages project name |

Optional names, by feature. Add them to the same file now or later:

| Feature | Names |
|---|---|
| LLM provider | `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`, `CLOUDFLARE_AI_TOKEN` (Workers AI → Read; blank falls back to `CLOUDFLARE_EMBEDDING_API_TOKEN`), or `CLOUDFLARE_AIG_TOKEN` (AI Gateway → Run, for `ai_gateway`; no fallback) |
| Vote similarity | `CLOUDFLARE_EMBEDDING_API_TOKEN` (Workers AI → Read) for `bge-m3`, or `GEMINI_API_KEY` |
| Discord links point at this Worker, where votes work | `CYRIS_PROMOTE_CUSTOM_DOMAIN`, a bare hostname: `cyris-app.<subdomain>.workers.dev` on a domainless deploy, or your own hostname |
| The archive lives somewhere other than `<project>.pages.dev` | `DIGEST_ORIGIN` |
| The archive behind Access, not public | `CYRIS_PRIVATE_ARCHIVE=true`; Worker-only, the container never sees it |
| RSS buffer, newsletter and vote Workers | see [Optional Workers](#optional-workers) |

## 5. Deploy the Worker

```sh
bunx wrangler deploy --env-file /dev/null --secrets-file .env.cloud
```

This builds the image, pushes it, and deploys `cyris-app` with its Durable Object, the
hourly cron and the secrets in one version, so the Worker is never live without its
token. `--secrets-file` is additive: a name you leave out keeps its current value, so
later you can add names to the file and deploy again, or set one with
`bunx wrangler secret put <NAME> --env-file /dev/null`.

The output names the Worker's URL, `https://cyris-app.<subdomain>.workers.dev`.

The Discord digest link names the custom domain attached to `cyris-app`, found at
each run through the Workers Scripts → Read permission from step 3, with or without
Access. On a domainless deploy, set `CYRIS_PROMOTE_CUSTOM_DOMAIN` to the `workers.dev`
hostname; set, it overrides the lookup. With neither, Discord links go to `pages.dev`,
where `/api/vote` does not exist, so readers never see vote buttons.

## 6. Log in and fill `/settings`

Open `https://cyris-app.<subdomain>.workers.dev/settings`. Access asks for your one-time PIN
first, then sends you back to the page you asked for. The archive has nothing to serve
until the first digest is published, so `/settings` is where you start.

The first request after a deploy starts the container, so the page can take a minute;
right after the very first deploy, container routes can fail for several minutes
while Cloudflare provisions it.

`/settings` marks every setting that D1 does not hold yet. All 23 are required and
none has a default in code, so save every category until nothing is marked:

- **Model**: the LLM provider and model, checked against the live API before it is
  stored, or `None — plain excerpts`. Save Vote similarity here too, even while it
  stays off; its fields are stored as one unit.
- **Digest** and **Pipeline**: publish hours, timezone, output language, limits and
  thresholds.
- **Notifications**: a Discord webhook, or turn it off; and an email recipient and
  sender, or leave the recipient empty. The recipient must first be a verified
  destination address (Email Service → Email Routing → Destination Addresses, then
  open the verification mail), and the sender must be an address on your Email Routing
  domain. Save sends a test message and stores the pair only if it is delivered.
- **Sources**: add at least one RSS source. Each takes a tier; see
  [How sources are processed](sources.md).

Until all 23 are saved, every run stops with `Missing settings in D1: …`; with no
source it stops with `No sources in D1`. `cyris settings push` and
`cyris sources push` do the same from a clone (see
[Running the CLI against the deployment](#running-the-cli-against-the-deployment)).

## 7. Run the first digest now

The hourly tick starts the pipeline only on the publish hours. To get a digest without
waiting, `POST /run` with a period. `/settings` has no button for it, so use curl with the
Access service token from step 3a:

```sh
APP=https://cyris-app.<subdomain>.workers.dev
CYRIS_ACCESS_CLIENT_ID=<the service token's client id>
CYRIS_ACCESS_CLIENT_SECRET=<the service token's client secret>
printf 'header = "CF-Access-Client-Id: %s"\nheader = "CF-Access-Client-Secret: %s"\n' \
  "$CYRIS_ACCESS_CLIENT_ID" "$CYRIS_ACCESS_CLIENT_SECRET" \
  | curl -s -X POST --config - "$APP/run?period=morning"
```

It answers `{"started":"run", …}` at once; the run takes minutes. `period` is
`morning` or `evening`. A plain `POST /run` without a period behaves like the cron
tick: outside the publish hours the reply is still `{"started":"run", …}`, and the log
(step 8) says `Not a digest hour`.

## 8. Read the run's log

The container's output goes only to Workers Logs, kept 7 days on Workers Paid. Open
the dashboard at Workers & Pages → `cyris-app` → Observability, or stream it live:

```sh
bunx wrangler tail cyris-app --env-file /dev/null
```

Every run ends with one `run_summary` JSON line: status, counts, LLM spend and wall
time. When `status` is `ok`, the digest is at
`https://<project>.pages.dev/<date>-<period>`, and the Worker's own URL serves the
archive.

## Optional Workers

Each is a separate deploy with its own README, and each is wired into the app through
`cyris-app` secrets. They are independent of one another.

| Worker | What it adds | Needs |
|---|---|---|
| [`workers/promote`](../workers/promote/README.md) | 👍/👎 votes on the digest and raw page | KV; a reader logged in on the app's hostname |
| [`workers/rss`](../workers/rss/README.md) | Hourly feed buffer, so busy feeds do not expire between digests | Workers Paid; the app's D1, after the app has booted |
| [`workers/newsletter`](../workers/newsletter/README.md) | Email-only newsletters | Your own domain on Cloudflare with Email Routing |

Each of these Workers' `wrangler.toml` ships ids that belong to another account: a KV
namespace id in promote and newsletter, a D1 database in rss. Replace them as that
Worker's README says before its first `wrangler deploy`.

Two bearer tokens connect them. `CYRIS_WORKER_TOKEN` on the app is shared by rss
(`RSS_TOKEN`) and newsletter (`NEWSLETTER_TOKEN`). `CYRIS_PROMOTE_TOKEN` on the app is
promote's alone (`PROMOTE_TOKEN`); keep it a different value.

Set the app side from the repo root once each Worker is deployed:

```sh
# promote
bunx wrangler secret put CYRIS_PROMOTE_WORKER_URL --env-file /dev/null  # https://cyris-promote.<subdomain>.workers.dev
bunx wrangler secret put CYRIS_PROMOTE_TOKEN --env-file /dev/null       # = its PROMOTE_TOKEN
# rss
bunx wrangler secret put CYRIS_RSS_WORKER_URL --env-file /dev/null      # https://cyris-rss.<subdomain>.workers.dev
bunx wrangler secret put CYRIS_WORKER_TOKEN --env-file /dev/null        # = its RSS_TOKEN
# newsletter
bunx wrangler secret put CYRIS_NEWSLETTER_WORKER_URL --env-file /dev/null
bunx wrangler secret put CYRIS_WORKER_TOKEN --env-file /dev/null        # = its NEWSLETTER_TOKEN, same value as RSS_TOKEN
```

rss and newsletter are used only when both the URL and `CYRIS_WORKER_TOKEN` are set.
promote with a URL but no token has every vote refused.

**rss comes after the app's first boot.** It reads its feed list from the app's D1
`sources` table and creates only its own `articles` table. Deployed against a D1 the
app has not reached yet, every poll fails with `could not read sources from D1: … no
such table`; with the table present but empty it logs `sources table is empty`.

Email-only sources (`type: newsletter` with `email_match`) go into D1 like any other
source: on `/settings` → Sources, or with `cyris sources push`. On a D1 deployment
`sources.yaml` alone does nothing.

## Running the CLI against the deployment

Work with no UI (bulk article changes, `sources push`, `doctor`) runs from a clone,
because every command reaches D1 over REST. Use a clone with **no `cyris.toml`**: a
value set in that file wins over the environment, so a copied example would keep the
CLI on the local JSON store. Put this in the clone's `.env`:

```sh
CYRIS_STORE_BACKEND=d1
CYRIS_STORE_DATABASE_ID=<the UUID>
CLOUDFLARE_ACCOUNT_ID=<your account id>
CLOUDFLARE_API_TOKEN=<the D1 + Pages token>
CYRIS_HTML_OUTPUT_ENABLED=true
CYRIS_PROMOTE_PUBLISH_ENABLED=true
CYRIS_PROMOTE_PAGES_PROJECT=<the project name>
ANTHROPIC_API_KEY=<the key for the provider stored in D1>
CYRIS_ACCESS_CLIENT_ID=<the service token's client id, for doctor --deployment>
CYRIS_ACCESS_CLIENT_SECRET=<the service token's client secret, for doctor --deployment>
# Only if you deployed the optional Workers, so doctor checks them too:
CYRIS_RSS_WORKER_URL=
CYRIS_NEWSLETTER_WORKER_URL=
CYRIS_WORKER_TOKEN=
CYRIS_PROMOTE_WORKER_URL=
CYRIS_PROMOTE_TOKEN=
```

`cyris settings push` needs a `cyris.toml` to copy from and `cyris sources push` needs
a `sources.yaml`; those two commands always write to D1 whatever the backend. Keep that
file under another name and pass it explicitly (`cyris settings push --config
settings.toml`), so the clone's other commands still find no `cyris.toml`. Keep the
`--env-file /dev/null` habit in this clone: the `.env` now holds a
`CLOUDFLARE_API_TOKEN` that wrangler would pick up.

`cyris doctor` then checks the deployment's configuration. Two lines are expected to
fail and do not mean anything is wrong:

- **publishing**, before the first digest exists: the Pages project is created by the
  first publish, so the API answers "not found", and the hint about the token's Pages
  permission does not apply.
- **deployment image**, with `--deployment`: an image built by `wrangler deploy` has no
  commit baked in, so doctor cannot date it. See [operations.md](operations.md).

Do not run `cyris run` from this clone while the Worker's cron is active: two
schedulers publishing one Pages project is the failure mode.

The rest of the CLI, for work with no UI (`cyris --help` lists everything):

```
cyris doctor                  Before the first run, and after any config change: exits
                              non-zero on anything that would break a run
cyris store migrate|diff      The move into D1, and the comparison to run before you
                              trust it
cyris articles list|accept|   Bulk work on the store: the only way to reach pending
      reject|score|clean      rows outside an issue, and to delete old rows
cyris llm-compare             Digest one window with several providers, side by side,
cyris embed-compare           or judge it with both embedding providers, before
cyris vote-sim                switching; vote-sim previews what similarity would
                              suppress before you enable it
```

## The Deploy to Cloudflare button (experimental)

[![Deploy to Cloudflare](https://deploy.workers.cloudflare.com/button)](https://deploy.workers.cloudflare.com/?url=https://github.com/musingfox/cyris)

The button clones the repository into your GitHub account and deploys it with Workers
Builds, asking for each secret in `.env.example`. Cloudflare's button documentation
does not mention Containers, and nobody has yet deployed this repository by button, so
treat it as unverified; the manual steps above are the supported path. If you try it,
upgrade to Workers Paid first, do steps 1 to 3 before pressing it, fill only the five
required secrets, and continue from step 6. In a fork, change the URL in the button to
your own repository.

## Troubleshooting

- **`Missing required environment variables: …`** in the log: a required secret is
  missing. Set it with `secret put` or `secret bulk`.
- **`Missing settings in D1: …`**: save every `/settings` category, Vote similarity
  on the Model tab included.
- **`No sources in D1`**: add a source on `/settings` → Sources.
- **`Not a digest hour (…)`** after `POST /run`: add `?period=morning` or
  `?period=evening`.
- **The Worker's root answers 503** "Neither CYRIS_PROMOTE_PAGES_PROJECT nor
  DIGEST_ORIGIN is set": set `CYRIS_PROMOTE_PAGES_PROJECT`. Before the first publish
  the archive is empty and pages 404.
- **Every run ends `publish_failed`**: check that the Pages project name was free (step
  2) and that the token has Pages Edit.
- **`/settings` answers `403` with a detail that names `CYRIS_ACCESS_TEAM_DOMAIN` or
  `CYRIS_ACCESS_AUD`**: set the one it names (step 3a), then deploy again.
- **Container routes fail right after the first deploy**: provisioning takes several
  minutes; wait and check the log.
- **rss logs `no such table`**: the app has not reached this D1 yet, or rss points at
  another database. **`sources table is empty`**: add a source.
- **wrangler acts on the wrong account or reports a permission error**: a `.env` in the
  current directory is supplying `CLOUDFLARE_API_TOKEN`; add `--env-file /dev/null`.
