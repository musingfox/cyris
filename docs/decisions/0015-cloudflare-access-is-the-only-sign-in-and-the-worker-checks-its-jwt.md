---
status: accepted
date: 2026-10-17
decision-makers: ""
---

# Cloudflare Access is the only sign-in, and the Worker checks its JWT

## Context and Problem Statement

[ADR-0009](0009-a-token-cookie-always-and-access-only-if-you-own-a-domain.md) made the
`CYRIS_UI_TOKEN` cookie the one layer every deployment has, with Cloudflare Access as an
optional second layer on a hostname the account owns. It gave two reasons the cookie had to be
mandatory:

* Access could protect only a hostname the account owns, so a fork on `*.workers.dev` without the
  cookie would be an open write surface.
* Scripts must sign in: `cyris doctor --deployment`, the `verify` step of
  `.github/workflows/deploy.yml`, and the receipts of `scripts/trial-wizard.sh`.

Both reasons have an answer now:

* Since 2025-10-03 Access can protect a `workers.dev` hostname, and a self-hosted Access
  application can name a `workers.dev` hostname and a path as its destination
  ([changelog](https://developers.cloudflare.com/changelog/post/2025-10-03-one-click-access-for-workers/),
  [Workers and Access](https://developers.cloudflare.com/workers/configuration/cloudflare-access/)).
  A deployment no longer needs a domain of its own to sit behind Access.
* A script can sign in to Access with a service token, sent as the `CF-Access-Client-Id` and
  `CF-Access-Client-Secret` headers, under a policy whose action is Service Auth
  ([service tokens](https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/)).

What brought the question back is the trial. A tester behind Access signs in twice, once with
the Access one-time PIN and once with the token. On 2026-10-17 the owner decided that every
deployment (production, trials and forks) signs readers in with Access only.

## Decision Drivers

* Every deployment has one reader: a self-hosted install, a fork, or a trial with one tester
* A reader signs in once
* A fork on `*.workers.dev` with no domain stays a protected install
* A missing or wrong Access setting must leave the write surface closed, never open
* Scripts keep a way to sign in without a browser
* The public archive stays public, because ADR-0010 keeps every Discord link free of a login

## Considered Options

* Keep ADR-0009: the token cookie always, Access optional
* Access as the only layer, trusting that a request which reached the Worker passed Access
* Access as the only layer, with the Worker validating the Access JWT and failing closed (chosen)

## Decision Outcome

Chosen option: "Access as the only layer, with the Worker validating the Access JWT and failing
closed", because it removes the second sign-in without leaving a deployment whose only guard is a
dashboard setting.

* **Access decides who.** `CYRIS_UI_TOKEN`, the login form and the `cyris_session` cookie go
  away. Each deployment has one self-hosted Access application whose destinations are the paths
  the Worker protects (`/settings`, `/onboarding`, `/labels`, `/interest`, `/login`, `/run`,
  `/api/*`, `/static/*`), on every hostname the Worker answers. `/login` stays as the digest page's sign-in link: Access signs the
  reader in, and the Worker sends them back to the page they came from. When `CYRIS_PRIVATE_ARCHIVE` is `"true"`, the destination is the whole
  hostname instead. The public archive is not an Access destination.
* **The Worker checks the JWT, because the cookie no longer exists.** ADR-0009 rejected JWT
  validation as "a second copy of layer 1". That held only while the cookie was the layer that
  every request had to pass. Without it, a deleted policy, a hostname Access does not cover, or a
  path the application leaves out would hand the request straight to the container. So on every
  protected path, and on the archive when it is private, the Worker verifies
  `Cf-Access-Jwt-Assertion` against the team domain's signing keys, checks that `iss` is the
  team domain and that `aud` holds the application's AUD tag, and refuses anything else before a
  byte reaches the container. Cloudflare's own guidance for Access on Workers says to validate
  this JWT.
* **Fail closed.** A Worker with the team domain or the AUD tag unset refuses every protected
  path, and the private archive, with an error. It never falls through to the container.
* **Scripts use a service token.** `cyris doctor --deployment`, the deploy workflow's `verify`
  step and the trial wizard's receipts send an Access service token. Access answers such a
  request with a JWT that carries the same `aud` and `iss`, with the token's Client ID in
  `common_name` and an empty `sub`
  ([application token](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/application-token/)),
  so the Worker's single check covers scripts and readers alike.
* **Previews sit behind Access too.** `preview_urls` stays `false`, so no Version URL and no
  Preview `workers.dev` URL exists. The Previews that
  [ADR-0021](0021-previews-answer-only-on-a-preview-only-custom-domain.md) puts on a Preview-only
  custom domain `<host>` run this Worker and its JWT check, so the Access application names
  `*.<host>` as a destination beside production's hostnames. Each Preview sets its own
  `CYRIS_ACCESS_TEAM_DOMAIN` and `CYRIS_ACCESS_AUD` as Preview secrets, since a Preview inherits
  none of production's. A Preview given neither fails closed like any other Worker: every
  protected path answers `403`. The owner decided this on 2026-10-23, replacing ADR-0021's
  "No Access on the Preview hostnames", which assumed the token cookie this ADR removes.
* **Out of scope.** `CYRIS_WORKER_TOKEN` and `CYRIS_PROMOTE_TOKEN` are bearers between Workers,
  the subject of [ADR-0008](0008-worker-bearers-split-by-published-versus-secret.md), and do not
  change.

The team domain and the AUD tag identify a deployment and are not secret, so they belong with
`CYRIS_UI_ACCESS_HOST` in `docs/architecture.md` §5; the service token's two values are secrets.
The rows land with the code change.

### Consequences

* Good, because a reader signs in once, with the Access one-time PIN or whatever identity
  provider the policy names.
* Good, because a fork on `*.workers.dev` is protected by Access without owning a domain.
* Good, because a misconfigured Access application can lock the owner out but cannot open the
  write surface.
* Good, because Access brings an email policy, MFA and an audit log to every deployment, not only
  the ones with a domain.
* Bad, because every deploying account must enable Zero Trust and create the Access application
  and a Service Auth policy, a dashboard or API step the deploy button cannot provision. Until it
  exists, `/settings` answers with an error.
* Bad, because a reader who is not signed in now meets an Access redirect on `/api/*` instead of a
  JSON `401`, so the digest page's vote probe has to read a failed or redirected request as "not
  signed in".
* Bad, because scripts carry two secrets, a Client ID and a Client Secret, instead of one, and a
  service token expires on the duration chosen when it was created.
* Bad, because the Worker now depends on fetching and caching the team domain's signing keys.
* Bad, because revoking a session moves from rotating `CYRIS_UI_TOKEN` to revoking it in the
  Zero Trust dashboard, and `docs/operations.md` has to say so.

### Confirmation

`workers/app/test/router.test.js` holds one case per refusal: a protected path without a JWT, with
a JWT whose `aud` or `iss` is wrong, and with a bad signature; the private archive without a JWT;
the same on a `workers.dev` hostname; and a Worker with the team domain or the AUD tag unset.
`grep -rn CYRIS_UI_TOKEN` finds the token only in records of the past: `CHANGELOG.md`,
superseded ADRs, and dated notes under `docs/milestones/` and `docs/spec/audits/`.

## Pros and Cons of the Options

### Keep ADR-0009: the token cookie always, Access optional

* Good, because nothing changes and no deployment needs Zero Trust
* Bad, because a reader behind Access signs in twice
* Bad, because a deployment on `workers.dev` gets no identity, MFA or audit log

### Access as the only layer, trusting that a request which reached the Worker passed Access

* Good, because the Worker has no key fetch and no verification code
* Bad, because the write surface is then guarded by a dashboard setting alone: a deleted policy, a
  typo in a hostname or a path left out of the application opens it with nothing to catch it

## More Information

Supersedes [ADR-0009](0009-a-token-cookie-always-and-access-only-if-you-own-a-domain.md), and
supersedes in part [ADR-0021](0021-previews-answer-only-on-a-preview-only-custom-domain.md): its
bullet "No Access on the Preview hostnames".

The task card is `pm/cyris/tasks/access-replaces-ui-token.md` in the owner's Obsidian vault. It
lists, at commit `3bd8d9c`, every file that reads the token: `workers/app/src/router.js`,
`src/cyris/diagnostics/doctor.py`, `.github/workflows/deploy.yml`, `scripts/trial-wizard.sh`, and
their tests and docs. The decision was accepted before that change, which the card tracks.

[ADR-0010](0010-the-digest-stays-static-behind-the-app-worker.md) is why the public archive is not
an Access destination.

A hosted, multi-tenant cyris would need roles that one Access application per deployment does not
give. It is out of scope here and gets its own ADR.
