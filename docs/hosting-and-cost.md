# Hosting and cost

What this deployment spends, what the paid plan buys that the free plan cannot, what a cheaper stack
would look like, and the platform questions settled on 2026-09-20/21. *Measured* figures come from
this deployment's D1 and `wrangler d1 info` on those dates; vendor figures carry an `[n]` into the
source list, and a vendor number that does not exist is **not stated**, never estimated.

## 1. What this deployment actually uses

Container rows come from 60 digest runs/month at ~64 s each (measured) on `instance_type = "basic"`
— 1/4 vCPU, 1 GiB memory, 4 GB disk [4]; the `run` role exits when the run ends, so it has no idle
tail, and `sleepAfter` is 5 m for the `ui` role.

| Meter | Measured | Workers Paid included | Used |
| --- | --- | --- | --- |
| D1 storage | 29.9 MB | 5 GB [7] | 0.6 % |
| D1 rows read | 53,930 / 24 h ≈ 1.6 M / month | 25 billion / month [7] | < 0.01 % |
| D1 rows written | 3,285 / 24 h ≈ 99 k / month | 50 million / month [7] | 0.2 % |
| Container memory | 1 GiB × 3,840 s ≈ 1.07 GiB-hours | 25 GiB-hours [3] | 4.3 % |
| Container vCPU | 0.25 × 3,840 s = 16 vCPU-minutes | 375 vCPU-minutes [3] | 4.3 % |
| Container disk | 4 GB × 3,840 s ≈ 4.3 GB-hours | 200 GB-hours [3] | 2.1 % |
| `ui` awake window | not measured | the same three container meters | — |
| Workers requests / CPU | not measured | 10 M requests, 30 M CPU-ms [1] | — |
| Durable Object duration | **not stated** for container-backed objects [6] | 400,000 GB-s [6] | — |

LLM spend, measured: **$0.875 across 33 runs in September** and **~$0.0118 per run** — two separate
readings that do not divide into each other, both carried as given. Everything metered sits under
5 % of its allotment, so the bill is the **$5/month account minimum** [1], not usage: about
**$6.3/month today** against about **$1.3/month** for §4's stack (measured), the $5 being the gap.

## 2. What binds the paid plan

**Containers is a paid-only feature.** The pricing table's Free column reads `N/A` for memory and
vCPU and is blank for disk; the allotments exist only "as part of the $5 USD per month Workers Paid
plan" [3]. There is no free container.

**The hourly RSS buffer Worker cannot run on the free plan.** A free Worker gets **10 ms of CPU per
Cron Trigger invocation** (15 min on Paid at a ≥ 1 hour interval) and **50 subrequests per
invocation** (10,000 on Paid) [2]; free D1 also caps **50 queries per Worker invocation** [8]. The
buffer polls the whole `sources` table and writes each feed's items per tick, so both counts scale
with the feed count. Cron Triggers themselves are free — 5 per account [2]; CPU and subrequests bind.

## 3. Free-plan ceilings worth knowing

| Free-plan limit | Figure | Against today |
| --- | --- | --- |
| D1 database size | 500 MB per database, 10 databases, 5 GB per account [8] | 29.9 MB — ~17× headroom |
| D1 rows per day | 5 million read, 100,000 written, resets 00:00 UTC [7] | 53,930 read, 3,285 written |
| Pages static serving | "free and unlimited" on both plans; 20,000 files/site, 25 MiB/file [11][12] | ~100 small pages |
| Pages direct upload | **no deployment-count cap is stated** on the limits, direct-upload or deployment-create pages [12][13]; only 500 git *builds*/month, which direct upload does not use | 60 deploys/month |
| Email Routing | free, **including the Workers email handler**; outbound Email Sending is Paid-only [15] | the newsletter path |
| Workers AI | 10,000 neurons/day, the same allocation on Free and Paid [14] | embeddings are off |
| KV writes | 1,000 writes, 1,000 deletes, 1,000 list requests per day, against 100,000 reads [10] | one write per vote |
| D1 in production | the FAQ offers only "the ability to **prototype and experiment** with D1 for free" [9] — neither permitted nor forbidden | a gap in the docs, not a verdict |

