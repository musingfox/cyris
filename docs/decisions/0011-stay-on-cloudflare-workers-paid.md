---
status: accepted
date: 2026-10-08
decision-makers: ""
---

# Stay on Cloudflare Workers Paid, with the pipeline in a Container

## Context and Problem Statement

Since 2026-08-30 the pipeline runs in a Cloudflare Container fronted by a Worker, with state in D1
and the digest on Pages. On 2026-09-20 the bill was compared with the cheapest stack that would
do the same job.

Measured that month, everything metered sat under 26 % of the Workers Paid allotment, so the bill
was the $5 account minimum, not usage. With LLM spend of about $1.9 a month, the deployment cost
about $6.9 a month. The cheapest alternative priced at about $1.9 a month, so the $5 minimum was
the whole gap.

## Considered Options

* Stay on Workers Paid: Container, hourly buffer Worker, D1, KV and Pages on one account
* Move to the free plan
* Cloud Run Jobs for the pipeline and the hourly fetch, with Cloudflare's free plan for Pages, D1,
  the vote Worker and KV
* Vercel, Netlify, Fly.io, Render, Hetzner or GitHub Actions
* A Python Worker in place of the Container

## Decision Outcome

Chosen option: "stay on Workers Paid", because the $5 buys Containers, the hourly buffer Worker
and one bill instead of four vendors.

* The free plan cannot run this. Containers are a paid-only feature. A free Worker gets 10 ms of
  CPU per Cron Trigger invocation and 50 subrequests, and free D1 caps 50 queries per Worker
  invocation, while the RSS buffer ([ADR-0001](0001-rss-arrives-through-an-hourly-buffer.md))
  polls and writes every feed each tick.
* The Cloud Run stack is $0 before LLM spend, but it splits the deployment across Google and
  Cloudflare, and inbound newsletters, a durable backup and the admin UI each need another
  vendor or a local process.
* The other hosts each fail on one figure: Vercel Hobby cron runs at most daily; Netlify's
  scheduled functions stop at 30 s, below the ~64 s run; Fly.io has no ongoing free allowance;
  Render has no free cron instance and a $1 minimum per cron service; Hetzner has no free tier;
  GitHub Actions schedules can be delayed or dropped and auto-disable after 60 days of inactivity.
* A Python Worker would remove the Container's sleep timer entirely. It was spiked on 2026-09-24
  and not taken: sync `httpx` timed out on fresh isolates, `aiohttp` could not connect, `blake3`
  has no Pyodide wheel, and startup already used 934 of its 1,000 ms.

### Consequences

* Good, because the whole deployment is one account, one bill and one set of credentials.
* Bad, because the deployment pays $5 a month that a split stack would not.
* Neutral, because a privately tracked ticket would replace the RSS buffer Worker with an hourly
  tick that fetches feeds itself; the Container would still need the paid plan.

## More Information

Extracted on 2026-10-08 from these sources at commit `c353d3f`:

* `docs/hosting-and-cost.md:40-58` (§1's LLM spend and bill, §2 *What binds the paid plan*).
* `docs/hosting-and-cost.md:73-95` (§4 *The cheapest alternative stack, priced*, and the hosts
  ruled out).
* `docs/hosting-and-cost.md:139-140` (§7, *Stay on Cloudflare Workers Paid*).

The Python Worker option was added on 2026-10-08 from `docs/architecture.md:1015` (§7, *D1 calls
have no total time budget*) at commit `2b31535`.

`docs/hosting-and-cost.md` stays the measurement record: its §1 table, its free-plan ceilings and
its numbered vendor sources, all fetched 2026-09-20, back every figure above. Its §1 notes that
since 2026-10-08 the container starts only on the two digest hours, so the measured container
time is now an upper bound.
