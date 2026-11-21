---
status: accepted
date: 2026-11-21
decision-makers: ""
---

# The hosted operator routes admit only the identities a list names

## Context and Problem Statement

The hosted Worker's operator routes, `/hosted/...`, are behind a Cloudflare Access application.
The Worker checks the Access JWT's signature, issuer and audience
([ADR-0015](0015-cloudflare-access-is-the-only-sign-in-and-the-worker-checks-its-jwt.md)), and
nothing else. So any identity the application admits holds every operator route. Those routes
include `register`, `seed`, `run`, `invite` and `query`, which runs any SQL on any tenant's
database ([ADR-0036](0036-the-operator-reaches-a-tenant-through-an-access-checked-query-route.md)).

The security review of 2026-11-19 (finding H1) found this. Today the application admits the
operator's service token alone, so nothing can be exploited yet. One mistaken policy edit would
change that, such as a reader's email added so they can see their digest, or a group added for a
tester. The reader's JWT would then be the operator's in all but name. Readers sign in with a
mailed link now ([ADR-0033](0033-hosted-readers-sign-in-with-a-mailed-link.md)), so no reader
needs to be in Access. That makes such an edit a mistake, but not an impossible one.

Who the Worker admits on an operator route, and where that list lives, was undecided.

## Decision Drivers

* A valid Access JWT must not be enough. The Worker should decide who the operator is, and the
  tests should show it.
* A Worker that names no operator must open no operator route. It must not fall back to "anyone
  Access admits".
* The service token that `scripts/hosted.sh` and `cyris --tenant` use must keep working, and so
  must an operator who signs in to Access with a browser.
* No operator email in the public repository, so not in `wrangler.hosted.toml`.
* The self-hosted Worker shares the Access check (`accessRefusal` in `workers/app/src/router.js`)
  and must not change.

## Considered Options

* A list in a Worker setting, `CYRIS_HOSTED_OPERATORS`, of emails and service token Client IDs
* Access policy alone, with the runbook telling the operator not to widen it
* One named service token only, as ADR-0036 considered for `query`
* A list in the registry Durable Object, managed through an operator route

## Decision Outcome

Chosen option: "a list in a Worker setting", because it puts the check in the Worker, where a test
can hold it. It needs no new route and no new credential, and it refuses everything when unset.

How it is built:

* `CYRIS_HOSTED_OPERATORS` (grade B, `cyris-hosted` only) is a comma-separated list. An entry with
  an `@` is an Access email, matched in any case against the JWT's `email` claim. Any other entry
  is a service token's Client ID, matched exactly against the JWT's `common_name` claim, which is
  where Access puts a service token's Client ID. Because the two kinds are kept apart, an email
  entry never admits a service token, and a Client ID entry never admits a user.
* `workers/app/src/hosted_operators.js` parses the list. `hosted_router.js` refuses every operator
  request with 403 when the list is empty or unset, before any JWT is read. Then it runs
  `accessRefusal` with an `admit` callback. The callback gets the verified token's claims and
  refuses, with 403, an identity the list does not name. Both refusals happen before any tenant is
  looked up.
* `accessRefusal` takes `admit` as an optional fourth argument, defaulting to "admit anyone". So
  the self-hosted Worker's check is unchanged.
* `scripts/hosted.sh secrets` asks for the list and sets it with `wrangler secret put`, as it does
  the hosted domain and the mail sender. It is not secret. It is a secret only so that the
  committed config names no one.
* `docs/hosted-tenants.md` keeps its rule that the Access application admits no identity but the
  operator's. The list is a second line of defence, not a reason to widen the first.

### Consequences

* Good, because a reader or tester added to the Access application by mistake is refused on every
  operator route.
* Good, because `query`'s any SQL now needs an identity the operator named, not just any sign-in
  Access allows.
* Good, because an unset list fails closed, and the 403 names the setting.
* Bad, because a deployment must set one more value. An existing trial deployment refuses the
  operator until `scripts/hosted.sh secrets` is run again with the list.
* Bad, because rotating the service token means updating the list too. A new token's Client ID is
  refused until it is added.
* Neutral, because the list does not replace Access. A request still needs a valid JWT from the
  configured team and audience.

### Confirmation

`workers/app/test/hosted.test.js`, under "the hosted Worker's operator routes", has the cases:

* A valid JWT whose email or `common_name` is off the list is refused 403 on every route kind, and
  no dependency is called.
* An email given as a `common_name`, and a Client ID given as an email, are both refused.
* The listed token and email are admitted, the email in any case.
* An unset, empty or blank list refuses every route.

`tests/test_hosted_script.py` holds that `secrets` sets the list on stdin.

## Pros and Cons of the Options

### A list in a Worker setting

* Good, because the check is the Worker's own, and the tests show it.
* Good, because it covers both the service token and a browser sign-in.
* Bad, because the list and the Access policy are two places to keep in step.

### Access policy alone

* Good, because there is nothing to build, and there is one place to edit.
* Bad, because one mistaken policy edit hands over every tenant. H1 is exactly this.

### One named service token only

* Good, because it is the smallest list.
* Bad, because the operator could no longer use a browser at all, not even for `page` or `report`.
* Neutral, because it is the list with one entry. The chosen option allows it.

### A list in the registry

* Good, because the list could change without a redeploy or a secret edit.
* Bad, because changing it would need an operator route. That route would be guarded by the list
  it edits, and a mistaken entry could lock the operator out or let someone else in.
* Bad, because the registry would hold deployment identity, which is grade B and belongs in the
  Worker's settings (`docs/architecture.md` §5).

## More Information

* Cloudflare documents the claim: the application token's `common_name` is "The Client ID of the
  service token (`CF-Access-Client-Id`)"
  (https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/application-token/).
  The header's Client ID ends in `.access`, so the entry is pasted whole and matched exactly. Not
  verified here: that the live JWT carries it so. The trial's `scripts/hosted.sh check` is the
  receipt: if the claim were named otherwise, every call with the token would be refused 403.
* A Client ID entry matches `common_name` whatever authenticated the request. Cloudflare's
  identity endpoint also puts a client certificate's common name in `common_name`, so a
  certificate whose CN equals a listed Client ID would pass `admitOperator`, if an mTLS rule were
  ever added to the application. Not verified: whether the application JWT carries `common_name`
  for an mTLS identity. This is accepted, not excluded in code: the application's policy admits
  the operator alone, and the runbook names no mTLS rule
  (`docs/hosted-tenants.md`, step 2), and a certificate carrying that CN has to be issued by a CA
  the application trusts, which only an operator-added mTLS rule creates. The same rule that keeps
  other identities out of the application keeps this out. If an mTLS rule is ever added, the entry
  should also require the service-token shape, which the same document gives as `sub` being
  empty, and that is a change to `hosted_operators.js` with its own test.
* Still the operator's to do in the dashboard: set the Access application's cookie to `HttpOnly`
  and `SameSite=Lax`, and keep its policy to the operator alone (`docs/hosted-tenants.md`, step 2).
