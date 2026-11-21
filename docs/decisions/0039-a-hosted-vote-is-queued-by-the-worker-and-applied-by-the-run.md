---
status: accepted
date: 2026-11-21
decision-makers: ""
---

# A hosted vote is queued by the Worker from a page token, and applied by the tenant's run

## Context and Problem Statement

A self-hosted reader's vote goes from the page to the app Worker's `/api/vote`, on to the shared
promote Worker's KV queue, and the next run pulls it with `promote-sync`
(`src/cyris/adapters/promotions.py`). That queue is one deployment's, so a hosted run does not
read it ([ADR-0028](0028-one-sqlite-durable-object-per-hosted-tenant.md)), and until now a hosted
page had no vote at all: the page's probe of `/api/vote` fails, so its buttons stay hidden
([ADR-0033](0033-hosted-readers-sign-in-with-a-mailed-link.md), Consequences).

The card is to make a hosted vote reach the tenant's own database, and the tenant's next run use
it. The security review of 2026-11-19 (section 3) sets the terms: the endpoint takes the tenant
from the session and never from the body or `Referer`; the voted URL must be one of the tenant's
stored articles; it has CSRF protection; and it does not go through the shared promote Worker's
queue. Two choices were open: where the write rule of `promotions.py` lives (rewritten in JS in
the Worker, or left to the container), and how a request from a page gets a session at all.

The second is the hard one. ADR-0033 serves every tenant page sandboxed, with an opaque origin and
`connect-src 'none'`. A script on such a page cannot read the session cookie, and a request it
makes comes from `Origin: null`, is cross-site, and so carries no `SameSite=Lax` cookie, and its
answer is unreadable without CORS. The session cookie cannot authenticate a vote from the page.
ADR-0033 named this and left it to this card: allow the inline scripts by hash, or give each
tenant an origin.

## Decision Drivers

* The tenant must come from a live session the registry holds, checked at every vote, so a revoke,
  a sign-out or an expiry ends the right to vote at the next one.
* The sandbox, the one-origin model and `connect-src 'none'` are what keep a script injected into
  a tenant page from reaching a session, another tenant or the operator (H2). A vote must not
  reopen them.
* One writer of what a vote means. Only a human's action may stamp `triaged_at`
  (`CLAUDE.md`, Conventions), and the topic vote and its one-click window are subtle
  (`topic_votes`, `unrecorded`). Two implementations drift.
* No new secret for the operator to provision, and no change to `wrangler.hosted.toml`.

## Considered Options

* The Worker queues the vote in the tenant's object, a page-borne token authenticates it, and the
  run applies it with the existing Python rule
* The same token, with the write rule rewritten in the Worker (stamp `triaged_at`, write the topic
  vote)
* The session cookie, with `allow-same-origin` added to the sandbox for a signed-in reader's pages
* One origin per tenant, with the cookie

## Decision Outcome

Chosen option: "the Worker queues the vote in the tenant's object, a page-borne token
authenticates it, and the run applies it with the existing Python rule".

**The write rule stays in the container.** `POST /api/vote` appends `{url, vote, ts}` to the
tenant's own `hosted_votes` table (`workers/app/src/tenant_votes.js`, `adapters/store/hosted_schema.sql`).
The tenant's next non-dry, non-rerun run drains it at the point `promote-sync` runs for a
self-hosted one (`sync_hosted_votes`, `adapters/hosted_votes.py`), and hands the rows to
`apply_promotions`, which `sync_promotions` was split to share. So `triaged_at`, the state, the
topic vote and the one-click window have one writer, in Python, and a hosted vote is read by vote
similarity exactly as a synced one. The cost is that a vote takes effect at the next run, not at
once; the self-hosted queue is the same, and the card asks only that the next run use it. A
rewrite in JS would be a second copy of `topic_votes` and `unrecorded`, with the topic table's
shape and the 60-second window duplicated across two languages, to buy an effect nobody reads
before the next run. The Worker writes nothing but the queue row, so the only path that stamps a
human decision is a row a signed-in reader's request put there.

**A vote token, not the cookie.** The Worker puts a vote token in a `<meta name="cyris-vote-token">`
of every HTML page it serves to a live session (`serveTenantPage`, `hosted_pages.js`), and the
page sends it in an `X-Cyris-Vote` header, with its probe and its votes. The token is the
session's hash, the second the page view made it at, and an HMAC of both under a key the registry
makes on first use and never reads out (`voteToken`, `voteSession`, `tenant_registry.js`). It opens
no page and no operator route, it ends 12 hours after the page view that made it
(`VOTE_TOKEN_TTL_MS`, `hosted_session.js`), and the registry still looks the session up, and its
address and tenant up, at every vote, so revoking, signing out and expiry end it at the next vote. The tenant is that session's
`tenant_id`: the body, the query string, `Referer` and any header but this one are never read for
it. The route reads no cookie at all.

