---
status: accepted
date: 2026-11-28
decision-makers: ""
---

# The Pages origin may sit behind Access, read with a service token the Worker and the publisher send

## Context and Problem Statement

The app Worker proxies the digest archive from its Pages project
([ADR-0010](0010-the-digest-stays-static-behind-the-app-worker.md)), and `CYRIS_PRIVATE_ARCHIVE`
puts the Worker's hostname behind the Access sign-in. The Pages project still answers at
`<project>.pages.dev`, and each deployment at its own hostname under it, to anyone, so a private
archive has a public copy. [ADR-0029](0029-a-hosted-tenants-pages-live-in-its-durable-object.md)
counted this against a Pages project per hosted tenant: such a project "is public at
`<name>.pages.dev`, whatever the Worker in front of it checks".

An Access application over `<project>.pages.dev` would close that copy, but two readers of the
origin carry no credential. The Worker's proxy (`workers/app/src/router.js`) strips the reader's
Access JWT and cookie and sends nothing in their place. The publisher's four reads of the live site
(`src/cyris/adapters/output/publish.py`: `_fetch_live_index`, `_fetch_live`, `page_serves`,
`_page_is_live`) fetch `https://<project>.pages.dev` directly. Behind Access each would get the
login page: every archive page would fail through the Worker, the deploy guard would refuse every
deploy once a manifest exists, and no digest would count as published.

## Decision Drivers

* A private archive should be private at every hostname that serves it.
* A deployment that does not put Access on its Pages project must run exactly as before.
* The reader's own Access credentials must not reach the origin, which is configurable
  (`DIGEST_ORIGIN`), and a reader must not be able to choose the credential the origin sees.
* `CYRIS_ACCESS_CLIENT_ID` and `CYRIS_ACCESS_CLIENT_SECRET` are the operator's script token, sent
  to the Access-guarded Worker and to the hosted gateway, and never forwarded to the container
  (`docs/architecture.md` §5).
* `service_layer/` and `domain/` stay unchanged (`docs/architecture.md` §8).

## Considered Options

* A dedicated Access service token for the Pages origin, which the Worker and the publisher send
* Reuse the script token `CYRIS_ACCESS_CLIENT_ID`/`CYRIS_ACCESS_CLIENT_SECRET`
* Forward the reader's Access JWT to the origin
* Serve the archive from somewhere that is not public by default
* Leave `<project>.pages.dev` public

## Decision Outcome

Chosen option: "a dedicated Access service token for the Pages origin", because it closes the
public copy with one dashboard step and two optional secrets, and changes nothing where they are
unset.

* **Two grade-C secrets.** `CYRIS_ARCHIVE_ACCESS_CLIENT_ID` and
  `CYRIS_ARCHIVE_ACCESS_CLIENT_SECRET`, set on `cyris-app` as Worker secrets. They are used only
  when both are set; half a token sends nothing.
* **The Worker.** Every request it proxies to the archive origin carries them as
  `CF-Access-Client-Id` and `CF-Access-Client-Secret`. Any `CF-Access-Client-*` header the
  reader sent is dropped first, and the reader's JWT and cookie are stripped as before.
* **The publisher.** The Worker forwards both into the container with the other run secrets, and
  the four reads of the live site send them. They are read in `publish.py`, beside the Pages API
  token `_client` reads there, because `scripts/prune_archive.py` calls those reads directly.
* **The host check.** Each of the four reads treats an answer whose final URL, after redirects,
  is on another host than the one it asked as it treats a non-200: a warning naming the host it
  landed on, and no content. Pages' own redirect stays on the host and still reads. The check holds
  whether or not the token is set.
* **Access.** Creating the Access application over the Pages project, and a Service Auth policy
  that admits this token, is the deployer's dashboard step. The application has to cover each
  deployment's own hostname under `<project>.pages.dev` as well as the project's, or a deployment
  URL stays public.

### Consequences

* Good, because `CYRIS_PRIVATE_ARCHIVE` together with this token and an Access application over
  the Pages project leaves no hostname that serves the archive without sign-in.
* Good, because with the secrets unset the Worker and the publisher send exactly what they sent
  before, so a public archive and a local install are unchanged.
* Good, because the token is separate from the script token: either can be rotated or revoked
  alone, and the script token still never enters the container.
* Bad, because a token the Access policy does not admit, or an Access application set up before
  the token, fails every read of the origin, so no digest publishes until it is fixed. Access is
  expected to redirect such a read to its sign-in page (not measured here), and the four reads
  follow redirects (`follow_redirects=True`, which Pages' `.html` → clean-URL redirect needs). A
  sign-in page that answered 200 would list no issue, so the deploy guard would pass as it does for
  an empty archive, and an evicted asset would be recovered as the sign-in page. The host check
  below makes a rejected token fail the guard instead of passing it: the read is unread, the
  guard refuses to deploy over a manifest, and recovery and `_page_is_live` fail.
* Bad, because httpx keeps custom headers across a redirect to another host, so a rejected read
  carries the token to the host it is redirected to, which is Access itself.
* Bad, because a script that reads the live site from a clone, such as
  `scripts/prune_archive.py`, needs the two values in that clone's environment too.
* Neutral, because the hosted edition has no Pages project (ADR-0029) and does not take the token.

### Confirmation

* `workers/app/test/router.test.js`, *ArchiveOriginServiceToken*: the token is sent when both
  halves are set, nothing is sent when either is unset, and a reader's `CF-Access-Client-*` headers
  are replaced.
* `tests/test_publish.py`: each of the four reads sends the token when both halves are set and no
  Access header otherwise, fails when a redirect lands on another host, and still reads through a
  same-host redirect to the clean URL.
* `tests/test_worker_env.py` and `tests/test_deploy_inputs.py` hold the forwarded names, the deploy
  form and `.env.example` in step.

## Pros and Cons of the Options

### A dedicated Access service token for the Pages origin

* Good, because one credential serves both readers of the origin, and neither is a reader.
* Bad, because the deployer has one more token and one more Access application to keep.

### Reuse the script token

* Good, because there is no new secret.
* Bad, because the script token would then enter every container, which §5 rules out, and
  revoking it for a leaked script would break publishing.

### Forward the reader's Access JWT to the origin

* Bad, because the publisher has no reader, so it still could not read the origin.
* Bad, because the origin is configurable, which is why the Worker strips the JWT (ADR-0010).

### Serve the archive from somewhere that is not public by default

* Bad, because it reopens ADR-0010, whose reason, that a page view must not wake a container,
  still holds, and the Pages publisher and its guards would need a replacement.

### Leave `<project>.pages.dev` public

* Good, because nothing changes.
* Bad, because a private archive keeps a public copy at a hostname anyone can guess from the
  project name.

## More Information

* This amends ADR-0010. The Worker still proxies Pages and still keeps the reader's credentials
  from it, but the proxied request may now carry a credential of the Worker's own.
* ADR-0029's "public at `<name>.pages.dev`, whatever the Worker in front of it checks" no longer
  holds for a self-hosted deployment that sets this token and puts its Pages project behind Access.
  It still holds for one that does not.
* `docs/architecture.md` §5 grades the two secrets, and §6 (*The reader-facing surfaces share one
  hostname*) describes the proxy.
