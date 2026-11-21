---
status: accepted
date: 2026-11-20
decision-makers: ""
---

# A hosted tenant's newsletters arrive at a random address and are queued in its own Durable Object

## Context and Problem Statement

A self-hosted deployment's newsletters arrive through `workers/newsletter`. Email Routing hands
each message to that Worker's `email()` handler, which stores it in one KV namespace under
`nl:<sha256(from|subject|date)>`. A run pulls the whole namespace with `GET /newsletters` and
deletes what it processed with `POST /ack`. Nothing in that path names a reader, because a
self-hosted deployment has one.

A hosted tenant has no newsletter path. Its run wires no newsletter queue, because the shared one
belongs to another deployment. On 2026-11-14 the human decided that the hosted service will not
read customers' mailboxes. Each tenant is instead given a random, unguessable address on the
operator's domain, and one catch-all rule routes every address to one Worker. If the existing
Worker served every tenant, three things would break:

* Two tenants sent the same issue produce the same KV key, so the later copy overwrites the
  earlier, and one tenant loses the issue.
* `GET /newsletters` and `/ack` span the whole namespace, so one tenant's run would read, and
  ack away, every other tenant's mail.
* Nothing keeps mail sent to an address nobody holds out of the queue.
* A hosted tenant has no newsletter source, and a run deletes every issue no source matches,
  so an issue that reached a tenant's queue would still reach no digest.

The security review of 2026-11-19 adds conditions for this card. The random part must be
unguessable and unrelated to the tenant id or email. The map from address to tenant must live
only in the registry, where no container can read it. A message to an unknown address must be
dropped without saying whether the address exists. Addresses must be revocable and rotatable.
Mail links must be http(s), and a "view in browser" link carrying a recipient token must not
appear where anyone else can see it.

On 2026-11-20 the product owner decided how mail to an address nobody holds is answered: it is
dropped in silence, not rejected. The card's 拒收 is reversed. Every refusal is answered the
same way, whatever its reason: an unknown address, a revoked one, a subaddress, a malformed
recipient, a message too large, a full queue. The answer the sender gets does not reveal whether
the address exists.

