---
status: accepted
date: 2026-10-08
decision-makers: ""
---

# A token cookie guards the app always, and Cloudflare Access is an optional second layer

## Context and Problem Statement

The app Worker fronts a write surface: `/settings`, `/api/*` and the vote endpoint change what
the pipeline does and record human verdicts. It has to be safe on the public internet for the
owner's deployment, which has a custom domain, and for a fork that deploys to `*.workers.dev`
with no domain at all.

Cloudflare Access can protect a hostname the account owns, but it is configured in the dashboard,
not by the repository, and scripts such as `cyris doctor --deployment` and the deploy workflow's
`verify` step must still sign in.

## Considered Options

* Cloudflare Access as the only layer
* A secret held by the deployment, checked by the Worker on every request, with Access optional on
  top of it
* The above, plus validating `Cf-Access-Jwt-Assertion` in the Worker

## Decision Outcome

Chosen option: "a deployment secret checked by the Worker, with Access optional on top", because
the first layer must deploy with the Worker, or a fork on `*.workers.dev` is an open write surface.

* **Layer 1, always.** The `CYRIS_UI_TOKEN` cookie decides whether a request carries this
  deployment's own secret. `/login` sets an HttpOnly cookie holding the token's SHA-256, compared
  in constant time, and anything without it gets the form or a 401 before a byte reaches the
  container. Revocation is rotating the secret.
* **Layer 2, optional.** Access on the hostname named by `CYRIS_UI_ACCESS_HOST` decides *who*:
  email policy, MFA and an audit log. It is a dashboard step on purpose, because automating the
  hostname and the policy would tie the repository to one account. It stays off `workers.dev`,
  where scripts sign in with the cookie alone. On the Access host, `/api/vote` trusts Access
  instead of the cookie, so a reader who already passed Access does not sign in twice.
* **No JWT validation.** A request reaches the Worker on an Access host only after Access allowed
  it. Verifying the JWT again defends only against someone who can already route traffic to the
  origin, which makes it a second copy of layer 1, not a third layer. On `workers.dev` the header
  is not a credential.
* **Preview URLs stay disabled**, because a second public hostname is a second door.

### Consequences

* Good, because a fork on `*.workers.dev` is a complete, protected install with the cookie alone.
* Good, because one rendered HTML gives a `pages.dev` reader everything except voting and a
  signed-in reader everything, with no second rendering and no service token.
* Bad, because behind Access the protected paths answer 302 rather than 401, so a script against
  an Access hostname needs an Access service token; the scripts therefore use the `workers.dev`
  URL.
* Bad, because a `workers.dev` URL that `workers_dev = true` re-enables beside a custom domain is
  a second hostname; the cookie is required there, so it is not an open one.

## More Information

Extracted on 2026-10-08 from these sources at commit `c353d3f`:

* `docs/architecture.md:778-784` (*Auth is one layer always, two if you own a domain*).
* `docs/architecture.md:1174-1186` (the vote-capability paragraph and its 2026-09-01 resolution).
* `workers/app/README.md:36-63` (*Auth*).

The token that the vote buttons once carried is the subject of
[ADR-0008](0008-worker-bearers-split-by-published-versus-secret.md).
