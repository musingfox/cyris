# Vote-Signal Utilization Analysis: Improving Article Selection from Digest Up/Downvotes

Scope: this document evaluates ways to turn the digest's up/downvote clicks into a
better article-selection signal than today's LLM score alone. Observation date for
every time-sensitive claim below is **2026-08-09** (the store's latest partition).
Everything stated as fact is recomputed directly from `agent-vault/articles/*.json`
(5,618 rows across 26 daily partitions, 2026-07-14..2026-08-09), `agent-vault/html/*.html`
(the rendered digests, for click-attribution evidence), and `agent-vault/usage.jsonl`
(49 rows), or cited to a repo `path:line`. External claims (bge-m3, Cloudflare pricing)
cite the documentation page. Anything not measured or cited is marked an assumption.

**Revision note.** This is a rewrite of the previous version of this document. That
version evaluated every option against how many labels exist *today* (5 stored rows),
which structurally favored whatever needed the fewest labels -- a hand-authored regex
-- and pushed every option that scales with a growing, more complex corpus (embedding
similarity, retrieval) into indefinite deferral on the same "not enough data" grounds.
That framing is wrong for a corpus that adds ~216 rows and multiple new recurring
template shapes every day: the question is not whether an option clears a bar set by
today's click count, but at what point on the corpus's growth curve each option starts
or stops being the right one. This version also corrects a granularity error: a
mechanism can be too broad (excluding a whole source misjudges everything else that
source publishes) or too narrow (a hand-authored regex only catches the exact wording
it was written against) independently of how many labels exist. The unit that actually
matters for "reject 今彩539 lottery draws, not 中央社財經 as a whole" is a **semantic
class** -- narrower than a source, wider than one article -- and excluding a feed is
demoted to a legitimate-but-not-current-priority option rather than a live
recommendation.

## 1. What The Votes Actually Are Today

Human triage has produced exactly **3 human clicks** to date, fanned across 5 stored
rows: one accept click fanned to 2 Hormuz-Strait articles (荷莫茲海峽再傳船隻遇襲 and
伊朗列荷莫茲全面開放條件), one reject click fanned to 2 lottery-draw articles
(今彩539第115192期　頭獎1注中獎 and 今彩539第115192期開獎) -- the same underlying click,
not two independent rejections (Section 2.2) -- and one singleton accept (TechCrunch's
"Planned Amazon data center..." story, score 80.0). All 3 clicks were located and
verified as rendered `.vote-group` elements in `agent-vault/html/2026-08-09-morning.html`.
Of 5,618 stored articles total, 1,614 carry an LLM score (871 of those score >= 70).
Only 1 of the 3 clicks (the TechCrunch accept) falls inside the population the scorer
ever touches; the other 2 clicks (4 rows) are score=None because they are 中央社即時新聞
items, which the scorer never sees (Section 2.1).

The reject click is the sharpest evidence in the whole store: it is one instance of a
periodic wire-service artifact -- 今彩539 (a government lottery number draw) reported
twice by 中央社即時新聞 財經新聞 in the same digest window, once per source URL. This is
not a topical rejection; it is a **format/junk filter** test case -- the reader is not
saying "I dislike financial news," they are saying "stop showing me routine
wire-service draw-result boilerplate." No option below may treat this negative class as
a signal about topical *preference*, and no option below may treat a source-level
suppression of 中央社財經 as equivalent to honoring this click -- doing either would
train every option to associate "finance" or "中央社" with rejection, which is not what
actually happened here (Section 6's granularity test makes this precise).

### The vote arrival rate cannot be measured

How often new clicks arrive matters directly to how long reaching any of the
class-level mechanisms below would take, but that arrival rate is structurally
unmeasurable from today's data, not merely unmeasured: all 5 `triaged_at` stamps
collapse to the exact same instant, `2026-08-09T05:51:08.904839Z`, because
`promotions.py:97` calls `store.update_triage_timestamp([a.url for a in found],
datetime.now(UTC))` -- it stamps every vote synced in one batch with the sync-time
clock, not the vote's own time. The vote's real time already exists on the wire --
`promotions.py:17-23` parses `PromotedArticle.ts` and `.digest_date` out of every
incoming payload -- but the value is parsed and then discarded; it is never written to
the store. Until that is fixed, no option in this document may quote a
labels-per-week/day/month figure as measured data; nothing below does.

## 2. Two Structural Mismatches Every Option Must Answer

### 2.1 The news/scorer mismatch

`run_digest.py:123` skips scoring for any article carrying `"news"` in
`source_tags` (`if "news" in a.source_tags: continue`), and `run_digest.py:121`
additionally skips the FAN tier -- the scorer never sees the population most of our
votes come from. Concretely, 2 of the 3 clicks (4 of the 5 rows) are 中央社 news items
that the scorer never touches; only the TechCrunch accept overlaps the scored set at
all (Section 1). Every option in Section 6 states explicitly how it handles this --
most cannot use the news-tagged clicks for anything, and the rule-based filter and the
news-scoring option both target that population directly.

### 2.2 Cluster-level vote attribution

Cluster-level vote attribution is a live risk, not a hypothetical, and it is the reason
Section 1's click count differs from the store's row count. The promote button macro
emits one `vote-group` per digest item whose `data-urls` attribute is a JSON list
(`digest.html.j2:2`); a news cluster shares that one vote-group across every article in
it (`digest.html.j2:871`: "a news cluster is one vote-group over every article in
it"), and a click fans out to a separate `POST /promote` per URL with the *same* vote
value and the same `digest_date` (`digest.html.j2:887,893`). Our two 中央社 "accepted"
rows -- 荷莫茲海峽再傳船隻遇襲 and 伊朗列荷莫茲全面開放條件, both Hormuz-Strait stories,
stamped at the identical instant -- are exactly this: one human click credited
identically to two separate stored articles, not two independent judgments. The same
fan-out produced the lottery evidence: the two rows "今彩539第115192期　頭獎1注中獎" and
"今彩539第115192期開獎" are not two independent reject events but **one reject click**
fanned to two article URLs by the same `digest.html.j2:887,893` `Promise.all` POST.
Because `StoredArticle` (`models.py:127-147`) carries no cluster id, this cannot be
proven after the fact from the store alone, so the attribution rule adopted here is:
**treat cluster-fanned votes as N correlated rows sharing one underlying click, never
as N independent labels**, until per-article voting (opt-signal-capture, Section 6)
replaces cluster-level voting.

## 3. Why The Existing PreferenceProfile Path Isn't Enough

As of the observation date **2026-08-09**, `agent-vault/learning/profile.json` has
**never been generated** -- every scoring call to date has run with
`preference_profile=None`, so `prompts.py:244-245` has always returned the bare
`SCORING_SYSTEM` string with no preference injection at all. This is not because the
path is disabled: `run_digest.py:35` defaults `enable_learning: bool = True`, and the
gate that has been blocking it, `profile.py:88`'s `accepted_count < 3` check, sits
against today's `actual_accepted = 3` (a stored-row count, satisfied here only because
the two Hormuz rows happen to sum with the Amazon row to 3 -- not because 3 independent
accept clicks exist; see Section 2.2) -- it would just barely pass if `cyris learn`
were run right now. `triage_feedback.py:13` additionally requires `min_triaged=3` total
triaged rows within `learning.py:25`'s 14-day collection window; both are also
satisfied today.

That the gate would pass is exactly why "not enough labels" is the wrong verdict on
this path. Even after the accepted_count>=3 gate passes, `generate_profile_from_triage`
(`profile.py:72-115`) feeds raw titles into a single unvalidated LLM call with no
held-out evaluation step, so the resulting `prompt_injection` text cannot be checked
against future votes before it starts silently steering every scoring call; nothing in
`profile.py` ever re-generates or invalidates a stale profile, so one contrastive read
of five titles -- most of which are the news items this document already disqualifies
as a topical preference signal (Section 1) -- would keep steering scoring indefinitely
with no drift check. Turning this path on today would inject a profile built almost
entirely from "reject 今彩539-style wire copy," misread by the LLM as a themed
preference, into every single scoring call. The structural problem is evaluation, not
volume.

## 4. Cost Baseline

Recomputing directly from `agent-vault/usage.jsonl`'s 49 rows --
`input_tokens x $1.50/1M + output_tokens x $7.50/1M`, the actual gemini-3.6-flash
rates configured in `cyris.toml:17` -- gives a baseline of **$0.073/run**
(~$4.38/month at 60 runs/month), matching `docs/cloud-migration.md:100-101`'s
independently-measured figure.

The `usage.jsonl` file's own `estimated_cost_usd` field must never be used as that
baseline: it averages $0.1461/run, almost exactly **2x too high**, because
`models.py:78-80` hardcodes Sonnet pricing (`input_tokens * 3 + output_tokens * 15`)
while the configured provider has been gemini since `cyris.toml:17` -- a stale
constant, not a measurement, and every cost comparison below uses the recomputed
$0.073/run baseline, never the logged field.

## 5. The 5,613 Pipeline-Verdict Rows

5,613 of the store's 5,618 rows (5,618 minus the 5 human-triaged rows) are the
pipeline's own accept/reject verdicts, not human judgments. `triage_feedback.py:38-39`
states why they are excluded from `collect_triage_feedback` today: "Only
triaged_at-stamped articles are human labels; the rest are the pipeline's own verdicts,
and learning from those is a self-reinforcing loop." The ruling in this document is
that these 5,613 rows **may not** be used as ground-truth preference labels by any
option below. Where an option chooses to fold them in anyway -- as weak/pseudo-labels
rather than ground truth, a real and sometimes reasonable design choice -- it must name
that self-reinforcement risk at its own point of use; opt-embedding-rerank and
opt-rag-exemplar in Section 6 do.

The lottery evidence makes this rule concrete rather than abstract: of the 50 rows
matching the exact draw-announcement template mined from the single reject click, 48
are pipeline-**accepted** and only the 2 rows behind the human's own click are
rejected. The pipeline's own verdicts are actively wrong on exactly the class the human
click is about -- so no option below may treat pipeline agreement on this class as
corroboration.

## 6. Options Considered, Evaluated On The Growth Curve

Each option below states a verdict, its granularity (does it operate at the source
level, the semantic-class level, or the exact-template level -- and does that
over-generalize to "reject 中央社財經" the way excluding a feed would?), its marginal
cost per newly-emerged cluster (does the option's authorship cost grow with the corpus,
or stay flat?), a crossover (the point -- in rows, in clusters, or in labels -- at
which the option's cost or capability profile changes), how it bootstraps from today's
3 clicks, an incremental cost against the $0.073 baseline, how it handles the
news/scorer mismatch, and whether it leans on the excluded pipeline verdicts.

### Title-to-Prompt Injection (Preference-Profile Text)

**Verdict: DEFER.** Granularity: source (a single global text blob, not a class).

Activating the already-built `PreferenceProfile` path (Section 3) for real: let
`generate_profile_from_triage` summarize accepted vs. rejected titles into a short
`prompt_injection` string and have `build_scoring_system_prompt` (`prompts.py:235-247`)
fold it into every scoring call's system prompt. A single blob applied to every call
cannot say "reject only the 今彩539-style draw-boilerplate format from 中央社財經"
without collapsing into "downweight anything financial/中央社" -- exactly the
over-generalization Section 3 already warns this path risks.

*Crossover.* `profile.py:88` requires `accepted_count >= 3`; assuming (an assumption --
the code only checks the accepted side) a symmetric bar for a genuinely contrastive
read gives 3 accepted + 3 rejected = 6 in-scope clicks needed, where in-scope means
landing in the scored, non-news population (Section 2.1). Today only 1 of the 3 clicks
does. Until opt-signal-capture's per-article attribution lets that in-scope rate itself
be tracked over time, growth in total clicks does not predictably translate into growth
in in-scope clicks -- this option needs *more* labels before it has anything to work
with, unlike the options below whose bottleneck is not label count.

*News mismatch.* Cannot use the 2 news-heavy clicks (4 rows) at all -- only the 1
TechCrunch overlap is usable signal today.

*Pipeline verdicts.* Not used -- `collect_triage_feedback` (`triage_feedback.py:40-57`)
already filters to `triaged_at`-stamped rows only.

*Cost.* Negligible: ~100 extra input tokens across an assumed 5 scoring calls/run at
gemini's $1.50/1M input rate is $0.00075/run, about 1% of the $0.073 baseline.

*Architecture.* No new IO boundary -- reuses the existing `LLMClient` Protocol via
`src/cyris/service_layer/prompts.py`; no `ArticleRepository` persistence touched.

### Embedding-Based Similarity Reranking (Workers AI bge-m3 + Vectorize)

**Verdict: DEFER, pending the pre-registered validation (Section 7).** Granularity:
semantic-class -- the one mechanism whose unit of judgment matches the constraint
directly.

Embed accepted/rejected articles with Cloudflare Workers AI's multilingual `bge-m3`
model (1024-dim, $0.012/1M input tokens, 60k context -- turn 1's dimensionality figure
was wrong, corrected against huggingface.co/BAAI/bge-m3), compute a similarity score
between each newly scored article and the accepted-label centroid, and blend that into
ranking. A similarity centroid built from the 193-row boilerplate class -- not the
1,252-row 財經 source -- separates 今彩539/大樂透/威力彩 draw-announcements from
ordinary 中央社財經 reporting by embedding distance, not by source label.

*Crossover -- capability (independent of scale).* At any corpus size, embedding
similarity is the only mechanism other than an ever-growing hand-authored regex library
that can express the 193-row semantic class without conflating it with the 1,252-row
source. Its marginal cost per newly-emerged cluster is **zero**: once the
class-preference entity and a similarity index exist, a new cluster needs no new
authored rule -- its members simply fall near an existing or new centroid in vector
space. This is the structural answer to the rule-based filter's unbounded per-cluster
authorship cost (see that option's crossover, below).

*Crossover -- cost (does depend on scale).* Vectorize Paid includes 10,000,000 stored
dims -> 10,000,000/1024 = 9,765 vectors of headroom; the corpus (5,618 rows) has 4,147
rows of headroom left, which at the measured full-day partition spread of 116-336
rows/day (mean 216) is a band of 12-36 days -- not a point estimate -- before the free
inclusion is exhausted. A 1-year corpus (5,618 + 216.1*365 ~= 84,486 rows) needs 86.5M
dims; on a total-dims billing basis that's 86.5M * $0.05/100M = $0.043/mo (an
overage-only basis would be $0.038/mo) -- the meter is cheap even past crossover.

*Bootstrap.* Not gated on accumulating more human clicks: its bootstrap gate is the
pre-registered validation check (Section 7) run against the corpus that already
exists, plus the class_preference_entity one-way door (Section 8) deciding where a
class-level judgment is stored once validated.

*News mismatch.* Runs over the same scored, non-news population as the LLM scorer, so
it inherits the identical news blind spot as prompt injection -- a separate news-side
index is a distinct future project.

*Pipeline verdicts.* Bootstrapping the accepted-centroid from score>=70 pipeline rows
folds the pipeline's own bias back into the reranker -- and the lottery evidence is the
proof it would be actively wrong here: 48 of the 50 lottery-format rows are
pipeline-accepted while the human click rejected exactly that class
(`triage_feedback.py:38-39`). Any centroid must be validated against the held-out
human-labeled set before any pipeline-verdict augmentation is trusted.

*Cost.* At an assumed 150 articles/run and ~250 tokens/article: $0.00045/run, well
under 1% of baseline (embedding cost is not the reason to defer -- see crossover
above).

*Architecture.* New adapter `src/cyris/adapters/embeddings.py`, no local GPU, no
resident model. No `ArticleRepository` persistence touched.

### Retrieval-Augmented Generation (RAG) Exemplar Injection

**Verdict: REJECT.** Granularity: semantic-class (attempted).

Retrieve the k most similar past labeled articles per batch and inject them as few-shot
exemplars at call time. Retrieved exemplars are drawn by similarity to the same
193-row class, not by source, so it does not conflate 樂透 rejection with 中央社財經 as
a whole -- but it inherits every embedding-index cost assigned to reranking above while
adding a second bias surface (which exemplars get retrieved).

*Crossover.* This bias surface does not shrink as the corpus grows -- more rows means
more retrieval candidates, not fewer failure modes. There is no corpus size at which
this option becomes preferable to plain reranking, so it is rejected rather than
deferred; its marginal cost per new cluster is a handful of diverse exemplars, unlike
reranking's zero.

*News mismatch.* Shares the identical news exclusion as the two options above.

*Pipeline verdicts.* Filling the retrieval corpus with pipeline-scored articles means
the LLM retrieves its own past verdicts as "evidence" for the next call -- the same
self-reinforcing-loop risk named at `triage_feedback.py:38-39`, compounded because the
exemplars sit directly inside the prompt the LLM reads; the 48-pipeline-accepted lottery
contradiction above applies here too.

*Cost.* ~$0.0056/run (5 exemplars x ~150 tokens x 5 calls/run at gemini's input rate),
about 7.7% of baseline in marginal tokens alone, before the shared embeddings/Vectorize
build cost.

*Architecture.* New adapter `src/cyris/adapters/vectorstore.py`. No `ArticleRepository`
persistence touched.

### Rule-Based Negative-Class Filter (Regex on Wire Boilerplate Titles)

**Verdict: ADOPT (as an immediate stopgap, not the long-term mechanism).**
Granularity: exact-template.

A deterministic, LLM-free filter that pattern-matches the periodic wire-service
draw-announcement title format (e.g. `第\d+期.*(開獎|中獎)` from 中央社財經 sources) and
drops matching articles before they ever reach scoring or the digest, the same way the
FAN tier and news skip already short-circuit scoring today (`run_digest.py:121-124`).
The regex is scoped to the exact template, not to 中央社財經 as a source -- it does not
touch the other 1,202 non-lottery articles from that source -- but it only covers 50 of
the 193-row semantic class (25.9%), so it is too narrow on the other side of the
granularity spectrum.

*Crossover -- and the retraction this version makes.* The narrow lottery regex already
spans 3 brands (今彩539:36, 大樂透:7, 威力彩:7) from a SINGLE reject click's two
title-rows -- one class already needed multiple template variants. Corpus-wide,
distinct recurring templates (a title-shape proxy, not a direct cluster measurement:
digits normalized to `#`, first 10 characters, count>=3) grew from 0 (at the first
90-row partition) to 71 (at the full 5,618-row corpus): (71-0)/(5618-90) = 1 new
template per ~77.9 rows, i.e. **~2.79 new template-shaped clusters/day** at the
corpus's measured 216.1 rows/day. Under this option every one of those needs its own
hand-authored rule, so the authorship burden is monotonically increasing and never
converges, even though each rule costs $0 in tokens. This retracts turn 1's reasoning
that "replication across at least two independent reject events" was already met and
therefore sufficient to treat the pattern as a rule: there was one reject click behind
those two title-rows, not two independent reject events, so that bar was never actually
met, and even where the underlying evidence is solid (the 193-row semantic class,
verified in Section 7), the mechanism's per-cluster authorship cost is what disqualifies
it as the long-term answer, not its label count.

*News mismatch.* Targets 中央社財經 wire-service titles directly -- exactly the
population the scorer skips -- so it is the only option, alongside opt-score-news, that
can act on the 2 news-heavy clicks (4 rows) at all.

*Pipeline verdicts.* Not used -- the rule is mined from the reject click's own titles,
not from any of the 5,613 excluded pipeline-verdict rows.

*Cost.* $0 marginal -- a title regex adds no LLM calls, and by dropping junk before
scoring it can only reduce token spend against the $0.073 baseline.

*Architecture.* Pure function, no new IO boundary; belongs beside the existing
tier-skip logic, anchored here at `src/cyris/domain/triage.py`. No `ArticleRepository`
persistence touched.

### Signal-Capture Fix: Persist Vote Timestamp and Per-Article Attribution

**Verdict: ADOPT.** Granularity: n/a -- this is infrastructure, not a preference
mechanism.

Change `promotions.py` to stop discarding `PromotedArticle.ts` and `.digest_date`
(currently parsed at `promotions.py:17-23` and then never read again) and persist them
onto the label record, and change the vote-group click (`digest.html.j2:871-893`) to
attribute one vote per underlying article rather than fanning one click's vote out to
every URL in a cluster (Section 2.2). This makes no topical claim about 樂透 or 中央社
財經 at all -- every option above that DOES have to answer the granularity question
depends on this one first, because a class-level label needs to know which single
article a vote was actually about.

*Crossover.* None -- this is prerequisite infrastructure whose benefit begins accruing
the moment it ships. Every day it does not ship, roughly 216.1 more rows accumulate
under the old cluster-fanned, sync-batch-clock scheme, whose true click-level
attribution cannot be reconstructed after the fact.

*News mismatch.* Instrumentation, not a scorer change -- it captures digest_date/ts and
per-article identity regardless of whether the article is news, preserving the mismatch
as recoverable raw data going forward instead of silently losing it.

*Pipeline verdicts.* Not used -- this option only changes how clicks are captured, not
what counts as a label.

*Cost.* $0 marginal -- a store/promotions schema change with no new LLM calls.

*Architecture.* Extends `src/cyris/adapters/promotions.py`. This is the one option that
does touch persistence: `promotions.py:97` already calls
`store.update_triage_timestamp(...)` synchronously against `ArticleRepository`
(`ports.py:37`), and `run_digest.py:61` already wraps the whole `sync_promotions` call
in `asyncio.to_thread(...)` precisely because `ArticleRepository` is a SYNCHRONOUS
Protocol -- any extension must keep calling it the same synchronous way, not introduce
an `await` into the store call itself.

### Score News-Tagged Articles (delete the `"news"` skip)

**Verdict: DEFER.** Granularity: n/a -- a population-scope fix, not a preference
mechanism, but it is a precondition for the scored-population options above to ever
touch a lottery-class label directly.

Deleting `run_digest.py:123`'s `if "news" in a.source_tags: continue` would move
中央社即時新聞 (and other "news"-tagged) articles into the scorer for the first time.

*Crossover -- when the cost cap starts mattering.*
`cfg.app.digest.max_articles_per_digest = 400` (`cyris.toml:20`; default 200,
`config.py:107`) is sliced BEFORE the news filter (`run_digest.py:117`), against
today's pool of ~114.7 rows/run (216.1 rows/day / 1.88 runs/day) -- the cap is
non-binding today. It would only start binding once rows/day grows to roughly
400*1.88 = 752/day, about 3.5x today's rate; until then the cost delta below is
unaffected by the cap.

*Cost -- reported as a band, not a point estimate.* News-tagged rows average
151.3/day; at `BATCH_SIZE = 20` (`scoring.py:13`) that is 7.57 batches/day. One real
20-article scoring prompt built via `build_scoring_prompt(snippet_length=1000)` is
7,759 chars/batch (6,630 prompt + 1,129 system). At a CJK band of 2.0-3.0
chars/token and gemini's $1.50/1M in + $7.50/1M out (`cyris.toml:17`): **$0.0430 to
$0.0576/day** against the recomputed $0.1376/day baseline (0.073/run x 1.88 runs/day)
-- a **+31% to +42%** delta. The chars-per-token uncertainty is why this is a band, not
a single number.

*News mismatch.* This IS the fix, for the scored population -- it does not touch the
2 news-heavy clicks' unscored status by itself; it makes future news-tagged articles
scorable at all.

*Pipeline verdicts.* Not used.

*Architecture.* `src/cyris/service_layer/run_digest.py`. No `ArticleRepository`
persistence touched.

### Exclude a Feed/Source on Vote Evidence

**Verdict: DEFER -- not the current priority.** Granularity: source -- this IS the
source-level cell of the granularity measurement below.

Excluding 中央社即時新聞 財經新聞 entirely to remove 50 lottery rows would suppress all
1,252 of its articles, misclassifying 1,202 non-lottery articles (784 already
pipeline-accepted) as unwanted. Whether to exclude a feed based on accumulated vote
evidence is a legitimate question in general -- and this document does not rule it out
forever -- but conflating "reject 今彩539-style boilerplate" with "reject 中央社財經" is
precisely the over-generalization ruled out here, and it is wrong at every corpus size
measured (today lottery is 50 of 1,252 財經 rows, and new non-lottery financial content
keeps arriving at that same source at every partition), not just today's. It is not
part of the bootstrap sequence below; it stays a legitimate option to raise once a
feed's rejection rate is itself measurable, which needs opt-signal-capture's
per-article attribution first.

*Cost.* $0 marginal to write -- but cheap-to-write is not the same as low-risk; the
cost that matters is the collateral suppression measured above.

*Architecture.* `sources.yaml`. No `ArticleRepository` persistence touched.

### 6.1 The Granularity Test, With Measured Collateral

The 樂透 vs 中央社財經 judgment, measured at three units:

| Level | Unit | Size | States | Verdict |
|---|---|---|---|---|
| Source | 中央社即時新聞 財經新聞 (the whole source) | 1,252 suppressed | 1,202 non-lottery collateral, 784 of those pipeline-accepted | **too broad** |
| Semantic class | routine numeric wire boilerplate (draw announcements, TAIEX open/close, TWD moves, institutional buy/sell, futures moves), all from 中央社即時新聞 財經新聞 | 193 | 175 accepted / 18 rejected | **correct granularity** |
| Exact template | the `第N期(開獎\|中獎)` regex, generalized from the single reject click's two title-rows | 50 | spans 3 brands (今彩539:36, 大樂透:7, 威力彩:7) | **too narrow** |

The middle row is the constraint this document holds every option to: a mechanism must
be able to express "routine wire boilerplate," not "中央社財經" (too broad, wrong by
1,202 rows) and not "the exact wording of one draw format" (too narrow, misses 74.1% of
the class it should catch).

### 6.2 Bootstrap Sequence: From Today's 3 Clicks To A Class-Level Mechanism

**The label arrival rate cannot be assumed even here.** promotions.py:97 stamps every
synced vote with `datetime.now(UTC)` at sync time rather than the wire payload's real
vote time, which `promotions.py:17-23` parses into `PromotedArticle.ts` and
`.digest_date` and then discards. Today's 5 rows / 3 clicks give zero information about
cadence, and no step below implies a labels-per-week/month figure until Step 1 closes
that capture gap.

1. **Fix signal capture** (serves opt-signal-capture). Stop discarding
   `PromotedArticle.ts`/`.digest_date`; move the vote-group click from cluster fan-out
   to per-article attribution. Unlocks: every future click becomes an individually
   attributable, correctly-timestamped record. Measurable when done: a new promote
   click produces a record whose timestamp matches the wire payload's parsed
   `PromotedArticle.ts`, not the sync-time `datetime.now(UTC)`. Not gated on anything
   prior.
2. **Ship the rule-based filter now** (serves opt-rule-filter), in parallel with Step
   1 -- it needs no infrastructure and no further human judgment, only the
   already-measured 193-row semantic class (Section 7). Unlocks: the class the vote is
   actually about stops reaching the digest today. Measurable when done: the 50
   matched titles (and any newly-emerged templates matching the same class) no longer
   appear in a produced digest. Not gated on anything prior.
3. **Accumulate in-scope clicks and (re-)generate the preference profile** (serves
   opt-prompt-injection), gated on Step 1: only once signal capture lands can
   individually-attributed, correctly-timestamped clicks be counted toward the
   in-scope threshold at all. Unlocks: a cheap, LLM-native preference signal for the
   scored population. Measurable when done: `agent-vault/learning/profile.json` exists,
   was generated from clicks stamped under the new per-article scheme, and is
   regenerated -- not silently reused -- as new in-scope clicks arrive.
4. **Run the pre-registered embedding validation, then open the class-level door**
   (serves opt-embedding-rerank), gated on the `class_preference_entity` one-way door
   decision (Section 8): run the bge-m3 validation (Section 7) against the corpus that
   already exists; if it clears the pre-stated threshold, decide where a class-level
   judgment is stored and stand up reranking. Unlocks: a mechanism whose marginal cost
   per new cluster is zero, which is what the corpus's ~2.79 new template-shaped
   clusters/day needs once hand-authored rules stop keeping pace. Measurable when done:
   the validation's measured precision/recall against the held-out split is recorded
   against the pre-stated floor, and the schema decision is made either way.

This sequence reaches a semantic-class-granularity mechanism (Step 4) without ever
assuming a labels-per-period figure, and without treating today's click count as the
reason to defer any of it.

## 7. Checks: Executed vs. Pre-Registered

Three checks below are **executed** -- every input is local, read-only data already in
the repo (`agent-vault/articles/*.json`), so they were run now, not proposed:

1. **Semantic-class prevalence vs. narrow-template regex.** Threshold pre-stated: if
   the narrow template regex covers three-quarters or more of the measured semantic
   class, exact-template regex is sufficient on its own. Measured: 50 of 193 (25.9%).
   Decision: discharged -- well under three-quarters, so exact-template regex is not
   sufficient alone; retained only as the immediate stopgap (opt-rule-filter).
2. **Source-exclusion collateral.** Threshold pre-stated: if excluding the source
   removes materially more non-lottery, pipeline-accepted rows than the 50 lottery rows
   it targets, source-level exclusion is disqualified as too broad. Measured: 1,252
   suppressed total, 1,202 non-lottery, 784 of those pipeline-accepted. Decision:
   discharged -- source-level exclusion is disqualified as the current action.
3. **Template-count growth across the corpus.** Threshold pre-stated: growth (no
   plateau) means hand-authored regexes accumulate rather than saturate. Measured: 0
   templates at the first 90-row partition, 71 at the full 5,618-row corpus, ~2.79
   new templates/day at today's pace. Decision: discharged -- no plateau observed
   across 26 partitions; a per-template rule does not converge.

One check is **pre-registered, not executed**, because at least one of its inputs is a
network call, and running it now would prematurely open the `embeddings_vectorize`
one-way door before this document has validated whether that is worth doing:

- **bge-m3 recall/precision validation on the 193-row boilerplate class.** Inputs:
  `agent-vault/articles/*.json` plus the Cloudflare Workers AI bge-m3 endpoint.
  Threshold, pre-stated before running: embed the 193-row boilerplate class plus a
  matched sample of ~1,100 non-boilerplate 中央社財經 rows; using the two title-rows
  produced by the single reject click as a nearest-neighbour ground-truth seed, both
  precision and recall of the boilerplate class on a held-out split must clear a floor
  fixed before running, not derived from the run itself. Decision rule: if both clear
  the floor, open `embeddings_vectorize` + `class_preference_entity` and build
  opt-embedding-rerank; otherwise keep opt-rule-filter as the front-line stopgap and
  re-run at the next order-of-magnitude corpus checkpoint. Estimated validation cost:
  ~1,300 rows x ~250 tokens x $0.012/1M input tokens ~= $0.004 -- trivial relative to
  the value of the answer.

## 8. One-Way Decisions For The Human

Four decisions are surfaced for the human here, not settled by this document -- each is
one-way: cheap to open, expensive or impossible to close back up once other work builds
on top of it.

- **Where a class-level preference lives (`class_preference_entity`, new this
  revision).** Constraint 2 demands a preference unit narrower than a source and wider
  than one article. `StoredArticle` (`models.py:127-147`: url, original_id, title,
  content, author, published_at, source_name, source_tier, source_tags, state,
  first_seen_at, digest_date, rejection_reason, score, language, scored_at,
  triaged_at, exported_at) has no field to hold it -- no cluster/class/concept id
  anywhere in the schema. Opening this door means choosing whether class-level
  preference is inspectable and editable by a human (a cluster id, a concept-store
  row) or only implicit in vector-space geometry with no persisted identity. Every one
  of opt-embedding-rerank, opt-rag-exemplar and opt-rule-filter implies a different
  answer; picking a mechanism before this decision is picking this door blind.
- **Vote semantics.** Redesigning promote payload handling to persist `ts`/
  `digest_date` (`promotions.py:17-23`) and to move from cluster-fanned votes to
  genuinely per-article ones opens the door to trustworthy arrival-rate and
  cluster-attribution measurement for the first time. It also means every future
  label's meaning changes shape at once: votes recorded under the current
  cluster-fanned, sync-batch-clock semantics cannot be reinterpreted after the fact.
- **Whether embeddings/Vectorize enter the stack.** Adopting Workers AI embeddings
  (bge-m3, 1024-dim, $0.012/1M input tokens) plus Vectorize opens a similarity/RAG
  surface no current adapter provides, and is the only mechanism this document finds
  that meets constraint 2's granularity requirement at any corpus size. It also locks
  in a model version and a re-index migration path the moment any article is embedded.
  Now quantified: the Vectorize Paid 10M-stored-dim inclusion holds 9,765 vectors at
  1024 dims and the corpus crosses it in a 12-36-day band at the measured daily
  spread; past that, ~$0.043/mo for a 1-year corpus (total-dims basis) -- the meter is
  cheap, but the door still locks in a model+index choice.
- **Whether the store grows a dedicated label record.** Giving the store a label
  record distinct from `state`+`triaged_at` opens room for multi-valued/weighted
  feedback (cluster-fan weight, vote source, real vote time, class id) today's
  two-field scheme cannot express. It also means every reader of `StoredArticle`
  (`models.py:127-147`) and every existing consumer of `triaged_at` gains a second,
  must-stay-in-sync source of truth about the same event.

These are one-way doors in the sense that matters here: later turns will build atop
whichever shape is chosen, and reversing the choice after votes have accumulated under
it is not a config flip.

## Recommendation

Ship opt-signal-capture -- stop discarding `PromotedArticle.ts`/`.digest_date` in
`promotions.py` and move the vote-group click to per-article attribution -- alongside
opt-rule-filter (Section 6.2's Step 2, which has no dependency on Step 1), before
building any of the labeled or embedding-based options. This is traceable to
opt-signal-capture's ADOPT verdict. The corpus adds ~216.1 rows/day and ~2.79 new
template-shaped clusters/day; every day without per-article attribution and real vote
timestamps, that growth accumulates under a cluster-fanned, sync-batch-clock scheme
whose true click-level attribution cannot be reconstructed after the fact -- this is
what makes every later option's own bootstrap step (Section 6.2) unable to state a
timeline, not today's click count. Once signal capture lands, Step 3 (prompt injection)
and Step 4 (the pre-registered embedding validation, then reranking) become the
sequence that reaches a genuine semantic-class mechanism -- the granularity this
document's whole analysis says the problem actually needs.

```gate-manifest
{
  "retain_rewrite_map": {
    "retained": {
      "1": {
        "corrections": [
          "H1: turn 1 counted 5 stored rows as 5 independent human labels (\"3 accepted and 2 rejected\"); the correct unit is the human click, not the row -- there are 3 human clicks behind those 5 rows (one accept click fanned to 2 Hormuz-Strait rows, one reject click fanned to 2 lottery rows, one singleton accept), verified against the rendered vote-groups in agent-vault/html/2026-08-09-morning.html."
        ]
      },
      "2.1": {
        "corrections": [
          "H3: removed the earlier percentage claim about the share of votes landing on unscored news (its denominator was 5, too small to support a percentage); restated using raw counts -- 2 of the 3 clicks (4 of the 5 rows) are 中央社 news items the scorer never sees."
        ]
      },
      "2.2": {
        "corrections": []
      },
      "3": {
        "corrections": []
      },
      "4": {
        "corrections": []
      },
      "5": {
        "corrections": []
      }
    },
    "rewritten": {
      "6": {},
      "7": {},
      "8": {},
      "recommendation": {}
    }
  },
  "label_accounting": {
    "rows": 5,
    "human_clicks": 3,
    "fanout_cause": "digest.html.j2's promote-button macro emits one .vote-group per digest item whose data-urls attribute is a JSON list (digest.html.j2:2); a click fans out to one POST /promote per URL via Promise.all (digest.html.j2:887,893), so one click can produce multiple stored-article rows with identical triaged_at stamps.",
    "evidence": "agent-vault/html/2026-08-09-morning.html",
    "click_groups": [
      {
        "vote": "accepted",
        "rows": 2,
        "titles": [
          "荷莫茲海峽再傳船隻遇襲　阿聯控伊朗攻擊國營油輪",
          "伊朗列荷莫茲全面開放條件　要美同意終戰並撤軍"
        ],
        "urls": [
          "https://www.cna.com.tw/news/aopl/202608080186.aspx",
          "https://www.cna.com.tw/news/aopl/202608090010.aspx"
        ]
      },
      {
        "vote": "rejected",
        "rows": 2,
        "titles": [
          "今彩539第115192期　頭獎1注中獎",
          "今彩539第115192期開獎"
        ],
        "urls": [
          "https://www.cna.com.tw/news/ahel/202608080203.aspx",
          "https://www.cna.com.tw/news/ahel/202608080193.aspx"
        ]
      },
      {
        "vote": "accepted",
        "rows": 1,
        "titles": [
          "Planned Amazon data center could become the biggest climate polluter in the U.S."
        ],
        "urls": [
          "https://techcrunch.com/2026/08/08/planned-amazon-data-center-could-become-the-biggest-climate-polluter-in-the-u-s/"
        ]
      }
    ]
  },
  "acquisition_paths": [
    {
      "anchor": "promotions.py:97",
      "human_action": "clicks up/down on a digest HTML vote-group; a news-cluster group fans the same click out to every article URL in the cluster (digest.html.j2:887,893)",
      "labels_per_human_action": "N (cluster size)",
      "fanout_factor": "cluster size (1 for a singleton item, up to the cluster's article count otherwise)",
      "attribution": "cluster-fanned",
      "acquisition_cost": "open the digest HTML/email and click a vote button; no login, no separate tool",
      "timestamp_fidelity": "sync-batch clock"
    },
    {
      "anchor": "triage_server.py:162",
      "human_action": "clicks accept on one card at a time in the triage web UI",
      "labels_per_human_action": 1,
      "fanout_factor": 1,
      "attribution": "per-article",
      "acquisition_cost": "run/open the triage-ui server and act on one card at a time",
      "timestamp_fidelity": "real vote time (datetime.now(UTC) is called synchronously inside the same request the click triggers -- no sync-interval skew)"
    },
    {
      "anchor": "triage_server.py:206",
      "human_action": "clicks reject on one card at a time in the triage web UI",
      "labels_per_human_action": 1,
      "fanout_factor": 1,
      "attribution": "per-article",
      "acquisition_cost": "run/open the triage-ui server and act on one card at a time",
      "timestamp_fidelity": "real vote time (datetime.now(UTC) is called synchronously inside the same request the click triggers -- no sync-interval skew)"
    },
    {
      "anchor": "cli.py:455",
      "human_action": "operator runs `cyris articles accept <url...>` from a shell",
      "labels_per_human_action": "N (one per URL argument)",
      "fanout_factor": 1,
      "attribution": "per-article",
      "acquisition_cost": "requires shell access and copying the exact article URL(s) -- highest-friction acquisition path of the five",
      "timestamp_fidelity": "real vote time (stamped synchronously with the command)"
    },
    {
      "anchor": "cli.py:496",
      "human_action": "operator runs `cyris articles reject <url...>` from a shell",
      "labels_per_human_action": "N (one per URL argument)",
      "fanout_factor": 1,
      "attribution": "per-article",
      "acquisition_cost": "requires shell access and copying the exact article URL(s) -- highest-friction acquisition path of the five",
      "timestamp_fidelity": "real vote time (stamped synchronously with the command)"
    }
  ],
  "corpus_growth": {
    "rows_total": 5618,
    "partitions": 26,
    "span": "2026-07-14..2026-08-09",
    "rows_per_day": 216.1,
    "news_tagged_rows": 3934,
    "news_per_day": 151.3,
    "scored_rows": 1614,
    "scored_per_day": 62.1,
    "runs_per_day": 1.88,
    "distinct_recurring_templates": {
      "proxy": "PROXY for semantic-cluster count, not a measurement of it: title with digits normalized to '#', first 10 characters, counted only when the signature recurs >=3 times within the cumulative partitions scanned so far",
      "at_90_rows": 0,
      "at_5618_rows": 71,
      "shape": "grows roughly linearly with corpus size -- 0 to 71 distinct recurring templates across 26 partitions, no plateau observed"
    }
  },
  "granularity_test": {
    "source_level": {
      "unit": "中央社即時新聞 財經新聞 (the whole source)",
      "suppressed": 1252,
      "collateral_non_lottery": 1202,
      "collateral_pipeline_accepted": 784,
      "verdict": "too broad"
    },
    "semantic_class_level": {
      "unit": "routine numeric wire boilerplate (lottery draw announcements, TAIEX open/close, TWD moves, institutional buy/sell tallies, futures moves)",
      "size": 193,
      "all_from": "中央社即時新聞 財經新聞",
      "states": {
        "accepted": 175,
        "rejected": 18
      },
      "verdict": "correct granularity"
    },
    "template_level": {
      "unit": "the exact 第N期(開獎|中獎) regex, generalized from the single reject click's two title-rows",
      "size": 50,
      "coverage_of_semantic_class": 0.259,
      "brands_covered": {
        "今彩539": 36,
        "大樂透": 7,
        "威力彩": 7
      },
      "verdict": "too narrow"
    }
  },
  "options": [
    {
      "id": "opt-prompt-injection",
      "name": "Title-to-Prompt Injection (Preference-Profile Text)",
      "verdict": "defer",
      "granularity": "source",
      "over_generalizes_to_source": true,
      "over_generalization_answer": "A single prompt_injection text is one global blob folded into every scoring call; it cannot say 'reject only the 今彩539/大樂透/威力彩 draw-boilerplate format from 中央社財經' without collapsing into 'downweight anything financial/中央社', which is exactly the over-generalization Section 3 already warns this path risks -- it can express a theme, not a class.",
      "marginal_cost_per_new_cluster": {
        "unit": "labels",
        "value": "no fixed count -- one profile regeneration is a single LLM call over whatever titles exist, attempting to cover every emergent cluster at once with no held-out check (Section 3)",
        "note": "cost does not scale per-cluster the way a hand-authored rule does, but neither does correctness -- the profile can silently mis-generalize across many clusters in one shot."
      },
      "crossover": {
        "axis": "labels",
        "value": 6,
        "direction": "wins_at",
        "arithmetic": "profile.py:88 requires accepted_count>=3; assuming (an assumption -- the code only checks the accepted side) a symmetric bar for a genuinely contrastive read gives 3 accepted + 3 rejected = 6 in-scope clicks needed. In-scope means landing in the scored, non-news population (Section 2.1): today only 1 of the 3 clicks (the TechCrunch Amazon accept) does. Until opt-signal-capture's per-article attribution lets that in-scope rate itself be tracked over time, growth in total clicks does not predictably translate into growth in in-scope clicks."
      },
      "bootstrap_path": "This is turn 1's seed direction, and it is the one option that literally needs more labels before it has anything to work with -- specifically more accepted and rejected clicks landing inside the scored, non-news population; per-article attribution (opt-signal-capture) is what would even let that in-scope rate be tracked going forward.",
      "cost": {
        "formula": "extra_tokens_per_call * calls_per_run * gemini_in / 1_000_000",
        "result": 0.00075,
        "baseline_per_run": 0.073,
        "unit_prices": [
          {
            "name": "gemini_in",
            "value": 1.5,
            "unit": "USD per 1M input tokens",
            "source": "cyris.toml:17"
          }
        ],
        "inputs": {
          "extra_tokens_per_call": 100,
          "calls_per_run": 5
        }
      },
      "handles_news_mismatch": "Prompt injection only reshapes the scoring system prompt, which never runs on news-tagged articles (run_digest.py:123), so it cannot use the 2 news-heavy clicks (4 rows) at all -- only the 1 TechCrunch overlap is usable signal today.",
      "uses_pipeline_verdicts": false,
      "seam": "src/cyris/service_layer/prompts.py",
      "touches_persistence": false
    },
    {
      "id": "opt-embedding-rerank",
      "name": "Embedding-Based Similarity Reranking (Workers AI bge-m3 + Vectorize)",
      "verdict": "defer",
      "granularity": "semantic-class",
      "over_generalizes_to_source": false,
      "over_generalization_answer": "A similarity centroid/kNN built from the 193-row boilerplate class -- not the 1,252-row 財經 source -- separates 今彩539/大樂透/威力彩 draw-announcements from ordinary 中央社財經 reporting by embedding distance, not by source label. It is the one mechanism whose unit of judgment matches the constraint directly: narrower than a source, wider than one article.",
      "marginal_cost_per_new_cluster": {
        "unit": "zero",
        "value": 0,
        "note": "once the class-preference entity and a similarity index exist, a newly-emerged cluster needs no new authored rule -- its members simply fall near an existing (or new) centroid in vector space. This is the structural answer to opt-rule-filter's unbounded per-cluster authorship cost."
      },
      "crossover": {
        "axis": "corpus_rows",
        "value": 9765,
        "direction": "fails_at",
        "billing_basis": "total",
        "arithmetic": "Cost crossover (when the free tier stops being free): bge-m3 is 1024-dim (turn 1's dimensionality figure was wrong; corrected against huggingface.co/BAAI/bge-m3), $0.012/1M input tokens, 60k context. Vectorize Paid includes 10,000,000 stored dims -> 10,000,000/1024 = 9,765 vectors of headroom; the corpus (5,618 rows) has 4,147 rows of headroom left, which at the measured full-day partition spread of 116-336 rows/day (mean 216) is a band of 12-36 days (not a point estimate) before the free inclusion is exhausted. A 1-year corpus (5,618 + 216.1*365 = 84,486 rows) needs 86.5M dims; on a total-dims billing basis that's 86.5M * $0.05/100M = $0.043/mo (an overage-only basis would be $0.038/mo) -- the meter is cheap even past crossover. Capability crossover (when it wins on granularity, independent of cost): at any corpus size, embedding similarity is the only mechanism other than an ever-growing hand-authored regex library that can express the 193-row semantic class without conflating it with the 1,252-row source -- so its capability advantage does not depend on scale the way its storage cost does."
      },
      "bootstrap_path": "This option's bootstrap gate is the pre-registered validation check (Section 7) run against the corpus that already exists, plus the class_preference_entity one-way door (Section 8) deciding where a class-level judgment is stored once validated -- no new human triage step is required before that validation can run.",
      "cost": {
        "formula": "per_run_articles * tokens_per_article * bgem3 / 1_000_000",
        "result": 0.00045,
        "baseline_per_run": 0.073,
        "unit_prices": [
          {
            "name": "bgem3",
            "value": 0.012,
            "unit": "USD per 1M input tokens",
            "source": "https://developers.cloudflare.com/workers-ai/models/bge-m3/"
          }
        ],
        "inputs": {
          "per_run_articles": 150,
          "tokens_per_article": 250
        }
      },
      "handles_news_mismatch": "Reranking would run over the same scored, non-news population as the LLM scorer today, so it inherits the identical news blind spot as opt-prompt-injection -- a separate news-side embedding index is a distinct future project, not part of this option.",
      "uses_pipeline_verdicts": true,
      "pipeline_verdict_risk": "Bootstrapping the accepted-class centroid from score>=70 pipeline rows folds the pipeline's own bias back into the reranker -- and the lottery evidence is the proof it would be actively wrong here: 48 of the 50 lottery-format rows are pipeline-accepted while the human click rejected exactly that class (triage_feedback.py:38-39). Any centroid must be validated against the held-out human-labeled set before any pipeline-verdict augmentation is trusted.",
      "seam": "src/cyris/adapters/embeddings.py",
      "touches_persistence": false
    },
    {
      "id": "opt-rag-exemplar",
      "name": "Retrieval-Augmented Generation (RAG) Exemplar Injection",
      "verdict": "reject",
      "granularity": "semantic-class",
      "over_generalizes_to_source": false,
      "over_generalization_answer": "Retrieved exemplars are drawn by similarity to the same 193-row class, not by source, so it does not conflate 樂透 rejection with 中央社財經 as a whole -- but it inherits every embedding-index cost this document assigns to opt-embedding-rerank while adding retrieval-time bias (which exemplars get retrieved), which is why it is rejected rather than deferred.",
      "marginal_cost_per_new_cluster": {
        "unit": "labels",
        "value": "a handful of diverse exemplars per newly emerged theme/class",
        "note": "unlike centroid reranking, retrieval quality degrades without several diverse exemplars per emerging cluster, so -- unlike opt-embedding-rerank -- this option is not truly marginal-cost-free as new clusters appear."
      },
      "crossover": {
        "axis": "labels",
        "value": "not applicable -- rejected independent of scale",
        "direction": "fails_at",
        "arithmetic": "Even granting opt-embedding-rerank's cost profile, retrieval-time exemplar selection adds a second bias surface that does not shrink as the corpus grows -- more rows means more retrieval candidates, not fewer failure modes. There is no corpus size at which this option becomes preferable to plain reranking, so it is rejected rather than deferred."
      },
      "bootstrap_path": "Would need more diverse per-class exemplars than reranking's single centroid, and would still need the same embeddings_vectorize door opened first -- not pursued (see verdict).",
      "cost": {
        "formula": "k_exemplars * tokens_per_exemplar * calls_per_run * gemini_in / 1_000_000",
        "result": 0.005625,
        "baseline_per_run": 0.073,
        "unit_prices": [
          {
            "name": "gemini_in",
            "value": 1.5,
            "unit": "USD per 1M input tokens",
            "source": "cyris.toml:17"
          }
        ],
        "inputs": {
          "k_exemplars": 5,
          "tokens_per_exemplar": 150,
          "calls_per_run": 5
        }
      },
      "handles_news_mismatch": "Retrieval only injects exemplars into the non-news scoring prompt, sharing the identical news exclusion as opt-prompt-injection and opt-embedding-rerank.",
      "uses_pipeline_verdicts": true,
      "pipeline_verdict_risk": "Filling the retrieval corpus with pipeline-scored articles means the LLM retrieves its own past verdicts as 'evidence' for the next call -- the same self-reinforcing-loop risk named at triage_feedback.py:38-39, compounded because the exemplars sit directly inside the prompt the LLM reads, not just a numeric rerank weight; the 48-pipeline-accepted-vs-2-human-rejected lottery contradiction applies here too.",
      "seam": "src/cyris/adapters/vectorstore.py",
      "touches_persistence": false
    },
    {
      "id": "opt-rule-filter",
      "name": "Rule-Based Negative-Class Filter (Regex on Wire Boilerplate Titles)",
      "verdict": "adopt",
      "granularity": "exact-template",
      "over_generalizes_to_source": false,
      "over_generalization_answer": "The regex is scoped to the exact 第N期(開獎|中獎) draw-announcement template, not to 中央社財經 as a source -- it does not touch the other 1,202 non-lottery articles from that source (the measured source-level collateral) -- but it only covers 50 of the 193-row semantic class (25.9%), so on the other side of the granularity spectrum it is too narrow, not over-broad.",
      "marginal_cost_per_new_cluster": {
        "unit": "human-authored rule",
        "value": 1,
        "note": "one new hand-written rule per newly observed repeating template; the cost does not amortize across clusters -- it accumulates."
      },
      "crossover": {
        "axis": "cluster_count",
        "value": 2.79,
        "direction": "fails_at",
        "arithmetic": "The narrow lottery regex covers 50 of the 193-row semantic class (25.9%) and already spans 3 brands (今彩539:36, 大樂透:7, 威力彩:7) from a SINGLE reject click's two title-rows -- one class already needed multiple template variants. Corpus-wide, distinct recurring templates (the proxy) grew 0 (at 90 rows) to 71 (at 5,618 rows): (71-0)/(5618-90) = 1 new template per ~77.9 rows, i.e. ~2.79 new template-shaped clusters/day at the corpus's measured 216.1 rows/day. Under this option every one of those needs its own hand-authored rule, so the authorship burden is monotonically increasing and never converges, even though each individual rule costs $0 in tokens -- this is the growth-axis argument that supersedes turn 1's now-retracted 'replication across at least two independent reject events already met' bar (there was one reject click, not two independent reject events, so that bar was never actually met)."
      },
      "bootstrap_path": "Already has enough evidence to ship today against the measured 193-row semantic class and the executed checks (Section 7) -- no infrastructure work and no further human judgment is needed before shipping, which is exactly why it is adopted as an immediate, zero-cost stopgap rather than the long-term mechanism.",
      "cost": {
        "formula": "llm_calls_added(0) * gemini_in",
        "result": 0,
        "baseline_per_run": 0.073,
        "unit_prices": [
          {
            "name": "gemini_in",
            "value": 1.5,
            "unit": "USD per 1M input tokens",
            "source": "cyris.toml:17"
          }
        ],
        "inputs": {
          "llm_calls_added": 0
        }
      },
      "handles_news_mismatch": "This filter targets 中央社財經 wire-service titles directly -- exactly the population the scorer skips (run_digest.py:123) -- so it is the only option that can act on the 2 news-heavy clicks (4 rows) at all.",
      "uses_pipeline_verdicts": false,
      "seam": "src/cyris/domain/triage.py",
      "touches_persistence": false
    },
    {
      "id": "opt-signal-capture",
      "name": "Signal-Capture Fix: Persist Vote Timestamp and Per-Article Attribution",
      "verdict": "adopt",
      "granularity": "n/a",
      "over_generalizes_to_source": false,
      "over_generalization_answer": "This makes no topical claim about 樂透 or 中央社財經 at all -- it only changes how a click is recorded. Every option above that DOES have to answer the granularity question depends on this one first, because a class-level label needs to know which single article a vote was actually about.",
      "marginal_cost_per_new_cluster": {
        "unit": "zero",
        "value": 0,
        "note": "a one-time engineering change; its cost does not scale with cluster count or corpus size."
      },
      "crossover": {
        "axis": "corpus_rows",
        "value": 0,
        "direction": "wins_at",
        "arithmetic": "No crossover -- this is prerequisite infrastructure whose benefit (attributable, correctly-timed labels) begins accruing the moment it ships. Every day it does not ship, roughly 216.1 more rows accumulate under the old cluster-fanned, sync-batch-clock scheme (Section 2.2), whose true click-level attribution cannot be reconstructed after the fact."
      },
      "bootstrap_path": "Ships immediately: the moment promotions.py stops discarding PromotedArticle.ts/.digest_date and the vote-group click records per-article attribution instead of fanning one click's vote out to every URL in a cluster, every future click writes a trustworthy, individually attributable, correctly-timed record -- no threshold to cross first.",
      "cost": {
        "formula": "engineering_only(0) * gemini_in",
        "result": 0,
        "baseline_per_run": 0.073,
        "unit_prices": [
          {
            "name": "gemini_in",
            "value": 1.5,
            "unit": "USD per 1M input tokens",
            "source": "cyris.toml:17"
          }
        ],
        "inputs": {
          "engineering_only": 0
        }
      },
      "handles_news_mismatch": "This is instrumentation, not a scorer change -- it captures digest_date/ts and per-article identity regardless of whether the underlying article is news, preserving the mismatch as recoverable raw data going forward instead of silently losing it the way today's scheme does.",
      "uses_pipeline_verdicts": false,
      "seam": "src/cyris/adapters/promotions.py",
      "touches_persistence": true,
      "sync_protocol_ack": "ports.py:37 declares the SYNCHRONOUS ArticleRepository Protocol; promotions.py:97 already calls it that way, wrapped in asyncio.to_thread at run_digest.py:61, and any ts/digest_date or per-article extension must keep calling it the same synchronous, blocking way -- not introduce an await into the store call itself."
    },
    {
      "id": "opt-score-news",
      "name": "Score News-Tagged Articles (delete the 'news' skip)",
      "verdict": "defer",
      "granularity": "n/a",
      "over_generalizes_to_source": false,
      "over_generalization_answer": "Scoring news does not decide anything about 樂透 vs 中央社財經 either -- it only moves 中央社財經 (and other 'news'-tagged) articles into the population the LLM scorer sees at all, which is a precondition for opt-prompt-injection and opt-embedding-rerank to ever use a lottery-class label directly instead of being structurally blind to it.",
      "marginal_cost_per_new_cluster": {
        "unit": "zero",
        "value": 0,
        "note": "a one-time code change (deleting run_digest.py:123's `if \"news\" in a.source_tags: continue`); its ongoing marginal cost is LLM token spend per additional scored article, not per new cluster -- see cost block."
      },
      "crossover": {
        "axis": "corpus_rows",
        "value": 752,
        "direction": "fails_at",
        "arithmetic": "cfg.app.digest.max_articles_per_digest = 400 (cyris.toml:20; default 200, config.py:107) is sliced BEFORE the news filter (run_digest.py:117), against today's pool of ~114.7 rows/run (216.1 rows/day / 1.88 runs/day) -- the cap is non-binding today. It would only start binding once rows/day grows to roughly 400*1.88 = 752/day, about 3.5x today's rate; until then the +31%..+42% delta below is unaffected by the cap."
      },
      "bootstrap_path": "Ships independently of the label bootstrap -- once enabled, every future news-tagged article gets an LLM score, closing the population gap that today makes the 2 news-heavy clicks unusable by any scored-population option above.",
      "cost": {
        "formula": "news_rows_per_day / BATCH_SIZE * chars_per_batch / chars_per_token_band(2.0-3.0) -> tokens/day; tokens/day priced at gemini_in/gemini_out",
        "result": [
          0.043,
          0.0576
        ],
        "baseline_per_run": 0.073,
        "unit_prices": [
          {
            "name": "gemini_in",
            "value": 1.5,
            "unit": "USD per 1M input tokens",
            "source": "cyris.toml:17"
          },
          {
            "name": "gemini_out",
            "value": 7.5,
            "unit": "USD per 1M output tokens",
            "source": "cyris.toml:17"
          }
        ],
        "inputs": {
          "news_rows_per_day": 151.3,
          "batch_size": 20,
          "batch_size_source": "scoring.py:13",
          "batches_per_day": 7.57,
          "chars_per_batch": 7759,
          "chars_per_token_band": [
            2.0,
            3.0
          ],
          "max_articles_per_digest": 400,
          "max_articles_per_digest_source": "cyris.toml:20",
          "rows_per_run": 114.65,
          "cap_note": "the max_articles_per_digest cap is non-binding today: the pool is ~115 rows/run against a 400 cap (the slice at run_digest.py:117 happens before the news filter)"
        },
        "note": "+31%..+42% delta against the recomputed $0.1376/day baseline (0.073/run * 1.88 runs/day); reported as a band, not a point estimate, because the CJK chars-per-token ratio is uncertain (2.0-3.0)."
      },
      "handles_news_mismatch": "This IS the fix to the news mismatch for the scored population -- it deletes the run_digest.py:123 skip so news-tagged articles enter the scorer for the first time.",
      "uses_pipeline_verdicts": false,
      "seam": "src/cyris/service_layer/run_digest.py",
      "touches_persistence": false
    },
    {
      "id": "opt-feed-exclusion",
      "name": "Exclude a Feed/Source on Vote Evidence",
      "verdict": "defer",
      "granularity": "source",
      "over_generalizes_to_source": true,
      "over_generalization_answer": "This IS the source-level cell of the granularity measurement: excluding 中央社即時新聞 財經新聞 entirely to remove 50 lottery rows would suppress all 1,252 of its articles, misclassifying 1,202 non-lottery articles (784 already pipeline-accepted) as unwanted. Whether to exclude a feed based on vote evidence is a legitimate question in general, but conflating 'reject 今彩539-style boilerplate' with 'reject 中央社財經' is precisely the over-generalization this document rules out.",
      "marginal_cost_per_new_cluster": {
        "unit": "zero",
        "value": 0,
        "note": "editing an exclusion list is cheap to write; the cost that matters here is collateral suppression, not authorship effort -- cheap-to-write is not the same as low-risk."
      },
      "crossover": {
        "axis": "cluster_count",
        "value": "not applicable",
        "direction": "fails_at",
        "arithmetic": "Excluding a whole feed operates at source granularity regardless of corpus size -- today lottery is 50 of 1,252 財經 rows, and new non-lottery financial content keeps arriving at that same source at every corpus size measured (26 partitions), so this option does not improve as the corpus grows; it is wrong at every scale, not just today's."
      },
      "bootstrap_path": "Not part of the bootstrap sequence: legitimate to raise later from accumulated vote evidence once a feed's rejection rate is itself measurable (which needs opt-signal-capture's per-article attribution first), but it is not the current priority.",
      "cost": {
        "formula": "engineering_only(0) * gemini_in",
        "result": 0,
        "baseline_per_run": 0.073,
        "unit_prices": [
          {
            "name": "gemini_in",
            "value": 1.5,
            "unit": "USD per 1M input tokens",
            "source": "cyris.toml:17"
          }
        ],
        "inputs": {
          "engineering_only": 0
        }
      },
      "handles_news_mismatch": "Would remove 中央社財經 (a news-tagged source) from the corpus entirely rather than handle the scorer mismatch -- it sidesteps the mismatch by deleting the population, which is exactly the over-broad move this document rules out as the current action.",
      "uses_pipeline_verdicts": false,
      "not_current_priority": true,
      "seam": "sources.yaml",
      "touches_persistence": false
    }
  ],
  "executed_checks": [
    {
      "name": "semantic-class prevalence vs narrow-template regex",
      "inputs": [
        "agent-vault/articles/*.json"
      ],
      "script": "load all 26 partitions; count title matches for the narrow lottery regex 第\\d+期.*(開獎|中獎) (N=50) vs the wider boilerplate-class regex covering lottery+TAIEX+TWD+institutional-flow+futures wire formats (N=193); report 50/193",
      "threshold": "pre-stated: if the narrow template regex covers three-quarters or more of the measured semantic class, exact-template regex is sufficient on its own; materially below that means the class needs a coarser mechanism too",
      "measured_outcome": {
        "narrow_template_matches": 50,
        "semantic_class_matches": 193,
        "coverage": "50/193 (25.9%)"
      },
      "decision": "discharged: the narrow regex covers well under three-quarters of the measured class -- exact-template regex is not sufficient on its own; retained only as an immediate, zero-cost stopgap (opt-rule-filter) while a class-level mechanism is built."
    },
    {
      "name": "source-exclusion collateral",
      "inputs": [
        "agent-vault/articles/*.json"
      ],
      "script": "filter source_name == 中央社即時新聞 財經新聞 (N=1252); subtract the 50 lottery matches -> 1202 non-lottery rows; of those, count state==accepted (784, i.e. pipeline-accepted non-lottery financial news)",
      "threshold": "pre-stated: if excluding the source removes materially more non-lottery, pipeline-accepted rows than the 50 lottery rows it targets, source-level exclusion is disqualified as too broad",
      "measured_outcome": {
        "suppressed_total": 1252,
        "non_lottery_suppressed": 1202,
        "non_lottery_pipeline_accepted": 784
      },
      "decision": "discharged: excluding the source suppresses 1,202 non-lottery articles (784 already pipeline-accepted) to remove 50 lottery rows -- source-level exclusion is disqualified as the current action; see opt-feed-exclusion's verdict."
    },
    {
      "name": "template-count growth across the corpus",
      "inputs": [
        "agent-vault/articles/*.json"
      ],
      "script": "accumulate the 26 partitions in date order; at each cumulative prefix count distinct re.sub(digits->'#', title)[:10] signatures occurring >=3 times; report the (rows, template_count) series from the first partition (90 rows) to the full store (5618 rows)",
      "threshold": "pre-stated: growth in distinct recurring templates as the corpus grows indicates whether hand-authored regexes accumulate (grows, no plateau) or saturate (plateaus) -- a plateau would support treating exact-template regex as eventually sufficient",
      "measured_outcome": {
        "templates_at_90_rows": 0,
        "templates_at_5618_rows": 71,
        "rate_per_row": 0.01284,
        "rate_per_day_at_current_pace": 2.79
      },
      "decision": "discharged: template count grew roughly linearly with corpus size (0 to 71, no plateau observed across 26 partitions) at ~2.79 new templates/day at today's pace -- a per-template hand-authored rule does not converge; this is the crossover evidence cited in opt-rule-filter."
    }
  ],
  "pre_registered_checks": [
    {
      "name": "bge-m3 recall/precision validation on the 193-row boilerplate class",
      "inputs": [
        "agent-vault/articles/*.json",
        "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/@cf/baai/bge-m3"
      ],
      "why_not_executed": "at least one input is a network API call to Cloudflare Workers AI, not local read-only data -- and running it would prematurely open the embeddings_vectorize one-way door before this document has validated whether it is worth opening",
      "threshold": "pre-stated: embed the 193-row boilerplate class plus a matched sample of ~1,100 non-boilerplate 中央社財經 rows; using the two title-rows produced by the single reject click as a nearest-neighbour ground-truth seed, both precision and recall of the boilerplate class on a held-out split must clear a floor to be fixed before running (not derived from the run itself) -- no threshold shortcut is taken here.",
      "decision_rule": "if precision and recall both clear the pre-stated floor, proceed to open embeddings_vectorize + class_preference_entity and build opt-embedding-rerank; otherwise keep opt-rule-filter as the front-line stopgap and re-run this validation at the next order-of-magnitude corpus checkpoint. Estimated validation cost: ~1,300 rows x ~250 tokens x $0.012/1M input tokens ~= $0.004 -- trivial relative to the value of the answer."
    }
  ],
  "cost_baseline": {
    "per_run": 0.073,
    "per_day": 0.1376,
    "runs_per_day": 1.88,
    "method": "recomputed directly from agent-vault/usage.jsonl's 49 rows at input_tokens * $1.50/1M + output_tokens * $7.50/1M, the actual gemini-3.6-flash rates configured at cyris.toml:17",
    "logged_field_rejected": {
      "field": "estimated_cost_usd",
      "logged_avg": 0.1461,
      "factor": 2,
      "cause": "models.py:78-80 hardcodes Sonnet pricing (input_tokens*3 + output_tokens*15)/1e6 while the configured provider has been gemini since cyris.toml:17 -- a stale constant, not a measurement"
    }
  },
  "pipeline_verdict_rule": {
    "text": "5,613 of the store's 5,618 rows (all but the 5 human-triaged rows) are the pipeline's own accept/reject verdicts, not human judgments, and triage_feedback.py:38-39 is why collect_triage_feedback excludes them: only triaged_at-stamped rows are human labels; the rest are the pipeline's own verdicts, and learning from those is a self-reinforcing loop. No option above may use those 5,613 rows as ground-truth preference labels; where an option folds them in anyway as weak/pseudo-labels, it must name that risk at its own point of use.",
    "anchor": "triage_feedback.py:38-39",
    "non_triaged_rows": 5613
  },
  "one_way_doors": [
    {
      "id": "class_preference_entity",
      "opens": "Where a preference at the semantic-class level -- narrower than a source, wider than one article -- is stored decides whether class-level preference is inspectable/editable by a human (a cluster id, a concept-store row) or only implicit in vector-space geometry with no persisted identity.",
      "closes": "StoredArticle (models.py:127-147: url, original_id, title, content, author, published_at, source_name, source_tier, source_tags, state, first_seen_at, digest_date, rejection_reason, score, language, scored_at, triaged_at, exported_at -- no cluster/class/concept id) has no field to hold it today. Every one of opt-embedding-rerank, opt-rag-exemplar and opt-rule-filter implies a different answer to where this lives; picking a mechanism before this decision is picking this door blind."
    },
    {
      "id": "vote_semantics",
      "opens": "Persisting ts/digest_date (promotions.py:17-23) and moving from cluster-fanned to per-article votes opens trustworthy arrival-rate and cluster-attribution measurement for the first time.",
      "closes": "Every future label's meaning changes shape at once: votes recorded under the current cluster-fanned, sync-batch-clock semantics cannot be reinterpreted after the fact, closing off any option built assuming the old shape once it changes."
    },
    {
      "id": "embeddings_vectorize",
      "opens": "Adopting Workers AI embeddings (bge-m3, 1024-dim, $0.012/1M input tokens) plus Vectorize opens a similarity/RAG surface no current adapter provides, and is the only mechanism this document finds that meets constraint 2's granularity requirement at any corpus size.",
      "closes": "Locks in a model version and a re-index migration path the moment any article is embedded -- swapping bge-m3 for a different model later means re-embedding the whole, growing corpus. Now quantified: the Vectorize Paid 10M-stored-dim inclusion holds 9,765 vectors at 1024 dims and the corpus crosses it in a 12-36-day band at the measured daily spread; past that, ~$0.043/mo for a 1-year corpus (total-dims basis) -- the meter is cheap, but the door still locks in a model+index choice."
    },
    {
      "id": "label_record",
      "opens": "A dedicated label record distinct from state+triaged_at opens room for multi-valued/weighted feedback (cluster-fan weight, vote source, real vote time, class id) today's two-field scheme cannot express.",
      "closes": "Every reader of StoredArticle (models.py:127-147) and every existing consumer of triaged_at gains a second, must-stay-in-sync source of truth about the same event, closing off today's single-field simplicity for good."
    }
  ],
  "bootstrap_sequence": {
    "label_arrival_caveat": "The rate at which new votes will arrive is structurally unmeasurable today, not merely unmeasured: promotions.py:97 stamps every synced vote with datetime.now(UTC) at sync time rather than the wire payload's real vote time, which promotions.py:17-23 parses into PromotedArticle.ts and .digest_date and then discards. Today's 5 rows / 3 clicks give zero information about cadence, and no step below may be read as implying a labels-per-week/month figure until Step 1 closes that capture gap.",
    "steps": [
      {
        "order": 1,
        "step": "Fix signal capture: stop discarding PromotedArticle.ts/.digest_date and move the vote-group click from cluster fan-out to per-article attribution.",
        "unlocks": "every future click becomes an individually attributable, correctly-timestamped record instead of a cluster-fanned, sync-clock-stamped one",
        "serves": [
          "opt-signal-capture"
        ],
        "gated_on": null,
        "measurable_when_done": "a new promote click produces a per-article record whose timestamp matches the wire payload's parsed PromotedArticle.ts, not the sync-time datetime.now(UTC)"
      },
      {
        "order": 2,
        "step": "Ship the rule-based filter against the measured 193-row semantic class immediately, in parallel with Step 1.",
        "unlocks": "the class the votes are actually about (routine wire boilerplate) stops reaching the digest today, without waiting on any infrastructure work",
        "serves": [
          "opt-rule-filter"
        ],
        "gated_on": null,
        "measurable_when_done": "the 50 matched lottery-format titles (and any newly emerged templates matching the same class) no longer appear in a produced digest"
      },
      {
        "order": 3,
        "step": "Once signal capture (Step 1) lands, accumulate individually-attributed, correctly-timestamped clicks toward the in-scope threshold and (re-)generate the prompt-injection profile.",
        "unlocks": "a cheap, LLM-native preference signal for the scored population, backed by clicks whose in-scope rate can finally be tracked instead of assumed",
        "serves": [
          "opt-prompt-injection"
        ],
        "gated_on": 1,
        "measurable_when_done": "agent-vault/learning/profile.json exists, was generated from clicks stamped under the new per-article scheme, and is regenerated (not silently reused) as new in-scope clicks arrive"
      },
      {
        "order": 4,
        "step": "Run the pre-registered bge-m3 validation (Section 7) against the existing corpus; if it clears the pre-stated threshold, open the class_preference_entity and embeddings_vectorize doors and stand up opt-embedding-rerank as the semantic-class mechanism.",
        "unlocks": "a mechanism whose marginal cost per new cluster is zero, which is what the corpus's ~2.79 new template-shaped clusters/day (Section 6, opt-rule-filter crossover) needs once hand-authored rules stop keeping pace",
        "serves": [
          "opt-embedding-rerank"
        ],
        "gated_on": "class_preference_entity",
        "measurable_when_done": "the validation's measured precision/recall against the held-out split is recorded and compared to the pre-stated floor, and a class_preference_entity schema decision is made either way"
      }
    ]
  },
  "recommendation": {
    "action": "Ship opt-signal-capture -- stop discarding PromotedArticle.ts/.digest_date in promotions.py and move the vote-group click to per-article attribution -- alongside opt-rule-filter (Step 2, no dependency), before building any of the three labeled/embedding options.",
    "traceable_to": "opt-signal-capture",
    "why_now": "The corpus adds ~216.1 rows/day and ~2.79 new template-shaped clusters/day (Section 6 crossover); every day without per-article attribution and real vote timestamps, that growth accumulates under a cluster-fanned, sync-batch-clock scheme whose true click-level attribution cannot be reconstructed after the fact -- this is what makes every later option's own bootstrap step (Section 6) unable to state a timeline, not today's click count."
  },
  "external_facts": {
    "bge_m3": {
      "dims": 1024,
      "price_per_1m_input_tokens": 0.012,
      "source": "https://huggingface.co/BAAI/bge-m3 (dims), https://developers.cloudflare.com/workers-ai/models/bge-m3/ (price, 60k context)"
    }
  }
}
```