Which inbound domain the service uses is open. The owner is weighing a dedicated apex domain
with a catch-all rule against a subdomain with one literal rule per address, created over the
API. Cloudflare's documentation says a catch-all rule exists only on an apex domain, a subdomain
takes literal rules only, and a domain holds at most 200 rules
(https://developers.cloudflare.com/email-service/configuration/subdomains/,
https://developers.cloudflare.com/email-service/platform/limits/). This record does not
decide it: the handler reads only the recipient's local part, so either choice routes mail to
the same code.

## Decision Drivers

* Only the envelope recipient (`message.to`) identifies a tenant. A sender can write any From
  header it likes.
* A tenant's mail stays in the tenant's own object (ADR-0028), and no container holds a binding.
* Only the hosted Worker reaches the registry (ADR-0030).
* The self-hosted `workers/newsletter` keeps its behaviour, and the parse is shared, not copied.
* Spam to a catch-all address must cost little and store nothing.
* The answer to a sender must not say whether an address exists (the owner, 2026-11-20).

## Considered Options

* Store each message in the addressed tenant's own Durable Object, from the hosted Worker's
  `email()` handler.
* Keep one KV namespace and add the tenant to each key (`nl:<tenant>:<sha>`), with a per-tenant
  prefix list and ack.
* A separate inbound Worker in front of the hosted one.

## Decision Outcome

Chosen option: "store each message in the addressed tenant's own Durable Object". The card
offered two options, a write straight into the tenant's object or a tenant-keyed KV, and left the
choice to the implementer.

The tenant-keyed KV would put every tenant's mail in one namespace. Isolation would then depend
on every list and ack passing the right prefix, and a run container would need a credential for
that namespace. The object needs neither. A run reaches its queue through the gateway it already
has, which answers its own tenant and refuses every other with 403. Isolation therefore holds by
construction, as it does for the RSS buffer (ADR-0031). A separate Worker is ruled out for the
same reason ADR-0031 ruled one out: only the hosted Worker may reach the registry.

How it is built:

* **The address.** `randomAddress()` (`workers/app/src/inbound_address.js`) draws 16 bytes from
  the runtime's CSPRNG and writes them in lowercase RFC 4648 base32, 26 characters long. Nothing
  of the tenant goes into it. Its only map to a tenant is a row of the registry's
  `inbound_addresses`, a table ADR-0030 created empty for this card. The table's shape does not
  change.
* **Issuing, rotating and revoking.** The operator's routes, behind Access, are
  `GET /hosted/tenants/<tenant>/address`, `POST .../rotate-address` and
  `POST .../revoke-address`. A rotation issues a new address and deletes every old one in one
  synchronous turn of the registry, so a leaked address stops taking mail at once. A revocation
  deletes them all. An unregistered tenant gets 404, as on every other tenant route.
* **Receiving.** `cyris-hosted` exports `email()` (`workers/app/src/hosted_inbound.js`). It reads
  the local part of `message.to`. Anything outside the address pattern is dropped without a
  registry call, including a `+` subaddress or a tenant id. Otherwise it asks the registry for
  the tenant. It then parses the message with `workers/newsletter/src/record.js`, which the
  newsletter Worker now uses too, and hands the record to the tenant's object.
  `TenantDb.receiveNewsletter` stores it in the tenant's own `newsletters` table
  (`hosted_schema.sql`), keyed by the same `nl:<sha256(from|subject|date)>`. Two tenants each
  keep one row. The first arrival of an id is kept, and a later one changes nothing. That covers
  a re-delivered copy, and also a message whose forged From, Subject and Date give it a queued
  issue's id, so anyone who knows an address cannot overwrite what is queued there. The address
  is not stored with the message.
* **The domain is not checked.** Only a rule on a zone of the operator's own account can route
  mail to the Worker. The 128 bits that make an address unguessable are all in its local part,
  and the inbound domain is not chosen yet. The change that chooses it adds the domain as a
  setting and checks it in the handler.
* **Dropping.** The handler never calls `setReject`, `forward` or `reply`. A message is either
  stored or accepted and discarded, so every sender gets the same answer, the owner's decision
  of 2026-11-20. That covers an unknown address, a revoked one, a subaddress, a malformed
  recipient, a message over 5 MiB, a record over 1 MiB, and a queue at its 200-message cap. The
  log line for an unknown address names neither the address nor a tenant.
* **Sources.** A run ingests an issue only for a source whose `email_match` names its sender.
  The operator adds one to a seeded tenant with
  `POST /hosted/tenants/<tenant>/newsletter-source?name=&from=[&homepage=][&lane=]` behind
  Access (`scripts/hosted.sh newsletter-source`). It writes the row a self-hosted `/settings`
  writes: type `newsletter`, `email_match` `from:<address>`, lane `read` unless named. This
  route was chosen over the seed because the seed names fixed catalog feeds per tenant, and a
  tenant's newsletters are its own. The route refuses a name an RSS source already holds, and a
  tenant not seeded yet. The tenant settings card replaces it.
* **Pulling.** A hosted run's newsletter source is `TenantNewsletterSource`. It reads the queue
  through its gateway and matches senders as `CloudflareNewsletterSource` does, except that it
  ignores case. The route writes `email_match` in lower case, while `record.js` stores From as the
  sender wrote it (`Letter@News.example`), and an address's case carries no meaning. The KV
  source's match has since been fixed the same way (case-insensitive substring, empty From
  matches nothing); the hosted class still overrides it, as it reads its own queue. A run acks by deleting the processed
  ids in chunks of 50. A dry run reads without deleting.
* **Unmatched mail waits.** A message no source matches stays queued for
  `UNMATCHED_HOLD_DAYS` (8) from its arrival, and a run acks it only after that. The KV source
  deletes such a message at once, which suits a self-hosted reader who adds a source before
  subscribing. A hosted tenant subscribes with an address the operator gave it, and its source
  may be added after the first issue arrives, so deleting at once would lose that issue
  unread. Eight days is the RSS buffer's retention, so mail and feeds wait as long. The hold also
  keeps Gmail's forwarding confirmation, which matches no source, readable in the operator's
  report.
