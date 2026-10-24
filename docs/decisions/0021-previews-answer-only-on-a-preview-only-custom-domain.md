---
status: accepted
date: 2026-10-22
decision-makers: ""
---

# Previews answer only on a Preview-only custom domain, and `preview_urls` stays off

> Superseded in part by [ADR-0015](0015-cloudflare-access-is-the-only-sign-in-and-the-worker-checks-its-jwt.md)
> (2026-10-23). The bullet "No Access on the Preview hostnames", and the token cookie it guards
> each door with, are history: Access is the only sign-in, so the Preview hostnames are an
> Access destination and each Preview sets its own Access team domain and AUD. The Preview-only
> custom domain and `preview_urls = false` still stand.

## Context and Problem Statement

The owner wants a Preview of the app Worker, named `staging`, to try a checkout on before
production gets it (`[previews]` in `wrangler.toml`). A Preview needs a hostname, and Cloudflare
offers two kinds
([Custom domains](https://developers.cloudflare.com/workers/previews/custom-domains/)): a
`workers.dev` Preview URL, `<preview>-cyris-app.<subdomain>.workers.dev`, and a custom-domain
Preview URL, `<preview>.<host>`, under a custom domain `<host>` attached for Preview traffic.

[ADR-0009](0009-a-token-cookie-always-and-access-only-if-you-own-a-domain.md) kept Preview URLs
disabled, because a second public hostname is a second door. Any reachable Preview is such a door,
so the question is which kind of hostname, and what else opens with it.

The `workers.dev` kind is not a switch of its own. `preview_urls` in `wrangler.toml` sets the
Worker's one `previews_enabled` flag, and every `wrangler deploy` applies it again. That flag
governs two things:

* **Version URLs**: one hostname per uploaded production version,
  `<version>-cyris-app.<subdomain>.workers.dev`, running that version with production's secrets
  and bindings
  ([Version URLs](https://developers.cloudflare.com/workers/versions-and-deployments/version-urls/)).
  Wrangler prints a Version URL only while the flag is on (wrangler 4.151.3,
  `wrangler-dist/cli.js:181943`).
* **Previews' `workers.dev` URLs**. Wrangler's own advice for a Preview that has no URL is to set
  `preview_urls = true` and redeploy production (`wrangler-dist/cli.js:182987-183070`).

## Considered Options

* `preview_urls = true`, with Previews on `workers.dev`
* A custom domain attached from the dashboard for Preview traffic only, with `preview_urls` staying
  `false`
* No Preview, with trial deployments (`docs/trial-deployment.md`) as the only place to try a change

## Decision Outcome

Chosen option: "a custom domain for Preview traffic only", because it opens the Previews' own
doors and no others. The owner decided it on 2026-10-22.

* **`preview_urls` stays `false`.** No Version URL and no Preview `workers.dev` URL exists.
* **One custom domain `<host>` is attached to `cyris-app` from the dashboard, enabled for
  Preview only.** The Workers domains API lists it with `enabled: false`: `<host>` itself routes
  no request, and each Preview answers on a subdomain of it.
* **The doors this opens** are these:
  * `staging.<host>`, the Preview URL, which always serves the Preview's latest deployment.
  * `<deployment-id>-staging.<host>`, one per deployment of the Preview, each serving that one
    deployment until it is deleted. Cloudflare keeps the latest 100 deployments of a Preview.
  * `<name>.<host>` for every other Preview of `cyris-app`. `wrangler preview` without `--name`
    names the Preview after the current git branch, so one run from a feature branch opens a
    door named after that branch, and it stays open until `wrangler preview delete`.
* **Each door is guarded by layer 1 of ADR-0009.** Every Preview runs this repository's Worker,
  so the `CYRIS_UI_TOKEN` cookie is checked before a byte reaches the container. A Preview has
  secrets of its own, never production's (`docs/architecture.md` §5, *Where a Preview's settings
  live*), so its token is its own. A Preview created without one is closed, not open: with no
  token, `authorized()` returns false (`workers/app/src/router.js:89-90`), and every protected
  path answers with the sign-in form or a 401.
* **No Access on the Preview hostnames**, by the owner's choice: the cookie alone guards them, as
  it guards the `workers.dev` URL in ADR-0009.

### Consequences

* Good, because no production version gets a hostname. The doors that open serve only Preview
  deployments, which run on their own secrets.
* Good, because `wrangler.toml` stays fork-neutral. The host is attached in the dashboard, as
  production's custom domain is, and a fork that attaches none has no Preview door at all.
* Bad, because every Preview name and every Preview deployment is a door until it is deleted, so a
  branch Preview nobody remembers stays reachable behind whatever token it was given.
* Bad, because the Preview-only domain is listed among `cyris-app`'s custom domains, which is where
  a run picks its digest link's host. `list_worker_domains` (`src/cyris/adapters/cloudflare.py`)
  skips a domain with `enabled: false` for that reason.
* Bad, because it needs a domain on the account. A fork without one cannot reach a Preview unless
  it turns `preview_urls` on, and with it every Version URL.

### Confirmation

`tests/test_worker_route_table.py` asserts `preview_urls = false` in `wrangler.toml`, and
`tests/test_worker_domains.py` holds the digest-host lookup to skipping a Preview-only domain.

## Pros and Cons of the Options

### `preview_urls = true`

* Good, because it needs no domain and no dashboard step.
* Bad, because the same flag gives every uploaded production version a public hostname, running
  production's secrets and bindings, until it is turned off again.
* Bad, because it reverses ADR-0009's bullet outright, not only for Previews.

### No Preview

* Good, because no new door opens.
* Bad, because a trial is a whole second deployment, with its own Worker, D1, KV, Pages project
  and rss Worker, so trying one checkout costs a provisioning run.

## More Information

* This ADR supersedes in part
  [ADR-0009](0009-a-token-cookie-always-and-access-only-if-you-own-a-domain.md). Its bullet
  "Preview URLs stay disabled" holds for Version URLs and Previews' `workers.dev` URLs. Its two
  layers stand unchanged.
* Sources:
  * [Previews](https://developers.cloudflare.com/workers/previews/): the URL formats, and the limit
    of 100 deployments per Preview.
  * [Custom domains](https://developers.cloudflare.com/workers/previews/custom-domains/): the two
    kinds of host, and `preview_urls`.
  * [Version URLs](https://developers.cloudflare.com/workers/versions-and-deployments/version-urls/):
    the same field governs Version URLs.
  * [List domains](https://developers.cloudflare.com/api/resources/workers/subresources/domains/methods/list/):
    the `enabled` and `previews_enabled` fields.
* `docs/architecture.md` §5 (*Where a Preview's settings live*) and §6 describe the Preview as it
  is deployed.
