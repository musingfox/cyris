---
status: accepted
date: 2026-11-20
decision-makers: ""
---

# A hosted reader signs in with a mailed single-use link, and Access guards only the operator

## Context and Problem Statement

[ADR-0015](0015-cloudflare-access-is-the-only-sign-in-and-the-worker-checks-its-jwt.md) made
Cloudflare Access the only sign-in, for deployments that each have one reader. It left a hosted,
multi-tenant cyris to an ADR of its own, because one Access application per deployment gives no
roles. This is that ADR.

The hosted Worker `cyris-hosted` ([ADR-0028](0028-one-sqlite-durable-object-per-hosted-tenant.md))
serves each tenant's pages at `/t/<tenant>/`
([ADR-0029](0029-a-hosted-tenants-pages-live-in-its-durable-object.md)) and the operator's routes
at `/hosted/...`. Until now both sat behind one check: any valid Access JWT. Access admitted the
operator alone, so the operator was every site's only reader. The closed beta needs readers who
each read their own tenant's digest and nothing else.

Four facts shaped the answer:

* The owner's card for this change: readers are not Access users, because Access charges each
  user over 50 seven US dollars a month. Readers sign in with a mailed link, sent through the
  Cloudflare Email Service the self-hosted mail already uses. Only addresses on the operator's
  invite list may sign in; there is no self sign-up. The invite list and sessions live in the
  registry ([ADR-0030](0030-the-operator-registry-is-one-sqlite-durable-object.md)).
* The security review of 2026-11-19 (finding H1) found that the Worker checks only that an Access
  JWT is valid, not who it names or which tenant it may read. It asked that the Worker itself
  match a session's tenant to the path's tenant, that a tenant session never pass `/hosted/`,
  and that `/hosted/` stay behind Access when Access is taken off `/t/`.
* The same review (finding H2) found that every tenant's pages and the operator's routes share
  one origin, and that a page body is served as the tenant's database holds it. A script running
  on any tenant page could act as whoever opened it, the operator included, and read Access's
  `CF_Authorization` cookie, which is not `HttpOnly` by default. It asked that the origin model be
  decided before a session cookie goes live: one origin per tenant, or one origin with a strict
  Content Security Policy.