**What the sandbox gives up, and no more.** Pages served with a token carry `connect-src` for
exactly `https://<host>/api/vote` in place of `'none'` (`votePolicy`); every other response, and
the operator's copy of a page, keeps `'none'`. The sandbox itself, the opaque origin and the one
origin are unchanged. The route answers with `Access-Control-Allow-Origin: *`, because the page is
`Origin: null` and the credential is a header the page holds, not anything the browser sends of
its own accord. A script injected into a tenant page could already read the page; it can now also
cast votes for that page's own tenant, as that reader. It cannot read the session, vote for another
tenant, or reach `/hosted/`. A vote is cheap to reverse, so this is the one thing taken from H2's
margin.

**A stolen token votes for hours, not for the session's 14 days.** The token sits in the page, so
an injected script can read it, and it can send it away: the policy keeps `img-src https:`, which
a digest needs for the feeds' own images, and the sandbox keeps `allow-popups` and top-level
navigation, which the article links need, and a URL carrying the token is enough to leak it. So
the channel out is not closed; tightening `img-src` on token-bearing pages would break every
digest's images and still leave a popup or a navigation. The window is shortened instead. Whoever
holds the token can vote from any client with no `Origin`, as the route allows (a script, curl),
after the page is closed, but only until 12 hours after the page view that made it, and only
while the session is live. Each page view mints a fresh token, so a reader who keeps reading is
never cut off; a page left open past the lifetime gets the signed-out answer, whose message asks
for a reload, and the reload carries a new token. The issue second is under the MAC, so a holder
cannot move it later, and a token dated after the registry's clock is refused. Twelve hours
covers an issue read on the day it arrives: the default schedule runs twice a day. The bound is
on each token, not on the script: a script injected into a page runs again at every page view and
takes that view's fresh token, so the window ends 12 hours after the page's last view, not its
first.

**CSRF.** The credential is a header a form cannot set, and the route ignores the cookie, so a
form or a fetch from another site, which a browser sends with the reader's cookie, votes nothing.
A browser that names another site (`Origin` not the Worker's and not `null`, or a
`Sec-Fetch-Site` of `cross-site` or `same-site` with no `Origin`) is refused 403 before any
session is read, by the sign-in's own `crossSite` helper (`hosted_session.js`); the literal
`null`, which a sandboxed page sends, is the one cross-site caller let through, and the token is
what admits it. A body is read only as far as 4 KiB, before the token is checked, so a request
with any token buffers no more than that: a declared `Content-Length` past it is refused unread,
and an undeclared body is cancelled once it passes. It takes `up` or `down` and a URL under 2 KiB.

**The article must be the tenant's.** The vote lands through `TenantDb.recordVote`, which looks
the URL up in the tenant's own `stored_articles` and refuses a miss with 404, so a vote can name
no article another tenant holds. A queue past 2000 rows refuses with 429 until the next run
drains it: a bound on what a leaked token can write, far over what a reader votes in a day.

**Self-hosted is untouched.** `workers/app`'s default Worker, the promote Worker, the KV queue and
`promote-sync` read and write what they did. `bootstrap.build_promotion_sync` returns
`sync_hosted_votes` only for a hosted tenant with a database; with none (the `promote-sync`
command) it still returns None. The page script sends the header only when the `<meta>` is
present, so a page on Pages or the local server sends what it sent. The probe learns one new
answer, `settings: false`, which closes the Settings link and the gated controls: the hosted
Worker has neither a Settings page nor an interest page, so the buttons open and nothing else.

### Consequences

* Good, because a hosted vote reaches the tenant's own database through the session the registry
  holds, and `triaged_at` and topic votes are written by the same Python as a self-hosted vote.
* Good, because the sandbox and one origin hold: a page still has no origin, reads no cookie, and
  may reach one URL.
* Good, because no secret is added: the HMAC key lives in the registry's own storage.
* Good, because the self-hosted KV path, `promote-sync` and its tests are unchanged.
* Bad, because a vote is effective at the next run, not at once, and the page cannot show it after
  a reload: an opaque origin has no `localStorage`, so a reader sees their votes painted only in
  the page they cast them on. The run's verdicts reach the next issue.
