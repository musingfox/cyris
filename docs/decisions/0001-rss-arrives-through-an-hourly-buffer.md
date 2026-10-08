---
status: accepted
date: 2026-10-08
decision-makers: ""
---

# RSS arrives through an hourly buffer that is read, never drained

## Context and Problem Statement

A feed publishes only its current snapshot, and a busy feed holds 2–4 hours of it, not 24. A
digest that polls its feeds at digest time therefore cannot see a 24-hour window. Measured
against Miniflux over the same window, a digest-time poll missed 141 of 317 articles, all of them
from the high-volume feeds. What Miniflux had been providing was hourly *accumulation*, not
parsing, so its replacement had to accumulate too.

The question was how cyris gets RSS articles once nothing runs on a local machine, and in what
shape the pipeline reads them.

## Considered Options

* Poll every feed at digest time (`RssSource`)
* Keep Miniflux as the buffer
* An hourly Worker buffer that the digest drains with an ack, like the email queue
* An hourly Worker buffer in D1 that the digest reads by time window, with no ack

## Decision Outcome

Chosen option: "an hourly Worker buffer in D1, read by time window, with no ack", because only an
hourly poll sees a busy feed's whole day, and a buffer that is read rather than drained loses
nothing when a digest crashes.

`workers/rss` polls every feed on the hour and writes with `INSERT OR IGNORE`, so seeing an entry
again every hour costs nothing. `cyris` reads `GET /articles?after=&before=`, which selects by
when an entry entered the buffer, not by the date its feed gave it. There is deliberately no ack
endpoint. Deleting on read would defeat the buffer's purpose and lose a batch whenever a digest
crashes, while an idempotent read lets a crashed digest read the same window again.

### Consequences

* Good, because a late-listed entry still reaches the next run, since the read goes by buffer
  entry rather than by publish date.
* Good, because a crashed run loses nothing; the email path, a queue with an ack, loses at most
  its current batch.
* Bad, because the Worker needs Workers Paid. On the free plan the scheduled handler died with
  *Exceeded CPU Limit* on every tick, and the free plan's 50 subrequests per invocation would bind
  as the feed list grows.
* Bad, because reading by buffer entry would put a blog's months of feed history into the next
  digest. Entries the feed dates more than 8 days back are therefore dropped before the write.
* Bad, because each run's window must reach back past the previous run by more than one poll's
  duration. A poll stamps its rows when it starts, so a row can land after a run has read past
  its stamp.
* Neutral, because `RssSource` stays as the fallback when no buffer is configured, and it is
  correct only for slow feeds.

## Pros and Cons of the Options

### Poll every feed at digest time

* Good, because it needs no Worker and no buffer table.
* Bad, because it measurably missed 141 of 317 articles over one window.

### Keep Miniflux as the buffer

* Good, because it already accumulated hourly, which is the property the comparison showed mattered.
* Neutral, because the cloud move replaced it with `workers/rss/` as the buffer; the earlier
  advice to drop it for direct polling was the one measured wrong.

### A Worker buffer drained with an ack

* Good, because the buffer would never hold an entry twice.
* Bad, because a crashed digest would lose the batch it had already acknowledged.

## More Information

Extracted on 2026-10-08 from these sources at commit `c353d3f`:

* `docs/architecture.md:251-267` (§3.1, *RSS — a buffer, read idempotently*, and the paragraph
  on why the buffer exists).
* `docs/cloud-migration.md:35-45` (*Why a buffer, and not direct polling*) and
  `docs/cloud-migration.md:8-11` (the superseded advice to drop Miniflux for direct polling).
* `workers/rss/README.md:20-28` (*Needs Workers Paid* and *Why this exists*) and
  `workers/rss/README.md:38-40` (no ack endpoint).
* `docs/hosting-and-cost.md:54-58` (the free plan's Cron Trigger limits).

The buffer shares the app's D1 database because it reads the `sources` table the app writes; that
coupling is part of [ADR-0012](0012-the-app-deploys-from-the-repo-root.md).