## 4. The cheapest alternative stack, priced

| Piece | Choice | Cost |
| --- | --- | --- |
| Pipeline, twice a day | Cloud Run Jobs — free tier 240,000 vCPU-s + 450,000 GiB-s/month, 1-minute billing minimum [17]; 2 runs/day at 1 vCPU / 1 GiB ≈ 3,900 vCPU-s and 3,900 GiB-s/month | $0 |
| Hourly feed fetch | the same job on a second schedule; Cloud Scheduler gives 3 free jobs per billing account, then $0.10/job/31 days [18] | $0 |
| Digest site + archive | Cloudflare Pages free plan — static requests free and unlimited [11] | $0 |
| Database | Cloudflare D1 free plan, inside every ceiling in §3 [7][8] | $0 |
| Vote Worker + queue | Cloudflare free plan — 100,000 requests/day [2], 1,000 KV writes/day [10] | $0 |
| Inbound newsletters | IMAP polling from the job, or Mailgun Free (1 inbound route) [25] / Resend Free (inbound counted against 3,000/month, 100/day) [26] | $0 |
| Durable backup | R2 free tier 10 GB-month [27], or Backblaze B2 "first 10GB storage is always free" [28] | $0 |
| Admin UI | run locally on demand — no hosted always-on tier | $0 |
| Total | LLM spend unchanged (§1); every row above is $0 | ~$1.3/month |

Ruled out, with the one figure that did it: **Vercel** — Hobby cron runs **once per day** minimum,
anything more frequent "will fail during deployment" [20]. **Netlify** — scheduled functions have a
**30-second execution limit** [21], below the ~64 s run. **Fly.io** — **no ongoing free allowance**;
1 GB `shared-cpu-1x` is $5.70/month [23]. **Render** — cron jobs have **no free instance type**,
plus a **$1/month minimum per cron service** [22]. **Hetzner** — no free tier, and the cheapest x86
row (CX23, €5.99/month displayed) was labelled unavailable at fetch time, leaving CPX12 at
€11.99/month [24]. **GitHub Actions** — scheduled runs "can be delayed during periods of high
loads", "some queued jobs may be dropped", and public-repo schedules **auto-disable after 60 days**
of repository inactivity [19].

## 5. Serving the reader from a container, priced

"Memory and disk usage are based on the *provisioned resources* for the instance type you select,
while CPU usage is based on *active usage* only" [3] — memory and disk therefore accrue for the
**whole awake window** whether or not anything happens, and `sleepAfter` (5 m here, 10 m by default)
is what ends it [5]. A `basic` instance kept awake by traffic for a full month, at the published
rates [3]: memory (720 − 25) GiB-hours × 3,600 × $0.0000025 ≈ **$6.26** plus disk (2,880 − 200)
GB-hours × 3,600 × $0.00000007 ≈ **$0.68** — ≈ **$7/month** before Workers requests, the container's
Durable Object and egress [3][6]. That tracks *minutes available*, not work done: a page view costs
almost no CPU but resets the sleep timer, so wake frequency — crawlers included — sets the bill and
the instance type is the only lever. Cloud Run's request-based model fits the shape better: billable
time "begins with the start of the first request and ends at the end of the last request", and "idle
instances that are not minimum instances are not charged" [17]. The `ui` role's cold-start latency
is **not measured** in this repo.

## 6. Evaluated and not done

**A dynamic archive index.** No public, unauthenticated, container-served path exists today: every
container fetch sits behind the cookie gate in `workers/app/src/router.js`, only `/login` and
`/api/vote` bypass it. The published `/index.html` is also load-bearing beyond readers — the deploy
guard fetches the live index and refuses to deploy when it cannot be read (`publish.py`), recovery
rebuilds the manifest from it, and every archived page links `href="index.html"` relatively.