* **Held mail never crowds out new mail.** A run stamps `held_at` on each message it leaves
  queued for a source. When the 200-message queue is full, the store evicts the oldest stamped
  message for the new one, in the same synchronous turn. Without that, anyone holding a leaked
  address could send 200 distinct messages and have every later issue dropped for 8 days, and
  rotating the address would not help, since rotation touches only the registry.
  * **Why eviction.** It is the smallest sound fix. A separate cap for held mail would add a
    second limit, and a shorter hold would shorten the window a reader has to add a source. The
    Worker can tell held mail apart only through the run's stamp, because matching a sender is
    the run's code, not the Worker's.
  * **What remains.** A queue full of messages that arrived since the last run, which nobody has
    sorted yet, still drops new mail until the next run stamps them. That lasts at most one
    schedule interval, and a rotation stops the flood.
* **Links.** An issue's link must be http(s), or it is no link. That applies to the article URL
  `newsletter_article` picks and to a reference link `citation_links` draws.
  `TenantNewsletterSource` passes `recipient_links=False`, so a hosted issue takes no link that
  may name its recipient. Two kinds of link are affected. The labelled "view in browser" link
  and the ESP-archive link, the two fallbacks, are never taken. The structural step still
  chooses as it does for every deployment, but a hosted issue drops its choice when it keeps a
  query after the tracking strip. A per-recipient token such as `?token=` cannot be told from
  any other query by its shape, and the strip lists name only parameters known to be one. The
  issue then keeps its source's homepage, or no link. A fragment is stripped and the link kept
  (`#comments`). A fragment never reaches the server, so it cannot carry a token to the sender.
  Stripping it still keeps one written there (`#sub=`) off the page. The choice is filtered after
  it is made, never before: dropping the token variant from the candidates first could make a
  second page look like the one canonical post, against CLAUDE.md's rule that an ambiguous issue
  gets no link.
* **Authentication.** The handler logs Cloudflare's own SPF, DKIM and DMARC verdicts for each
  stored message. It reads them from the topmost `Authentication-Results` header, and only when
  its authserv-id is `mx.cloudflare.net`. It does not act on them yet (see Consequences).

### Consequences

* Good, because two tenants sent one issue each get it in their digests. Each copy is a row in
  its tenant's own object, nothing is keyed across tenants, and each tenant's run ingests it
  through the source the operator added.
* Good, because one tenant's run can neither read nor ack another's mail. Its only way to any
  table is a gateway that answers its own tenant alone.
* Good, because the address carries 128 random bits and nothing else. Knowing a tenant says
  nothing about its address, and a guess over SMTP costs one delivery attempt. No container can
  read the map, because only the registry holds it.
* Good, because spam to the catch-all costs one Worker invocation and at most one registry read.
  A recipient outside the pattern costs no read at all.
* Good, because every sender gets the same answer whether the address exists or not, so even
  the one-message signal a rejection gave is gone.
* Bad, because a sender who mistyped an address, or still sends to a rotated one, is never told
  that the mail did not arrive. The owner accepted this on 2026-11-20.
* Bad, because a forged message that pre-empts a future issue's exact From, Subject and Date is
  kept, and the real issue is then taken as its copy. Doing that needs the address and the
  issue's Date to the second. Keying by a random id instead would let every re-delivered copy
  through as a duplicate.
* Bad, because a flood to a leaked address still fills the queue between two runs, and new
  mail is dropped until the next run stamps the flood held. After that, each new message evicts
  held spam, so the loss lasts at most one schedule interval, not the 8-day hold.
* Bad, because a flood can evict real held mail too, an issue whose source has not been added
  yet. The oldest stamp goes first.
* Bad, because a hosted issue loses its 原文 link when the only candidate was a web-version
  link, or a post link with any query left, `?p=123` included. The view link of an ESP such as
  Substack signs in as the recipient, and today a hosted page is read by the operator as well as
  by its tenant (ADR-0015 and the 2026-11-19 review, H1). Losing the link is the price of keeping
  that token off the page. A token written into a URL's path is not caught by this rule. The body
  that holds the links is stored only in the tenant's own object, and no page draws it.
* Bad, because rotating an address drops mail already in flight to the old one. No grace period
  is kept: the reason to rotate is usually a leak.
* Bad, because the Worker logs Cloudflare's DMARC verdict and does not yet require a pass. A
  message from a publisher whose DMARC policy is `p=none` can be spoofed in a tenant's queue. It
  still reaches no digest unless it also matches a source's `email_match`. Whether Gmail's filter
  forwarding passes DMARC has not been measured. The runbook records it before any rule is
  written against the verdict.
