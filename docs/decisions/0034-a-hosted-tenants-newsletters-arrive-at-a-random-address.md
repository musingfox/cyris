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
* Nothing refuses mail sent to an address nobody holds.

The security review of 2026-11-19 adds conditions for this card. The random part must be
unguessable and unrelated to the tenant id or email. The map from address to tenant must live
only in the registry, where no container can read it. A message to an unknown address must be
dropped without saying whether the address exists. Addresses must be revocable and rotatable.
Mail links must be http(s), and a "view in browser" link carrying a recipient token must not
appear where anyone else can see it.

## Decision Drivers

* Only the envelope recipient (`message.to`) identifies a tenant. A sender can write any From
  header it likes.
* A tenant's mail stays in the tenant's own object (ADR-0028), and no container holds a binding.
* Only the hosted Worker reaches the registry (ADR-0030).
* The self-hosted `workers/newsletter` keeps its behaviour, and the parse is shared, not copied.
* Spam to a catch-all address must cost little and store nothing.

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
  the local part of `message.to`. Anything outside the address pattern is refused without a
  registry call, including a `+` subaddress or a tenant id. Otherwise it asks the registry for
  the tenant. It then parses the message with `workers/newsletter/src/record.js`, which the
  newsletter Worker now uses too, and hands the record to the tenant's object.
  `TenantDb.receiveNewsletter` stores it in the tenant's own `newsletters` table
  (`hosted_schema.sql`), keyed by the same `nl:<sha256(from|subject|date)>`. A re-delivered copy
  therefore replaces its own row, while two tenants each keep one. The address is not stored with
  the message.
* **Refusing.** Every refusal calls `setReject` with one fixed reason, `REFUSAL`. That covers an
  unknown address, a revoked one, a message over 5 MiB, a record over 1 MiB, and a queue at its
  200-message cap. The SMTP answer therefore says the message was not taken, and never which of
  those reasons applied. The log line for an unknown address names neither the address nor a
  tenant.
* **Pulling.** A hosted run's newsletter source is `TenantNewsletterSource`. It reads the queue
  through its gateway, matches senders exactly as `CloudflareNewsletterSource` does, and acks by
  deleting the processed ids in chunks of 50. A dry run reads without deleting.
* **Links.** An issue's link must be http(s), or it is no link. That applies to the article URL
  `newsletter_article` picks and to a reference link `citation_links` draws.
  `TenantNewsletterSource` passes `web_version=False`. For a hosted issue, the labelled "view in
  browser" link and the ESP-archive link are therefore never the article URL. The issue keeps its
  sender's canonical post link, or its source's homepage, or no link.
* **Authentication.** The handler logs Cloudflare's own SPF, DKIM and DMARC verdicts for each
  stored message. It reads them from the topmost `Authentication-Results` header, and only when
  its authserv-id is `mx.cloudflare.net`. It does not act on them yet (see Consequences).

### Consequences

* Good, because two tenants sent one issue each get it. Each copy is a row in its tenant's own
  object, and nothing is keyed across tenants.
* Good, because one tenant's run can neither read nor ack another's mail. Its only way to any
  table is a gateway that answers its own tenant alone.
* Good, because the address carries 128 random bits and nothing else. Knowing a tenant says
  nothing about its address, and a guess over SMTP costs one delivery attempt. No container can
  read the map, because only the registry holds it.
* Good, because spam to the catch-all costs one Worker invocation and at most one registry read.
  A recipient outside the pattern costs no read at all.
* Bad, because the SMTP answer itself still tells a sender whether this one message was taken.
  The card requires a rejection rather than a silent drop, and the address can only be found by
  guessing 128 bits, so the remaining signal is useless for finding one. A rejection also tells a
  real sender who mistyped the address that the mail did not arrive.
* Bad, because a hosted issue loses its 原文 link when the only candidate was a web-version
  link. The view link of an ESP such as Substack signs in as the recipient, and today a hosted
  page is read by the operator as well as by its tenant (ADR-0015 and the 2026-11-19 review, H1).
  Losing the link is the price of keeping that token off the page. The body that holds the link is
  stored only in the tenant's own object, and no page draws it.
* Bad, because rotating an address bounces mail already in flight to the old one. No grace
  period is kept: the reason to rotate is usually a leak.
* Bad, because the Worker logs Cloudflare's DMARC verdict and does not yet require a pass. A
  message from a publisher whose DMARC policy is `p=none` can be spoofed in a tenant's queue. It
  still reaches no digest unless it also matches a source's `email_match`. Whether Gmail's filter
  forwarding passes DMARC has not been measured. The runbook records it before any rule is
  written against the verdict.
* Bad, because the hosted Worker now bundles `postal-mime` from `workers/newsletter`, so that
  package has to be installed before a hosted deploy and in CI, as `workers/rss` already is.
* Neutral, because the catch-all rule is set in the zone's Email Routing, which is a dashboard
  step. Nothing in `wrangler.hosted.toml` changes.

### Confirmation

* `workers/app/test/hosted_inbound.test.js` drives `hosted.js` itself over SQLite stand-ins for
  the registry and the tenant objects. It checks that:
  * one issue sent to two tenants is stored in both;
  * an address never issued, a revoked one, a rotated one, a subaddress, a tenant id and a
    malformed recipient are each refused with `REFUSAL`, and no tenant object is reached;
  * oversize messages and a full queue are refused the same way;
  * a paused tenant still takes mail;
  * the address appears in none of the tenant's tables;
  * alpha's gateway reads only alpha's queue, its ack deletes nothing of bravo's, and it is
    refused bravo's path;
  * the address routes issue, read, rotate and revoke an address, and refuse unregistered
    tenants;
  * an address depends on the random bytes alone.
* `tests/test_tenant_newsletter_source.py` checks that the run's source pulls and acks only
  through its own tenant's gateway, leaves the queue alone on a preview, chunks its ack, and never
  takes a web-version link. `tests/test_hosted_run_wiring.py` checks that `build_deps` wires it
  for a hosted run.
* `tests/test_newsletter.py` and `tests/test_html_digest.py` check the http(s) rule and the
  `web_version` switch.
* What runs on a real account: the catch-all rule and one Gmail filter forward to a tenant's
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
* `docs/hosted-tenants.md` sets up the catch-all rule, issues each tenant an address, and
  records a Gmail filter forward's `Authentication-Results`.
* ADR-0030 created `inbound_addresses` empty for this card. ADR-0031 is the RSS buffer this
  follows.