**UI theme customization.** The three sanctioned colour literals are hand-derived from
tokens (`--accent` at 45 %, `--bg` at 88 %, a lightened `--accent`), so overriding `--accent` leaves
the brand glow, site-bar background and primary-button hover behind — and the colour-literal test
passes anyway, those strings being its allow-list. The Google Fonts URL is literal in four
templates, so a font-token override changes no webfont.

**Structural UI customization.** Section identity is a pydantic field name in four layers at once —
`domain/models.py` → `service_layer/digest_pipeline.py` → `domain/selection.py` →
`digest.html.j2` — and `filtered_headlines` is a `list[DigestItem]` where its siblings are
`list[DigestSection]`, so a reorderable section list is a model change, not a rename.

**Full-pass raw triage.** `sync_promotions` stamps `triaged_at` on every row it finds, whatever the
vote and the prior state, and a stamped row is immune to the pipeline's `update_states` and to
`delete_articles` — a pass over every article makes the store append-only and `cyris articles clean`
a no-op. Vote-similarity seeding is calibrated for rare votes: `max_similarity` is a maximum over
the seed list, measured at 2 downvote seeds → 8 articles suppressed, 24 seeds → 45.

## 7. Decisions and their dates

- **Stay on Cloudflare Workers Paid** (2026-09-20). The $5 minimum is the whole gap to §4's stack,
  and it buys Containers, the hourly buffer Worker (§2) and one bill instead of four vendors.
- **The digest stays static** (2026-09-20/21). Pages serves it free and unlimited [11]; serving it
  from the container prices at minutes-available (§5) and needs a path shape that does not exist.
- **Browsing history stays behind Access** (2026-09-20/21), a private tool, not a reader page.
- **Reader-facing live preferences stop at the type size** (2026-09-20/21) — the one token axis with
  a grade-D setting, a Worker injection path and a closed allowlist; a theme override has no grade.

Two tickets in the Obsidian vault (`pm/cyris/tasks/`) carry what is left:
`retire-rss-buffer-worker` and `digest-content-durable-backup`.

## Sources

All fetched 2026-09-20; only Hetzner's machine-readable feed printed a date. `[1]`–`[15]` and `[27]`
are paths under `https://developers.cloudflare.com/`: `[1]` `workers/platform/pricing/` · `[2]`
`workers/platform/limits/` · `[3]` `containers/platform/pricing/` · `[4]`
`containers/platform/limits/` · `[5]` `containers/reference/container-class/#sleepafter` · `[6]`
`durable-objects/platform/pricing/` · `[7]` `d1/platform/pricing/` · `[8]` `d1/platform/limits/` ·
`[9]` `d1/reference/faq/` · `[10]` `kv/platform/pricing/` · `[11]` `pages/functions/pricing/` ·
`[12]` `pages/platform/limits/` · `[13]` `pages/get-started/direct-upload/` · `[14]`
`workers-ai/platform/pricing/` · `[15]` `email-service/` · `[27]` `r2/pricing/`. The rest:
`[17]` <https://cloud.google.com/run/pricing> · `[18]` <https://cloud.google.com/scheduler/pricing> ·
`[19]` <https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows>
· `[20]` <https://vercel.com/docs/cron-jobs/usage-and-pricing> · `[21]`
<https://docs.netlify.com/build/functions/scheduled-functions/> · `[22]`
<https://render.com/docs/cronjobs> · `[23]` <https://fly.io/docs/about/pricing/> · `[24]`
<https://www.hetzner.com/_resources/app/data/app/live_data_prices.json> · `[25]`
<https://www.mailgun.com/pricing/> · `[26]` <https://resend.com/pricing> · `[28]`
<https://www.backblaze.com/cloud-storage/pricing>
