---
status: accepted
date: 2026-10-08
decision-makers: ""
---

# The digest stays static on Pages, and the app Worker proxies it

## Context and Problem Statement

Since 2026-08-30 the digest archive, each issue and `/settings` share one hostname behind the app
Worker. The Worker can either hand a reader's request to the container, which renders or serves
the page, or proxy the static files already deployed to Cloudflare Pages. On 2026-09-20/21 the
question came back as whether the archive index should become a dynamic, container-served page.

## Considered Options

* Serve the digest and its archive from the container
* Make the archive index dynamic and container-served, the issues static
* Keep every reader page static on Pages, and split the hostname by path

## Decision Outcome

Chosen option: "keep every reader page static on Pages, split by path", because the container is
priced by the minutes it is awake, and the published index is load-bearing.

```
/                         digest index  ─┐ public unless CYRIS_PRIVATE_ARCHIVE is "true"
/2026-08-30-evening.html  one digest    ─┘ (then the cookie too): the Worker proxies Pages
/settings · /api/* · /static/*            the container, behind the CYRIS_UI_TOKEN cookie
```

* Serving the digest from the container would wake a container to hand back a file Cloudflare
  already holds. Container memory and disk accrue for the whole awake window, and `sleepAfter`
  ends it, so a page view costs almost no CPU but resets the sleep timer. Kept awake for a month
  by traffic, crawlers included, a `basic` instance prices at about $7 before requests, the
  Durable Object and egress. Pages serves static requests free and unlimited.
* Putting the archive of record behind an auth layer would turn every Discord link into a login.
* The published `/index.html` is load-bearing beyond readers. The deploy guard fetches the live
  index and refuses to deploy when it cannot be read, recovery rebuilds `pages_manifest` from it,
  and every archived page links `index.html` relatively. No public, unauthenticated,
  container-served path exists to put a dynamic index on: every container fetch sits behind the
  cookie gate in `workers/app/src/router.js`.

### Consequences

* Good, because reading the archive costs one Worker request and never wakes the container.
* Good, because the archive stays public by default, which is what makes a digest worth sending
  to someone; `CYRIS_PRIVATE_ARCHIVE` closes it for a deployment that wants it private.
* Bad, because a reader-facing change to an already-published issue needs either a republish or
  an injection by the Worker (the type size, [ADR-0013](0013-reader-type-size-is-injected-by-the-worker.md)).

## More Information

Extracted on 2026-10-08 from these sources at commit `c353d3f`:

* `docs/architecture.md:1156-1172` (*The reader-facing surfaces*, the path split and why).
* `docs/hosting-and-cost.md:97-118` (§5 *Serving the reader from a container, priced*, and the
  *dynamic archive index* entry of §6).
* `docs/hosting-and-cost.md:141-142` (§7, *The digest stays static*).

`docs/hosting-and-cost.md` stays the measurement record, with the vendor sources for every figure.
