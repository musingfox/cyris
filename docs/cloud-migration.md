# Cloud Migration Plan

> Status: phase 0 done, phase 1 in progress. Supersedes the Cloudflare half of
> [`deployment.md`](deployment.md), whose "drop Miniflux, fetch RSS directly" plan was
> measured and found wrong (see [Why the buffer](#why-a-buffer-and-not-direct-polling)).

Goal: the HTML digest keeps being produced with no always-on machine, on the US$5/mo
Workers Paid plan. Obsidian output is handled separately and is not a migration target.

## Verdict

**US$5/mo covers the compute with ~8× headroom. It does not cover the work.** The
remaining migration is roughly 1,300 lines of adapter rewriting, dominated by
`article_store.py`. Nothing in `service_layer/` or `domain/` moves.

## Phase 0 — done

| Piece | Where it runs |
|---|---|
| Digest vote clicks | `workers/promote/` → KV |
| Newsletter email ingestion | `workers/newsletter/` → KV |
| Feed buffer | `workers/rss/` cron → D1 |
| Published digest | Cloudflare Pages |

Still local: the `cyris` container on a Mac mini (digest at 08:00 and 20:00),
Miniflux + Postgres, the triage UI, and all persistent state as JSON files.

### Why a buffer, and not direct polling

A feed publishes only its current snapshot, and high-volume feeds hold 2–4h of it.
Polling once at digest time cannot see a 24h window: measured against Miniflux over
the same window, a digest-time poll missed 141 of 317 articles, all from those feeds.
What Miniflux provides is hourly *accumulation*, not parsing — so the replacement has
to be a scheduled buffer, which is what `workers/rss/` is.

`RssSource` (the direct-polling adapter that comparison was built on) stays in the tree
unwired, as the local fallback for when `MinifluxSource` is retired.

## Budget

Measured, not estimated. Two runs a day, 60/month, 1.3–3 min each, 97.4 MiB peak.

| Resource | Use | Paid allowance | % |
|---|---|---|---|
| Container memory (`basic`, 1 GiB × 3h) | 3 GiB-hours | 25 GiB-hours | 12% |
| Container CPU (1/4 vCPU, provisioned) | 45 vCPU-min | 375 vCPU-min | 12% |
| Container disk (4 GB × 3h) | 12 GB-hours | 200 GB-hours | 6% |
| Worker CPU (RSS poll, 471ms × 720) | ~6 min | 30M ms | 1.2% |
| Worker requests | ~2k | 10M | 0.02% |
| D1 rows written | ~7.5k/mo | 50M/day | ~0% |
| D1 storage | 2.05 MB | 5 GB | ~0% |

Two things this table assumes:

- **The container must stop itself.** Billing runs until the instance sleeps, so
  `onActivityExpired` calling `stop()` is a requirement, not an optimisation — at the
  10-minute default idle, 60 runs would add 10 container-hours and take memory to 52%.
- **`basic` is the safe instance, not the cheapest.** Peak memory is 97.4 MiB, so `lite`
  (256 MiB) would likely fit and cut memory use 4×. Not worth the risk until a run has
  been profiled inside a container.

Image size is not a constraint: 672 MB against `basic`'s 4 GB disk.

## Design constraints

These decide the shape of the work, so they are settled before any code moves.

1. **`ArticleRepository` is a synchronous Protocol.** `ports.py` declares `def save(...)`,
   and `run_digest.py` calls it without `await`. From Python in a Container, D1 is not a
   binding — it is HTTP. The D1-backed store therefore **must use a blocking client**
   (as `newsletter_worker_source.py` already does). An async adapter would push `async`
   up through every call site and straight into `service_layer/`, which is the one thing
   this plan promises not to touch. If this constraint fails, the plan needs rewriting.
2. **The surface callers actually use is 15 methods across `ArticleStore` and
   `EventStore`, not the 9 in the Protocol.** Callers also use
   `delete_articles`, `list_articles`, `load_events`, `mark_stale_inactive`, `save_event`
   and `update_triage_timestamp`. A replacement satisfying only the Protocol will not run.
3. **linux/amd64 only.** The build host is arm64, so cross-building becomes part of the
   release path. This is the only item that changes the build workflow rather than code.
4. **All container disk is ephemeral** — a woken instance gets a fresh image. Nothing may
   rely on files surviving between runs.
5. **Config gets baked into the image.** `cyris.toml` and `sources.yaml` are `:ro` mounts
   today, so adding a feed is a file edit; in a Container it becomes rebuild + deploy.
   Accepted for now; move to KV if it starts to bite.
6. **HTTP-only ingress.** A Worker fronts the container; nothing else can reach it.

## Phase 1 — prerequisites (no migration yet)

None of this needs the cloud, and all of it is wrong to carry forward.

- [ ] **Raise `max_articles_per_digest`.** It is 200 and truncates on every run. The
      buffer's entire benefit is more articles inside the 24h window, and the cap throws
      them away: on 2026-08-08 08:00 Miniflux returned exactly 200 (capped) and the buffer
      156, but the union was **203** — the buffer contributed 3 net articles because the
      cap had already saturated the pool. Until this changes the buffer is paid for and
      unused. ~400 costs about US$0.20 → US$0.35 per run in LLM spend.
- [ ] **Move the 9 Substack sources to the email path.** Substack rate-limits Cloudflare's
      egress (HTTP 429 on 4–5 feeds per poll). Most of them send email, and the newsletter
      Worker already handles that. This is a routing fix, not a retry-tuning problem.
- [ ] **Close the remaining buffer gap.** First clean comparison, 2026-08-08 over 24h:
      Miniflux 234 URLs, buffer 205, 191 shared. Near parity, and the buffer already finds
      14 that Miniflux misses (12 Wired). The deficit is 42 中央社 articles. Those feeds
      are **not** failing in the poll — the buffer holds 113 中央社 rows over the same
      window, i.e. 73% of Miniflux's 155. That is a capture rate, not an outage, so the
      hourly tick is too slow for feeds that churn faster than their snapshot depth.
      Fix by polling those three feeds more often, not by retrying harder. Miniflux
      cannot be retired until the rate is ~100%.
- [ ] **Retire `MinifluxSource`** once the comparison is clean. This is what frees the plan
      from needing Postgres anywhere.

## Phase 2 — state to D1, compute stays local

The risky half, done while the local store is still running and can be diffed against.

| Adapter | Lines | Target |
|---|---|---|
| `store/article_store.py` | 526 | D1 |
| `store/events.py` + `store/event_store.py` | 256 | D1 |
| `tracking_yaml.py` | 117 | KV |
| `output/usage_log.py` | 41 | D1 |

526 lines does not mean 526 new lines: the 8-day dedup scan currently reads eight local
partitions per `save`, and over SQL that is one query.

`triage_server.py` reads the same store, so it **moves in this phase or it breaks**. It is
not deferrable scope — per the two-channel model it is the knowledge gate and the only
source of real-human training signal, and `cyris learn` reads what it writes.

Independently valuable even if phase 3 never happens: once state is in D1, a dead Mac mini
loses nothing.

## Phase 3 — compute to Container

Mechanical once phase 2 lands, with no new failure modes left untested.

| Adapter | Lines | Change |
|---|---|---|
| `output/html_digest.py` | 180 | write to R2 instead of disk |
| `output/publish.py` | 78 | Pages REST API — `bunx wrangler` cannot shell out from a Worker-fronted container |
| `output/article_export.py` | 106 | R2, or drop with the Obsidian path |

Plus: cross-build the amd64 image, replace supercronic with Workers Cron Triggers, and
wire `onActivityExpired` → `stop()`.

Dropped rather than migrated: `output/digest.py` (Obsidian markdown), `adapters/cookies.py`,
`fetch/newsletter_source.py` (local maildir, superseded by the newsletter Worker).

## Unsolved regardless of plan or plan tier

- **Paid-source cookies.** `adapters/cookies.py` reads the live browser DB; that cannot
  follow to the cloud. Only stratechery is affected, and it also sends email — so the fix
  is the same routing change as Substack, not a cookie-sync mechanism.
- **The intermittent publish no-op.** `wrangler pages deploy` exiting 0 having deployed
  nothing. Masked by a bounded retry and now detected by receipt; root cause unknown. The
  phase-3 move to the REST API may remove it by removing the shell-out, but that is a
  side effect, not a diagnosis.

## Keeping the current system running

Every phase above leaves the existing pipeline working, because each one lands behind the
adapter seam and is wired only when configured — `CloudflareRssSource` is already opt-in
on `[rss] worker_url` + `CYRIS_RSS_TOKEN`, and `fetch_all_articles` dedups by URL, so old
and new sources run side by side. Verify a change with the receipts, not the exit code:

```bash
# the published digest carries today
curl -sfL https://<pages>.pages.dev/ | grep -o '2026-[0-9-]*'
# the archive grew rather than being replaced
ls agent-vault/html/*.html | wc -l
# the buffer is current
curl -s -H "Authorization: Bearer $CYRIS_RSS_TOKEN" <worker>/stats
```