* Cloudflare Email Service, read on 2026-11-20: until a sending domain is onboarded, mail goes
  only to verified destination addresses; once one is, "you can send to any recipient
  immediately". Sending to arbitrary recipients needs Workers Paid, which the trial account has
  for its containers. The domain must use Cloudflare DNS; onboarding adds SPF, DKIM, DMARC and
  bounce MX records. Workers Paid includes 3,000 outbound mails a month and charges US$0.35 per
  1,000 beyond. A new account starts with a conservative daily quota. A Worker sends through a
  `send_email` binding, with no API token
  ([limits](https://developers.cloudflare.com/email-service/platform/limits/),
  [pricing](https://developers.cloudflare.com/email-service/platform/pricing/),
  [Workers API](https://developers.cloudflare.com/email-service/api/send-emails/workers-api/),
  [get started](https://developers.cloudflare.com/email-service/get-started/send-emails/)).

## Decision Drivers

* A reader of one tenant reads that tenant only. The Worker enforces it on every request, and a
  test shows it.
* A refusal tells a reader nothing about another tenant, not even whether it exists.
* The operator's routes stay behind Access, and no tenant credential opens them.
* Readers cost nothing per seat.
* Only the operator decides who may sign in, and to which tenant.
* No token is stored, logged, or sent in a `Referer`. Every sign-in token is single-use and
  short-lived, and a session can be ended at once.
* A script on a tenant page cannot reach a session, another tenant or the operator.
* The self-hosted deployment keeps ADR-0015 unchanged.
* No new external service.

## Considered Options

How a reader signs in:

* Access for readers too: an Allow policy per reader, and the Worker maps the JWT's email to a
  tenant
* A mailed single-use link that opens a session the Worker keeps in the registry
* A password per reader

Where a tenant's pages live (the origin model):

* One origin per tenant: a subdomain each, and another for the operator
* One origin, with every tenant page sandboxed by its Content Security Policy

## Decision Outcome

Chosen sign-in: "a mailed single-use link that opens a session the Worker keeps in the
registry". The owner chose a mailed link in the card. Of the three options, it is the only one
with no per-seat cost and no password to store. Chosen origin model: "one origin, with every
tenant page sandboxed by its Content Security Policy". It needs no domain and no change to the
run's links, and it removes the page's origin outright, which a subdomain per tenant would not.

How it works:

* **Two doors, two credentials.** `handleHostedRequest` (`workers/app/src/hosted_router.js`)
  sends `/t/<tenant>/...` to the session check and `/login`, `/login/confirm` and `/logout` to the
  sign-in pages. Neither reads an Access JWT. Every other path, `/hosted/...` above all, takes the
  Access JWT alone, as ADR-0015 checks it, and reads no cookie. So no tenant session passes
  `/hosted/`, whatever Access covers. The Access check is not made "a JWT or a session" for every
  route.
* **The invite list.** Only the operator adds an address, with
  `POST /hosted/tenants/<tenant>/invite`, and takes one off, with `.../revoke`. Both are
  Access-checked and take the address in a JSON body, so it stays out of URLs and logs. Both
  also refuse a request a browser sent from another site, by the sign-in's own check (below): the
  Access cookie is `SameSite=None` by default, so without it a page on any site could post a
  `text/plain` body with the operator's sign-in and put its own address on a tenant's list. curl
  sends no `Origin` and no `Sec-Fetch-Site`, so `scripts/hosted.sh` passes. `GET
  .../members` lists a tenant's addresses. An address belongs to one tenant: `members.email` is
  the key. The registry alone answers which tenant a sign-in opens, so no request names one.
* **The link.** `POST /login` takes an address. If the registry's list holds it, the Worker
  records a link and mails it. The registry is asked after the answer is sent (`waitUntil`), so
  the page and its timing are the same for an address on the list and one off it. A reader's own
  asks are capped, so asking again and again can neither fill an inbox nor spend the operator's
  mail quota: one address gets at most one link a minute and three in any hour, one tenant's
  addresses together at most ten in any hour, and every tenant's together at most 500 in any 24
  hours, a cost backstop that no one tenant's ten an hour can reach (`SIGN_IN_ADDRESS_CAP`,
  `SIGN_IN_TENANT_CAP`, `SIGN_IN_DAILY_CEILING`). The registry counts the asks in
  `sign_in_asks`, a row per link a reader's ask was issued, kept a day. Past a cap no link is
  mailed and the answer is still the one an address off the list gets, so the cap tells the asker
  nothing. An address off the list writes nothing to the registry, so a flood of strangers adds
  no row. The operator's invitation is the first link, mailed by the invite route. It is held by
  no cap and by no gap, and counts against none, so the operator can always invite, or re-invite
  a reader whom a stranger's asks hold, with no revoke, and a cohort invited at once takes none of
  its tenant's hour. A link is 32 random bytes. The registry keeps only its SHA-256
  (`invites.code`), bound to the address and the tenant on the list. The link expires after 15
  minutes and is spent the first time it is used.
* **The token rides in the fragment.** The link is `https://<hosted domain>/login/confirm#<token>`.
  A browser never sends a fragment, so the token is in no request URL, no Workers Logs
  invocation record and no `Referer`. The confirm page's one script moves it into a form and drops
  it from the address bar. The reader presses Sign in, which POSTs it. A mail scanner that opens
  the link therefore spends nothing.
* **The session.** Spending a link opens a session for the link's own address and tenant. Its
  token is another 32 random bytes; the registry keeps its SHA-256 (`sessions.token_hash`). The
  reader is sent with a 303 to `/t/<tenant>/`, a URL with no token in it. The cookie is
  `__Host-reader_session`, with `Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/` and a
  `Max-Age` of 14 days. The session is not extended by use. Each `/t/` request reads the session
  from the registry, never from a cache. The read also requires the address to still be on its
  tenant's list. So `POST /logout`, the operator's revoke and expiry each end it at its next
  request.
* **The tenant check.** For `/t/<tenant>/...`, a request with no live session is sent to
  `/login`, before any tenant is looked up. A session whose tenant is not the path's gets one fixed
  404, `{"error":"not found"}`. That is the same answer for a tenant that exists and one that does
  not, and it is decided before the path's tenant is read.
* **Lax, not Strict.** A reader opens the digest from a mail, and every tenant page is sandboxed,
  so each click from one page to the next comes from an opaque origin. A `Strict` cookie is sent on
  neither, so the reader would be asked to sign in again on every click. `Lax` still withholds the
  cookie from cross-site POSTs. The sign-in's own POSTs also refuse an `Origin` that is not the
  Worker's, or a `Sec-Fetch-Site` of `cross-site` or `same-site`, as the local server does.
* **The origin model.** Every response under `/t/`, and the operator's copy of a page, carries
  `Content-Security-Policy: sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox`,
  then `default-src 'none'`, scripts inline only, styles inline and from Google Fonts, fonts from
  Google Fonts, images from any `https:` and `data:`, `connect-src 'none'`,
  `form-action 'none'`, `base-uri 'none'` and `frame-ancestors 'none'` (`SITE_POLICY` in
  `workers/app/src/hosted_pages.js`). The sandbox gives the page an opaque origin. Its own
  scripts, the layered view among them, still run, but as no origin at all. They cannot read
  `document.cookie` or storage. A request they make is cross-site: it carries no `Lax` cookie,
  and its answer is unreadable without CORS, which the Worker never sends. So a script injected
  into one tenant's page cannot reach that reader's session, another tenant's pages, or
  `/hosted/` as the operator. The Worker serves a stored page only as `text/html` or
  `image/svg+xml`, the two types the run writes, never a type the row names. Every hosted
  response there also carries `X-Content-Type-Options: nosniff`, `X-Robots-Tag: noindex,
  nofollow` and `Referrer-Policy: no-referrer`. The sign-in pages are served `no-store`, with a
  policy that allows their one script by its hash.
* **The operator reads a page under `/hosted/`.** `GET /hosted/tenants/<tenant>/page` answers
  the page `/t/<tenant>/` serves, behind Access and sandboxed the same way. The runbook's check
  reads it with the service token. The operator holds no tenant session.
* **Mail.** The hosted Worker sends through a `send_email` binding, `MAIL`, from the address
  `CYRIS_HOSTED_MAIL_FROM` (grade B), on a domain the operator onboards to Email Sending. The
  link's host is `CYRIS_STORE_HOSTED_DOMAIN`, never the request's `Host`. A Worker that lacks
  any of the three mails nothing and says so to the operator. A failed send is logged by the
  binding's error code alone, never the token or the address.
* **Tenant ids.** A new id must also start and end with a letter or a digit, and may take no
  word of `workers/app/src/hosted_reserved_ids.json`. Those are the hosted Worker's own paths, the
  default Worker's paths that a page links to absolutely, `cdn-cgi`, and host labels an operator
  would keep. So a later route or a later per-tenant host cannot collide with a tenant.

Relation to ADR-0015. This supersedes ADR-0015 in part, for `cyris-hosted` only. Its readers do
not sign in with Access, and Access no longer decides who reads a tenant's pages. ADR-0015 still
holds for the hosted operator: the operator's routes take an Access JWT, checked in the Worker
against the team domain's keys, issuer and audience, and refused closed when a setting is unset.
The Access application's destination narrows from the whole hostname to `/hosted/*`. For every
self-hosted deployment, production, trials and forks, ADR-0015 is unchanged: `wrangler.toml`,
`index.js` and `router.js` do not change, and `index.js` bundles none of the hosted modules.

### Consequences

* Good, because a reader costs nothing per seat. The mail is within Workers Paid's included
  3,000 a month until the beta is large, and the caps on asking hold the worst case to 500 mails
  a day.
* Bad, because the caps count asks by address and by tenant, not by who asks, so a stranger's
  asks hold back the reader they name. Anyone who knows one reader's address holds that reader
  with three asks an hour, and anyone who knows four addresses of one tenant holds that tenant's
  readers with ten; each hold lasts until an hour after the asking stops. A flood of one tenant
  holds no other, since its ten an hour make at most 240 a day, under the 500-a-day ceiling. A
  flood of three tenants holds every tenant: about 12 known addresses, four for each, are issued
  30 links an hour and reach the ceiling in about 17 hours, and from then on no reader of any
  tenant is mailed a link while the flood goes on, and until its asks are a day old. The
  operator's re-invite mails a reader a link at once all the same. The Worker logs which cap was
  reached, so the operator can tell a flood from a lost mail. Only a limit per client would tell
  the stranger from the reader; see below.
* Bad, because there is no limit per client on `POST /login`. Cloudflare's Workers Rate Limiting
  binding was considered and not adopted, because it cannot stop this hold. It counts per
  Cloudflare location, not across the network, only over a period of 10 or 60 seconds, and its
  documentation discourages keying it by IP address
  ([Rate Limiting](https://developers.cloudflare.com/workers/runtime-apis/bindings/rate-limit/)).
  A hold needs three asks an hour, far under the lowest limit a 60-second period can set, and a
  stranger asking from several locations is counted apart in each. An address off the list writes
  nothing however often it asks, so the cost of a flood of strangers is the registry's requests,
  not its rows or the mail.
* Good, because the tenant a reader may read is decided by the Worker against the registry on
  every request, and is no longer whatever the Access policy admits.
* Good, because no token is stored, logged or sent in a `Referer`, and a session ends at its next
  request after a sign-out, a revoke or its expiry.
* Good, because a script on a tenant page runs without its origin, so H2's way from one tenant's
  page to the operator or another tenant is closed without a domain.
* Good, because the self-hosted sign-in and its tests are untouched.
* Bad, because the operator must onboard a sending domain on Cloudflare DNS to the trial account
  before a link reaches anyone not verified there. Until then the sign-in mails only verified
  addresses. A new account's daily quota is not published.
* Bad, because the Access application must be narrowed to `/hosted/*` by hand. Until then Access
  stops readers at its own login. If it is narrowed wrongly, `/hosted/` still refuses any request
  without a valid JWT, but the operator is then locked out rather than let in.
* Bad, because a page's scripts run in an opaque origin. The vote buttons' probe fails, as it
  already does on hosted pages, which have no `/api/vote`. A vote from the page cannot carry the
  session either. The card that brings votes to hosted pages must revisit this model, by moving the
  per-issue values out of the inline scripts so they can be allowed by hash, or by giving each
  tenant an origin.
* Bad, because `script-src 'unsafe-inline'` lets an injected script read the page it is on and
  send it out through an image URL or a navigation. It reaches nothing else. Allowing the inline
  scripts by hash was considered and set aside: the vote script embeds the issue's date and the
  page's language, so no fixed list of hashes covers the pages, and a hash or a nonce that the run
  container chose would trust the very writer H2 distrusts.
* Bad, because the sign-in pages and mail are worded in English only. The tenant's language is in
  its own database, which no one signed out may read.
* Bad, because the invite list holds one tenant per address. A person in two tenants needs two
  addresses.
* Neutral, because Workers Logs keeps an invocation record of each request. A token is never in
  a URL, but whether that record keeps the request's `Cookie` header is not stated in the
  documentation read on 2026-11-20. The runbook has the operator check one record.

### Confirmation

* `workers/app/test/tenant_registry.test.js` ("the registry's sign-in") checks five things. An
  address goes on one tenant's list only. A link is issued only to a listed address, for its
  tenant, keyed by a hash, at most once a minute and within the caps per address, per tenant
  and per day, while the operator's invitation is held by none, and an address off the list
  writes nothing. A link opens one session once and none when
  expired or unknown. A session past its time is refused. A sign-out or a revoke ends a session at
  once. It also checks the reserved and hyphen-edged ids.
* `workers/app/test/hosted_login.test.js` drives the router over the real registry and checks
  nine things:
  * a listed address is mailed a link and an unlisted one is not, with the same answer, and past
    any cap a listed address gets that same answer and no mail; a reader a stranger asked for
    five times signs in again within the hour and is re-invited at once, and one tenant's flood
    holds no other tenant's readers and no invitation;
  * the link opens the reader's own latest digest;
  * A's session is refused on every path of B, registered or not, with one answer;
  * an expired session, a reused link and an expired link are each refused;
  * a tenant session never passes `/hosted/`, and `/hosted/` refuses a request without an
    Access JWT even when it carries one;
  * an invite or a revoke posted from another site with the operator's Access JWT is refused,
    and nothing is written or mailed;
  * an Access JWT opens no `/t/` page;
  * the cookie's attributes are set, the token is in no URL and no log line, and the registry
    keeps no token;
  * the site's policy and headers are set, and only HTML or SVG is served.
* `tests/test_hosted_config.py` checks that the self-hosted Worker bundles no sign-in module and
  that `wrangler.toml` has no `send_email` binding.
* The human's check on the trial account is in `docs/hosted-tenants.md`: a link mailed to an
  unverified external inbox arrives, and opening it shows that tenant's latest digest.

## Pros and Cons of the Options

### Access for readers too

* Good, because Access brings MFA, identity providers and an audit log, and the Worker already
  checks its JWT.
* Bad, because each reader is an Access user, at a cost per seat past 50 (the card's figure).
* Bad, because the Worker would still have to map each JWT's email to a tenant. A reader added to
  the policy and not to the map would hold an operator-shaped JWT, which is H1.
* Bad, because the reader's `CF_Authorization` cookie would sit on the tenant pages' origin, not
  `HttpOnly` by default.

### A mailed single-use link and a Worker session

* Good, because it has no per-seat cost and no password, and the registry already has the tables.
* Good, because the Worker owns the whole check, so the tests can show it.
* Bad, because it depends on mail delivery and an onboarded sending domain.
* Bad, because a session lives in the registry, one object that serialises every `/t/` request's
  read. ADR-0030 named login traffic as the load to watch.

### A password per reader

* Good, because it needs no mail after the invite.
* Bad, because the Worker would store password hashes, need a reset flow (which is a mailed link
  again), and face guessing.

### One origin per tenant

* Good, because each tenant's pages are their own origin, so a page's scripts could use storage
  and same-origin requests, which votes will want.
* Bad, because `workers.dev` gives the Worker one hostname. A host per tenant needs a zone, a
  wildcard route and a certificate for it, and the operator's routes need yet another host.
* Bad, because the run's links (`CYRIS_STORE_HOSTED_DOMAIN`, ADR-0029) would change shape, on
  both sides.
* Bad, because a script on a tenant's own origin could still read that tenant's pages and act
  with its session. The sandbox gives a page no origin at all.

### One origin, every tenant page sandboxed

* Good, because it needs no domain and changes no link.
* Good, because a page has no origin to abuse, whatever its body holds.
* Bad, because a page's scripts can use no storage and make no same-origin request.

## More Information

* `docs/architecture.md` §4 has the registry's tables, §5 grades the sender, the lifetimes, the
  caps on asking and the reserved ids, and §6 describes the two doors.
* `docs/hosted-tenants.md` has the operator's steps: onboard the sending domain, set the sender,
  narrow the Access application to `/hosted/*`, invite a reader, and check delivery to an
  unverified inbox.
* Unverified here: whether a browser runs every script of a sandboxed digest page as it does an
  unsandboxed one, which needs a browser; the Access dashboard's labels for a path-scoped
  destination on `workers.dev`; and whether Workers Logs records request headers.
* Left to other cards: the operator's own identity check (H1's operator allow-list), CSRF
  protection on the operator's other POST routes, `register`, `seed`, `run` and `poll` (M1; the
  invite list's two routes have it here), and the tenant's language on the sign-in pages.
