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

- [x] **Raise `max_articles_per_digest` 200 → 400.** Done 2026-08-08. At 200 it truncated
      every run and threw away the buffer's whole benefit: on 08-08 08:00 Miniflux returned
      exactly 200 (capped) and the buffer 156, but the union was **203** — the buffer
      contributed 3 net articles because the cap had already saturated the pool.

      **It cost nothing.** The pre-change estimate of ~US$0.22/run and ~US$13/mo was
      extrapolated from a single high run (47,936 in / 7,656 out = US$0.129) that was high
      precisely *because* the cap had left a backlog. Measured after the change: 08-08
      evening US$0.042, 08-09 morning US$0.045 — cost went **down**. Across all 49 logged
      runs the average is US$0.073/run, ~US$4.38/mo at gemini-3.6-flash rates
      (US$1.50/1M in, US$7.50/1M out).

      The reason is that per-run volume is 120–170 unique articles — under both caps. Daily
      volume swings hard with the news cycle (~240 on a weekday, ~100 at a weekend), so 200
      bound only on busy days. At 400 the cap is a safety limit rather than a routine
      truncation, and the LLM only ever pays for the articles that actually arrive.

      If cost ever does matter, `scoring_snippet_length` (1000 chars × every article) is
      the input driver, and gemini-2.5-flash is 5× cheaper.
- [x] **Vote-similarity filtering does not need the cloud.** Shipped 2026-08-09 against the
      existing `GEMINI_API_KEY` (`adapters/embedding.py`), off by default, previewable with
      `cyris vote-sim`. This had been assumed to be gated on Workers AI + Vectorize; it is
      not. Verified over 168h / 1,238 candidates: 12 of 12 unvoted lottery articles
      suppressed, no false positives, generalising to 大樂透 and 威力彩 that neither seed
      contained. See [`vote-signal-measurement.md`](vote-signal-measurement.md).

      The only thing the free tier costs is patience: back-filling the corpus hit HTTP 429
      and needed exponential backoff. A paid Gemini key removes that for ~US$0.02/mo. The
      *storage* ceiling (81 MB of JSON vectors) is real and is what phase 3 addresses.
- [x] ~~Move the 9 Substack sources to the email path.~~ **Dropped — the 429s cost nothing.**
      Substack does rate-limit Cloudflare's egress (4–6 feeds per poll, and the failing set
      rotates), but over a 7-day window the buffer holds 13 Substack articles to Miniflux's
      5, with **zero missing**. The arithmetic: these feeds publish ~0.2 posts/day, the cron
      runs 24×/day, a Substack feed's snapshot holds ~20 items (weeks of depth), and
      retention is 8 days — so a post has dozens of chances to be picked up and needs one.
      Content is full-length too (12k–47k chars), so email buys nothing on completeness.

      Worth stating plainly because it cuts against the instinct: RSS is *more* reliable
      here, not less. Polling is idempotent and retryable at a time we choose; email is a
      single push with no replay, so a dropped delivery is gone. Email is the right path
      only where there is no usable feed — 曼報, ieo and 粉虱通訊 already live there.
- [x] **Close the buffer gap.** Closed — **the buffer is a strict superset of Miniflux.**
      Measured 2026-08-08 over the 17h since the cron outage ended: Miniflux 114 URLs,
      buffer 116, **zero missing**.

      The apparent 73% 中央社 capture rate in the 24h measurement was an artifact of
      measuring across the outage boundary. Hour by hour, every missing article falls in
      08-07 13:00–18:00 — while the Free-plan cron was still dying — and from 19:00 onward
      the buffer misses nothing. The "hourly tick is too slow" reading was wrong: CNA feeds
      hold 14–17h per snapshot, so an hourly poll cannot miss them.

- [ ] **Retire `MinifluxSource`** — now unblocked, but not yet earned. The parity receipt
      is 17 hours old, against a component whose failure mode is silent loss and which
      went fully dark for a day this week. Keep both running and re-run `compare.py` for a
      few more days; Miniflux costs nothing while the Mac mini is up anyway. Only phase 3
      actually requires it gone.

- [x] **Drop the TMTB source.** Its feed served one item dated 2023-08-24 — dormant for
      three years.
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
| `adapters/embedding.py` | 105 | Workers AI `@cf/baai/bge-m3` + Vectorize — **for storage, not for price** |

Plus: cross-build the amd64 image, replace supercronic with Workers Cron Triggers, and
wire `onActivityExpired` → `stop()`.

The embedding row is the one whose usual justification is wrong, so it is worth stating
plainly. Measured token volume is ~128k/month (titles average 18.6 tokens), which prices
at **US$0.019/mo on Gemini's paid tier and US$0.0015/mo on bge-m3** — a 12.5× ratio on a
number small enough that it can never justify the work. What does justify it is that the
local vector cache is a whole-file JSON rewritten on every miss and already stands at
**81 MB**; Vectorize replaces a read pattern that loads everything, not a bill.

`bge-m3` is the right target when that happens — it is multilingual (Cloudflare lists it
under "Multi-Linguality", 60k context), which the 62%-中央社 corpus requires. The
English-only trap is the `bge-*-en-v1.5` family, not this model.

Dropped rather than migrated: `output/digest.py` (Obsidian markdown), `adapters/cookies.py`,
`fetch/newsletter_source.py` (local maildir, superseded by the newsletter Worker).

## Unsolved regardless of plan or plan tier

- **Paid-source cookies.** `adapters/cookies.py` reads the live browser DB; that cannot
  follow to the cloud. Only stratechery is affected, and it also sends email — so the fix
  is the same routing change as Substack, not a cookie-sync mechanism.
- **The intermittent publish failure.** Reproduced 2026-08-09 08:01, and the receipt check
  caught what an exit code never would. It is **not** a no-op: wrangler printed its banner,
  got to `Uploading... (15/16)`, and then exited 0 mid-upload without its completion line.
  So the failure is a truncated upload that wrangler swallows into a success exit, not a
  run that did nothing. The retry succeeded 7 seconds later.

  Note the shape: 16 files in the deployment and it died on the last one. The archive grows
  by two files a day, so the upload phase gets longer every day — worth re-checking whether
  the failure rate tracks the file count. The phase-3 move to the REST API would replace
  this code path entirely.

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