* Bad, because the hosted Worker now bundles `postal-mime` from `workers/newsletter`, so that
  package has to be installed before a hosted deploy and in CI, as `workers/rss` already is.
* Neutral, because the routing rule, a catch-all or literal rules, is set in the zone's Email
  Routing, outside `wrangler.hosted.toml`, which does not change.

### Confirmation

* `workers/app/test/hosted_inbound.test.js` drives `hosted.js` itself over SQLite stand-ins for
  the registry and the tenant objects. It checks that:
  * one issue sent to two tenants is stored in both;
  * a re-delivered copy and a forged copy with the same From, Subject and Date leave the first
    arrival's row as it was;
  * an address never issued, a revoked one, a rotated one, a subaddress, a tenant id and a
    malformed recipient are each dropped: the handler calls no `setReject`, `forward` or
    `reply`, answers as it does for a live address, and reaches no tenant object;
  * oversize messages and a full queue are dropped the same way;
  * a flood of 200 messages from an unmatched sender holds back new mail only until a run stamps
    them held; after that, each new issue evicts the oldest held spam;
  * the `newsletter-source` route writes each row of `workers/app/test/newsletter_source_cases.json`,
    refuses each refused case, an RSS source's name and an unseeded tenant, and lets two tenants
    hold the same source;
  * a paused tenant still takes mail;
  * the address appears in none of the tenant's tables;
  * alpha's gateway reads only alpha's queue, its ack deletes nothing of bravo's, and it is
    refused bravo's path;
  * the address routes issue, read, rotate and revoke an address, and refuse unregistered
    tenants;
  * an address depends on the random bytes alone.
* `tests/test_tenant_newsletter_source.py` checks several things:
  * the run's source pulls and acks only through its own tenant's gateway;
  * it leaves the queue alone on a preview, and chunks its ack;
  * it holds unmatched mail for 8 days, stamps what it holds (a preview does not), and ingests
    it once its source is added;
  * a mixed-case sender matches the route's lowercase source, by From or by a manual forward's
    body, and an empty sender matches none;
  * it never takes a link that may name the recipient;
  * each case's row reads back as a fetchable newsletter source;
  * one issue reaches both tenants' runs' articles through that source.
* `tests/test_hosted_run_wiring.py` checks that `build_deps` wires the source for a hosted run.
* `tests/test_newsletter.py` and `tests/test_html_digest.py` check the http(s) rule and the
  `recipient_links` switch, including the reviewer's probe:
  `https://news.example/p/issue-12?token=SECRET` is no link for a hosted issue, and stays the
  self-hosted choice.
* What runs on a real account: the routing rule and one Gmail filter forward to a tenant's
  address are steps in `docs/hosted-tenants.md`, and nobody has run them yet.

## Pros and Cons of the Options

### Store each message in the addressed tenant's own Durable Object

* Good, because the run's existing gateway is its only way in, so isolation needs no new
  credential and no new check.
* Good, because it is the RSS buffer's shape: a tenant's inbound data lives in the tenant's object.
* Bad, because each message costs a Durable Object call, and the object's 2 MB row bounds a
  message's size.

### One KV namespace with tenant-keyed entries

* Good, because the newsletter Worker's code would barely change.
* Bad, because every tenant's mail would share one namespace. Every list and every ack would
  have to carry the right prefix, and a run container would need a KV credential that reaches
  all of it.
* Bad, because KV is eventually consistent, so an ack could be read back as still queued at
  another location.

### A separate inbound Worker

* Good, because a fault in mail parsing could not reach the hosted Worker's other routes.
* Bad, because it could not read the registry, which only the hosted Worker may reach (ADR-0030).

## More Information

* `docs/architecture.md` §3.1 describes the path, §4 has the `newsletters` row and the registry
  row's `inbound_addresses`, §5 grades the address shape and the caps, and §6 describes the
  Worker's `email()` handler and routes.
* `docs/hosted-tenants.md` routes an inbound domain, issues each tenant an address, adds a
  newsletter source, and records a Gmail filter forward's `Authentication-Results`.
* ADR-0030 created `inbound_addresses` empty for this card. ADR-0031 is the RSS buffer this
  follows.
