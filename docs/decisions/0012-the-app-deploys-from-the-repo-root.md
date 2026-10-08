---
status: accepted
date: 2026-10-08
decision-makers: ""
---

# One-button deploy: four Workers, four buttons, and the app's config at the repo root

## Context and Problem Statement

Milestone M6 asks that someone with a clean Cloudflare account can press a button, fill the
secrets and get a digest, with no code edits. On 2026-09-04 the shape was settled against
Cloudflare's [Deploy buttons documentation](https://developers.cloudflare.com/workers/platform/deploy-buttons/),
which contradicted three assumptions the ticket carried:

* There is no `deploy.json`. A button is a README link to
  `deploy.workers.cloudflare.com/?url=<repo>`. Its inputs are the Wrangler config (bindings),
  `.env.example` (which secrets to ask for) and `package.json`'s `cloudflare.bindings` (the
  guidance beside each field).
* A subdirectory must be self-contained, because Cloudflare treats it as the root of the
  repository it clones. `workers/app/` is not: its image is built from the repository's
  `Dockerfile` and installs the Python package.
* Workers Builds does build the container image on the production branch, where it runs
  `wrangler deploy`. This was the gate on the whole shape.

One button deploys one Worker, and cyris has four: `app`, `rss`, `promote` and `newsletter`.

## Considered Options

* Keep the app's config in `workers/app/`, and copy the `Dockerfile` down to make it
  self-contained
* Move the app's `wrangler.toml`, `package.json` and `vitest.config.js` to the repository root
* One button for all four Workers

## Decision Outcome

Chosen option: "the app's config at the repository root, and one button per Worker with the app
as the primary one", because a button can only deploy a self-contained root, and copying the
`Dockerfile` down would duplicate the thing being deployed.

Three steps stay outside the button, and "the button cannot" is not "stays manual": the
deployment holds a Cloudflare token of its own, so anything that token can do, first boot can do.

| Step | Why the button cannot provision it |
|---|---|
| Create the D1 database and paste its UUID as `CYRIS_STORE_DATABASE_ID` | The container reaches D1 over REST, not through a binding. A `[[d1_databases]]` binding would make the deploy create a database whose id nothing can read at runtime, a trick that still ends in a paste |
| Create the Pages project | Deploy buttons support Workers only. Since 2026-09-06 the first publish creates the project itself over the REST API it deploys with |
| Attach a domain, then Cloudflare Access | Neither is in the provisioning list, and Access stays off `workers.dev` ([ADR-0009](0009-a-token-cookie-always-and-access-only-if-you-own-a-domain.md)) |

None of these breaks the acceptance condition, because an id pasted into a secret field is not a
code edit.

### Consequences

* Good, because a clone deploys unmodified, and `.env.example` is the checklist the deploy page
  reads.
* Bad, because wrangler for the app runs from the repository root, beside `.env`. Wrangler loads
  the working directory's dotenv, and a stale `CLOUDFLARE_API_TOKEN` there overrides an OAuth
  login without falling back, so every documented invocation carries `--env-file /dev/null`.
* Bad, because the four Workers are coupled through one D1. `workers/rss/` reads the `sources`
  table the app writes, and the article store and the RSS buffer share one database (`cyris-rss`),
  which is the binding `workers/rss/wrangler.toml` already declares. Provisioned separately they
  get two databases, and the RSS Worker reads an empty table and polls nothing.
* Bad, because the clean-account run of the button has not been done (tracked outside this
  repository); the three wrong assumptions above were found by reading, not by pressing.

## More Information

Extracted on 2026-10-08 from these sources at commit `c353d3f`:

* `docs/architecture.md:870-911` (*What the deploy button can and cannot do*).
* `docs/architecture.md:411-413` (§4, the shared database).
* `docs/architecture.md:1118-1121` (§7 rows #6 to #8b).