* Bad, because a script injected into a tenant page can cast votes for that tenant as that
  reader, and can send the token out through an image or a popup, after which its holder votes
  from anywhere until 12 hours after the page's last view (each view hands the script a fresh
  token), up to the queue's 2000 rows a run. It is
  narrower than the cookie it replaces, it reaches only that tenant's stored articles, and the
  registry ends it with the session.
* Bad, because a page left open more than 12 hours shows the signed-out message at its next vote
  although the session is live; a reload mends it.
* Bad, because every page view of a signed-in reader makes one more registry call, for the token,
  and the route's CORS answer is open to any origin. The credential, not the origin, is what is
  checked.
* Neutral, because the tenant's own run container can write `hosted_votes` through its gateway,
  as it can write any table of its own database. That reaches nothing another tenant holds.

### Confirmation

* `workers/app/test/hosted_vote.test.js` drives `hosted.js` itself over the real registry and
  tenant objects. A signed-in page carries a token and a policy that names only the vote route; the
  operator's copy carries neither. A vote queues in the session's tenant with the Worker's clock.
  Alpha's token, with every way of naming bravo (body, query, `Referer`, a header), queues only in
  alpha, and a vote for bravo's article is a 404 that writes nothing. A token with another
  session's hash or a wrong MAC, a cookie with no token, a request from another site and a request
  with `Sec-Fetch-Site: cross-site` and no `Origin` each vote nothing. Sign-out, revoke and expiry
  end a token. A token is refused `VOTE_TOKEN_TTL_MS` after the page view that made it while its
  session lives on, the next page view's token votes, and a token whose issue time is rewritten or
  lies ahead of the clock is refused. A URL the tenant has not stored, a direction but up and down, a body over 4 KiB
  (read no further than its bound, and not at all when its declared length is over) and a full
  queue are refused. No request reaches the promote Worker's URL.
* `tests/test_hosted_votes.py` drains a tenant's queue: a vote stamps its article and no other,
  three posts of one topic click are one topic vote, a change of mind ends on the later vote, a
  vote queued during the write stays queued, and a failed write leaves every vote queued.
* `tests/test_hosted_run_wiring.py` holds the wiring: a hosted run's `sync_promotions` is the
  hosted drain, and `build_promotion_sync` without a database is still None. The existing
  `tests/test_sync_promotions.py` passes unchanged over the split `apply_promotions`.
* `tests/test_hosted_config.py` holds the page's route, meta name, header and ended-session code
  to the Worker's. `tests/test_static_signed_out.py` holds the script's one 401 branch to that
  code.

## Pros and Cons of the Options

### The Worker queues, a page token authenticates, the run applies

* Good, because one Python rule writes every vote, and the sandbox stays.
* Good, because the token is revocable with the session and needs no secret.
* Bad, because the page holds a credential an injected script can read and send away, usable for
  12 hours, and a vote waits for the next run.

### The same token, with the write rule in the Worker

* Good, because a vote would be stamped at once.
* Bad, because the topic lookup, the one-click window and the verdict rows would be written twice,
  in two languages, and the Worker would write `triaged_at` itself.
* Bad, because nothing reads the stamp before the next run.

### The session cookie, with `allow-same-origin`

* Good, because the cookie and `crossSite` would be the whole credential.
* Bad, because a script on any tenant page would then run as the host's origin: it could read
  other pages of its own tenant, call every same-origin route with whatever cookie the browser
  holds (an operator who also signed in as a reader included), and the whole of ADR-0033's
  isolation would rest on every template's escaping.

### One origin per tenant, with the cookie

* Good, because the cookie would work and pages would be isolated from each other.
* Bad, because `workers.dev` gives one hostname; a host per tenant needs a zone, a wildcard route
  and a certificate, and every link the run writes would change shape (ADR-0033).
* Bad, because a script on a tenant's own origin could still act with that tenant's session.

## More Information

* `docs/architecture.md` §4 has the `hosted_votes` row and the registry's `vote_key`, §5 the queue
  bound, and §6 the route.
* Unverified here: that a browser lets a sandboxed page's `fetch` to the one URL its policy names
  carry a custom header past the route's preflight, and that the buttons show on a real page. That
  needs a browser, and a deployed hosted Worker. The test reads the headers a browser needs; the
  operator should vote once on a trial page and read the report's `hosted_votes` count
  (`docs/hosted-tenants.md`).
