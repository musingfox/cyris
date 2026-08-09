# Vote-Signal Utilization Analysis: Improving Article Selection from Digest Up/Downvotes

Scope: this document evaluates ways to turn the digest's up/downvote clicks into a
better article-selection signal than today's LLM score alone. Observation date for
every time-sensitive claim below is **2026-08-09** (the store's latest partition).
Everything stated as fact is recomputed directly from `agent-vault/articles/*.json`
(5,618 rows across 26 daily partitions) and `agent-vault/usage.jsonl` (49 rows), or
cited to a repo `path:line`. External claims (Cloudflare pricing) cite the
documentation page. Anything not measured or cited is marked an assumption.

The headline finding is uncomfortable but load-bearing for everything that follows:
**there are only 5 human votes in the entire store, they are lopsidedly one category
of article, and the one existing machine-learning path built to consume them has
never actually run.** Every option below is evaluated against that reality, not
against a hoped-for future where labels are plentiful.

## 1. What The Votes Actually Are Today

Human triage has produced exactly 5 labels to date — 3 accepted articles and 2
rejected articles — out of 5,618 stored articles total, of which 1,614 carry an LLM
score (871 of those score >= 70). The overlap between the human labels and the LLM's
scored population is 1 of the 5: only the TechCrunch "Planned Amazon data center..."
accept (score 80.0) falls inside the population the scorer ever touches; the other 4
labels are score=None because they are 中央社即時新聞 items, which the scorer never
sees (Section 2 below).

Both of the two rejected labels are the exact same wire-service artifact: 今彩539 (a
government lottery number draw) reported twice by 中央社即時新聞 財經新聞 within the
same digest window — "今彩539第115192期　頭獎1注中獎" and "今彩539第115192期開獎" are
the same draw announced from two separate CNA URLs. This is not a topical rejection;
it is a **format/junk filter** test case — the reader is not saying "I dislike
financial news," they are saying "stop showing me routine wire-service draw-result
boilerplate." No option below may treat this negative class as a signal about
topical *preference*; doing so would train every option to associate "finance" or
"中央社" with rejection, which is not what actually happened here.

### The vote arrival rate cannot be measured

How often new votes arrive matters directly to how long reaching any of the label
targets derived below would take, but that arrival rate is unmeasurable from today's
data: all 5 `triaged_at` stamps collapse to the exact same instant,
`2026-08-09T05:51:08.904839Z`, because `promotions.py:97` calls
`store.update_triage_timestamp([a.url for a in found], datetime.now(UTC))` — it
stamps every vote synced in one batch with the sync-time clock, not the vote's own
time. The vote's real time already exists on the wire — `promotions.py:17-23` parses
`PromotedArticle.ts` and `.digest_date` out of every incoming payload — but the value
is parsed and then discarded; it is never written to the store. Until that is fixed,
no option in this document may quote a labels-per-week/day/month figure as measured
data; nothing below does.

## 2. Two Structural Mismatches Every Option Must Answer

### 2.1 The news/scorer mismatch

`run_digest.py:123` skips scoring for any article carrying `"news"` in
`source_tags` (`if "news" in a.source_tags: continue`), and `run_digest.py:121`
additionally skips the FAN tier — the scorer never sees the population most of our
votes come from. Concretely, 4 of the 5 human labels are 中央社 news items, so 80% of
today's real votes land on articles the LLM scoring path structurally never touches;
only the TechCrunch accept overlaps the scored set at all (Section 1). Every option
in Section 6 states explicitly how it handles this — most cannot use the news-tagged
votes for anything, and only the rule-based filter targets that population directly.

### 2.2 Cluster-level vote attribution

Cluster-level vote attribution is a live risk, not a hypothetical. The promote button
macro emits one `vote-group` per digest item whose `data-urls` attribute is a JSON
list (`digest.html.j2:1-2`); a news cluster shares that one vote-group across every
article in it (`digest.html.j2:871`: "a news cluster is one vote-group over every
article in it"), and a click fans out to a separate `POST /promote` per URL with the
*same* vote value and the same `digest_date` (`digest.html.j2:887,893`). Our two
中央社 "accepted" labels — 荷莫茲海峽再傳船隻遇襲 and 伊朗列荷莫茲全面開放條件, both
Hormuz-Strait stories, stamped at the identical instant — are consistent with exactly
this: one human click credited identically to two separate stored articles, not two
independent judgments. Because `StoredArticle` (`models.py:127-147`) carries no
cluster id, this cannot be proven after the fact from the store alone, so the
attribution rule adopted here is: **treat cluster-fanned votes as N correlated
labels sharing one underlying judgment, never as N independent labels**, until
per-article voting (a one-way door, Section 8) replaces cluster-level voting.

## 3. Why The Existing PreferenceProfile Path Isn't Enough

As of the observation date **2026-08-09**, `agent-vault/learning/profile.json` has
**never been generated** — every scoring call to date has run with
`preference_profile=None`, so `prompts.py:244-245` has always returned the bare
`SCORING_SYSTEM` string with no preference injection at all. This is not because the
path is disabled: `run_digest.py:35` defaults `enable_learning: bool = True`, and the
gate that has been blocking it, `profile.py:88`'s `accepted_count < 3` check, sits
against today's `actual_accepted = 3` — it would just barely pass if `cyris learn`
were run right now. `triage_feedback.py:13` additionally requires `min_triaged=3`
total triaged rows within `learning.py:25`'s 14-day collection window; both are also
satisfied today.

That the gate would pass is exactly why "not enough labels" is the wrong verdict on
this path. Even after the accepted_count>=3 gate passes, `generate_profile_from_triage`
(`profile.py:72-115`) feeds raw titles into a single unvalidated LLM call with no
held-out evaluation step, so the resulting `prompt_injection` text cannot be checked
against future votes before it starts silently steering every scoring call; nothing
in `profile.py` ever re-generates or invalidates a stale profile, so one contrastive
read of five titles — 4 of which are the news items this document already
disqualifies as a topical preference signal (Section 1) — would keep steering scoring
indefinitely with no drift check. Turning this path on today would inject a profile
built almost entirely from "reject 今彩539-style wire copy," misread by the LLM as a
themed preference, into every single scoring call. The structural problem is
evaluation, not volume.

## 4. Cost Baseline

Recomputing directly from `agent-vault/usage.jsonl`'s 49 rows —
`input_tokens x $1.50/1M + output_tokens x $7.50/1M`, the actual gemini-3.6-flash
rates configured in `cyris.toml:17` — gives a baseline of **$0.073/run**
(~$4.38/month at 60 runs/month), matching `docs/cloud-migration.md:100-101`'s
independently-measured figure.

The `usage.jsonl` file's own `estimated_cost_usd` field must never be used as that baseline: it averages $0.1461/run, almost exactly **2x too high**, because `models.py:78-80` hardcodes Sonnet pricing (`input_tokens * 3 + output_tokens * 15`) while the configured provider has been gemini since `cyris.toml:17` — a stale constant, not a measurement, and every cost comparison below uses the recomputed $0.073/run baseline, never the logged field.

## 5. The 5,613 Pipeline-Verdict Rows

5613 of the store's 5,618 rows (5,618 minus the 5 human labels) are the pipeline's
own accept/reject verdicts, not human judgments. `triage_feedback.py:38-39` states
why they are excluded from `collect_triage_feedback` today: "Only triaged_at-stamped
articles are human labels; the rest are the pipeline's own verdicts, and learning
from those is a self-reinforcing loop." The ruling in this document is that these
5,613 rows **may not** be used as ground-truth preference labels by any option below.
Where an option chooses to fold them in anyway — as weak/pseudo-labels rather than
ground truth, a real and sometimes reasonable design choice — it must name that
self-reinforcement risk at its own point of use; opt-2 and opt-3 in Section 6 do.

## 6. Options Considered

Each option below states a verdict, the minimum number of human labels it would need
to structurally have a chance of outperforming today's score-only ordering (with a
recomputable, provenance-tagged derivation), an incremental cost against the $0.073
baseline, how it handles the news/scorer mismatch, its architectural seam, and
whether it leans on the excluded pipeline verdicts.

### Title-to-Prompt Injection (Preference-Profile Text Prompt)

**Verdict: DEFER.**

This is the seed direction of activating the already-built `PreferenceProfile` path
(Section 3) for real: let `generate_profile_from_triage` summarize accepted vs.
rejected titles into a short `prompt_injection` string and have `build_scoring_system_prompt`
(`prompts.py:235-247`) fold it into every scoring call's system prompt.

*Required labels.* The path only affects the scoring system prompt, and the scoring
system prompt only ever runs on non-news, non-fan articles (Section 2.1) — so a label
only teaches this option anything if it lands in that scored population. Today the
overlap fraction is 1 of the 5 triaged rows. `profile.py:88` requires
`accepted_count >= 3`; assuming that threshold applies symmetrically to the rejected
side for a genuinely contrastive read (an assumption — the code only checks the
accepted side), that is 3 accepted + 3 rejected = 6 in-scope labels needed. Scaling
6 in-scope labels by the measured 1-in-5 in-scope rate gives:

`N = required_per_class(3, measured, profile.py:88) * classes(2, assumed) * triaged(5, measured, store) / overlap(1, measured, store) = 3 * 2 * 5 / 1 = 30`

Thirty total human votes, composed the way today's 5 are, before this option even has
raw material to work with — and Section 1 already showed the arrival rate needed to
turn that into a timeline is itself unmeasured.

*News mismatch.* Prompt injection only reshapes the scoring system prompt, which
never runs on news articles (`run_digest.py:123`), so it cannot use the 4-of-5
news-heavy label set at all — only the 1 TechCrunch overlap is usable signal today.

*Pipeline verdicts.* Not used — `collect_triage_feedback` (`triage_feedback.py:40-57`)
already filters to `triaged_at`-stamped rows only.

*Cost.* Negligible: the injected text adds on the order of 100 extra input tokens to
each of an assumed 5 scoring calls per run, at gemini's $1.50/1M input rate:
`100 * 5 * 1.5 / 1,000,000 = $0.00075/run`, about 1% of the $0.073 baseline.

*Architecture.* No new IO boundary — reuses the existing `LLMClient` Protocol
(`ports.py`); no persistence beyond the already-existing `agent-vault/learning/profile.json`
file write, which does not go through `ArticleRepository`.

### Embedding-Based Similarity Reranking (Workers AI bge-m3 + Vectorize)

**Verdict: DEFER.**

Embed accepted/rejected articles with Cloudflare Workers AI's multilingual `bge-m3`
model, compute a similarity score between each newly scored article and the
accepted-label centroid, and blend that into ranking alongside the LLM score.
bge-m3 is the model to default to if this is ever built: at $0.012/1M input tokens it
is the cheapest of the three Workers AI embedding models, and it is the only
multilingual one — the `bge-*-en` variants are English-only, which does not fit a
corpus that is heavily 中央社 Chinese-language wire copy
(<https://developers.cloudflare.com/workers-ai/platform/pricing/>).

*Required labels.* Same scored-population restriction as the prompt-injection option
(Section 2.1), so the same 1-in-5 in-scope rate applies. A similarity centroid is
noisier than an LLM's contrastive read, so a materially larger per-class minimum is
assumed — 10 examples per class (assumed; no repo anchor, a general few-shot/centroid
stability heuristic) rather than profile.py's 3:

`N = per_class_min(10, assumed) * classes(2, assumed) * triaged(5, measured, store) / overlap(1, measured, store) = 10 * 2 * 5 / 1 = 100`

*News mismatch.* Reranking would run over the same scored (non-news) population as
the LLM scorer, so it inherits the identical news blind spot; the 4 news labels
remain unusable until a separate news-side embedding index is built as its own
project.

*Pipeline verdicts.* Bootstrapping the accepted-centroid from score>=70 pipeline rows
in addition to the 1 real label folds the pipeline's own bias back into the reranker
signal — a self-reinforcing loop per `triage_feedback.py:38-39`. If this option is
ever built, its centroid must be validated against a human-labeled holdout before any
pipeline-verdict augmentation is trusted.

*Cost.* At an assumed 150 articles/run (informed by `docs/cloud-migration.md:103`'s
measured 120-170/run range) and an assumed ~250 tokens/article (1000-char scoring
snippet / ~4 chars-per-token): `150 * 250 * 0.012 / 1,000,000 = $0.00045/run`, well
under 1% of baseline. Vectorize storage for the full 5,618-article backlog is
5,618 x 768 = 4.31M dimensions, inside the Paid plan's first-10M-stored-dimensions
inclusion (<https://developers.cloudflare.com/vectorize/platform/pricing/>), so
storage cost is $0 at current volume; query cost is likewise inside the first
50M-queried-dimensions/month inclusion at this scale.

*Architecture.* New adapter, e.g. `adapters/embeddings.py`, behind no existing
Protocol (a new one would be reasonable) — no local GPU, no resident model, HTTP-only
Workers AI call. No `ArticleRepository` persistence touched; vectors would be keyed
by URL in a separate Vectorize index.

### Retrieval-Augmented Generation (RAG) Exemplar Injection

**Verdict: REJECT.**

Rather than a static profile or a similarity score, retrieve the k most similar past
labeled articles per batch and inject them as few-shot exemplars into the scoring
prompt at call time. This needs the same embeddings + Vectorize infrastructure as the
reranking option (Section 6, previous), plus a live retrieval step per batch, for a
form of signal that is strictly harder to validate than a static profile or centroid.

*Required labels.* This option needs enough exemplar diversity per class to make
retrieval meaningful, not just enough for one centroid — a materially larger bar than
reranking. Using `profile.py:19`'s own stated theme count (the `TRIAGE_PROFILE_PROMPT`
asks the LLM for "3-5 themes") as the upper-bound theme count, and assuming 5 diverse
examples per theme per class as a retrieval-diversity heuristic (assumed; no repo
anchor):

`N = per_theme_min(5, assumed) * themes(5, measured, profile.py:19) * classes(2, assumed) * triaged(5, measured, store) / overlap(1, measured, store) = 5 * 5 * 2 * 5 / 1 = 250`

`N = 250` is five times the reranking option's requirement and over 80x today's label
count, for a technique that shares every one of reranking's infrastructure costs
(Section 8's `embeddings_vectorize` door) while adding retrieval latency and a second
place bias can enter (which exemplars get retrieved). Given the identical news
mismatch and identical self-reinforcement exposure below, the added complexity is not
proportionate to any accuracy gain this document can substantiate — hence reject, not
defer, distinguishing it from the simpler reranking option this document keeps open.

*News mismatch.* Retrieval only injects exemplars into the non-news scoring prompt,
so it shares the same news exclusion as the two previous options and gains nothing
from the 4 news-tagged labels.

*Pipeline verdicts.* Filling the retrieval corpus with pipeline-scored articles
beyond the 5 human labels means the LLM retrieves its own past verdicts as
"evidence" for the next call — the same self-reinforcing-loop risk named at
`triage_feedback.py:38-39`, compounded because the exemplars sit directly inside the
prompt the LLM reads, not just a numeric rerank weight.

*Cost.* Assuming 5 retrieved exemplars/batch at ~150 tokens each, across an assumed 5
scoring calls/run, at gemini's $1.50/1M input rate:
`5 * 150 * 5 * 1.5 / 1,000,000 = $0.005625/run` (~$0.0056), about 7.7% of the $0.073
baseline in marginal LLM tokens alone — before the embeddings/Vectorize build cost
this shares with the reranking option.

*Architecture.* New adapter, e.g. `adapters/vectorstore.py`, for the retrieval index;
no local GPU, no resident model. No `ArticleRepository` persistence touched.

### Rule-Based Negative-Class Filter (Regex on 今彩539-style Titles)

**Verdict: ADOPT.**

Not in the seed list: a deterministic, LLM-free filter that pattern-matches the
periodic wire-service draw-announcement title format the two real rejects exemplify
(e.g. `第\d+期.*(開獎|中獎)` from 中央社財經 sources) and drops matching articles
before they ever reach scoring or the digest, the same way the FAN tier and news skip
already short-circuit scoring today (`run_digest.py:121-124`).

*Required labels.* This is not a statistical model; it is a rule mined directly from
the negative examples in hand. The bar for treating a repeated title format as a
rule rather than coincidence is replication across at least 2 independent reject
events — which today's data already provides:

`N = rejected(2, measured, store) → formula = rejected → N = 2`

Today's 2 rejected labels already clear this option's threshold — it is the only
option in this document whose data requirement is already met, though that only
justifies these specific two labels, not the rule's precision at scale (Section 7's
experiment tests exactly that, before deployment).

*News mismatch.* This filter targets 中央社財經 wire-service titles directly — exactly
the population the scorer skips (Section 2.1). It is the only option that can use the
4 news-tagged labels at all.

*Pipeline verdicts.* Not used — the rule is mined from the 2 human rejects only, not
from any of the 5,613 excluded pipeline-verdict rows.

*Cost.* $0 marginal: `llm_calls_added(0) * gemini_in(1.5) = $0/run` — a title regex
adds no LLM calls, and by dropping junk before scoring it can only reduce token spend
against the $0.073 baseline, never increase it.

*Architecture.* Pure function, no new IO boundary; conceptually belongs beside the
existing tier-skip logic in `run_digest.py` / `domain/triage.py`. Anchored here at
`service_layer/ports.py` for schema consistency since no new Protocol or adapter is
required. No `ArticleRepository` persistence touched.

### Signal-Capture Fix: Persist Vote Timestamp and Per-Article Attribution

**Verdict: ADOPT.**

Not in the seed list: this is not a modeling option at all — it is the prerequisite
infrastructure fix. Change `promotions.py` to stop discarding `PromotedArticle.ts`
and `.digest_date` (currently parsed at `promotions.py:17-23` and then never read
again) and persist them onto the label record, and change the vote-group click
(`digest.html.j2:871-893`) to attribute one vote per underlying article rather than
fanning one click's vote out to every URL in a cluster (Section 2.2).

*Required labels.* This option does not compete on labeled accuracy, so the usual
derivation does not apply to it the way it does to the modeling options above; it is
the reason any of their `N` targets could ever be reached on a known timeline.

`N = prerequisite_labels(0, assumed) → formula = prerequisite_labels → N = 0`

Zero, because this is a data-capture prerequisite, not a model that needs training
examples to beat anything.

*News mismatch.* This is instrumentation, not a scorer change; it captures
digest_date/ts context and per-article identity regardless of whether the underlying
article is news or not, so the news/scorer mismatch is preserved as recoverable raw
data going forward rather than silently lost the way it is today.

*Pipeline verdicts.* Not used — this option only changes how the 5 (soon to be more)
human-vote rows are captured, not what counts as a label.

*Cost.* $0 marginal: `engineering_only(0) * gemini_in(1.5) = $0/run` — a
store/promotions schema change with no new LLM calls.

*Architecture.* Extends the existing `adapters/promotions.py` adapter. This is the
one option that does touch persistence: `promotions.py:97` already calls
`store.update_triage_timestamp(...)` synchronously against `ArticleRepository`
(`ports.py:37`), and `run_digest.py:61` already wraps the whole `sync_promotions`
call in `asyncio.to_thread(...)` precisely because `ArticleRepository` is a
SYNCHRONOUS Protocol — any extension (writing `ts`/`digest_date`, or a per-article
label record) must keep calling it the same synchronous way, not introduce an
`await` into the store call itself.

## 7. Cold-Start Experiment (Pre-Registered)

This experiment is runnable today, against data already in the repo, with no new
labels and no new infrastructure — and it is deliberately **not** a held-out
predictive-accuracy test over the 5 labels, because n=5 with a degenerate,
single-category negative class cannot support one (Section 1). Instead it measures
something n=5,618 can actually decide: how common the rejected title FORMAT is across
the whole corpus, independent of any scoring or accuracy question.

- **Inputs:** `agent-vault/articles/*.json` (all 26 partitions, read-only).
- **Null hypothesis:** the periodic draw-announcement pattern mined from the 2 known
  rejects (title matching a regex like `第\d+期.*(開獎|中獎)`, source 中央社財經) is a
  one-off quirk of this single digest run, not a recurring category — i.e., fewer
  than 5 titles in the entire 5,618-row store match the same pattern.
- **Pre-registered threshold:** >= 5 matching titles across the whole store rejects
  the null; < 5 fails to reject it.
- **Measured quantity:** count of stored articles (all 5,618 rows, not just the 1,614
  scored subset) whose title matches the mined draw-announcement regex — a corpus
  prevalence count of a title-format pattern, not a scorer-quality measurement.
- **Sample size:** 5,618 (the full store is scanned; the pattern itself is mined from
  the 2 known rejects).
- **Decisions:**
  - Outcome ">= 5 of the 5,618 stored titles match the mined draw-announcement
    pattern" -> decision: **adopt the rule-based filter (Section 6) as specified**,
    using the mined regex as its production rule.
  - Outcome "< 5 of the 5,618 stored titles match the mined pattern" -> decision:
    **defer the rule-based filter** — treat the two known rejects as a one-off rather
    than a recurring category, and collect more reject labels before automating a
    rule.
- **What this experiment cannot conclude:** it only measures how common the
  draw-announcement TITLE FORMAT is corpus-wide; it says nothing about whether the
  LLM scorer would already down-rank equivalent junk on its own for scored articles,
  because news-tagged articles are never scored in the first place
  (`run_digest.py:123`) — the junk-filtering problem and the score-based-ranking
  problem are structurally disjoint populations, and this experiment cannot bridge
  that gap.

## 8. One-Way Decisions For The Human

These three are surfaced as decisions for the human, not settled by this document —
each is one-way: cheap to open, expensive or impossible to close back up once other
work builds on top of it.

- **Vote semantics.** Redesigning promote payload handling to persist `ts`/
  `digest_date` (`promotions.py:17-23`) and to move from cluster-fanned votes to
  genuinely per-article ones opens the door to trustworthy arrival-rate and
  cluster-attribution measurement for the first time. It also means every future
  label's meaning changes shape at once: votes recorded under the current
  cluster/no-timestamp semantics cannot be reinterpreted after the fact, closing off
  any option built assuming the old shape once it changes.
- **Whether embeddings/Vectorize enter the stack.** Adopting Workers AI embeddings +
  Vectorize (bge-m3 at $0.012/1M tokens; Vectorize at $0.01/1M queried dims and
  $0.05/100M stored dims) opens a whole similarity/RAG surface no current adapter
  provides. It also locks in a model version and a re-index migration path the
  moment any article is embedded — swapping bge-m3 for a different model later means
  re-embedding the whole (and growing) corpus, closing off a costless model change.
- **Whether the store grows a dedicated label record.** Giving the store a label
  record distinct from `state`+`triaged_at` opens room for multi-valued/weighted
  feedback (cluster-fan weight, vote source, real vote time) that today's two-field
  scheme cannot express. It also means every reader of `StoredArticle`
  (`models.py:127-147`) and every existing consumer of `triaged_at` gains a second,
  must-stay-in-sync source of truth about the same event, closing off today's
  single-field simplicity for good.

These are one-way doors in the sense that matters here: later turns will build atop
whichever shape is chosen, and reversing the choice after votes have accumulated
under it is not a config flip.

## Recommendation

Fix `promotions.py:97` to stop discarding `PromotedArticle.ts`/`.digest_date`, and
change the vote-group click to attribute one vote per underlying article instead of
fanning out to every URL in a cluster (`digest.html.j2:887`) — before building any of
the four modeling/filtering options in Section 6. This is traceable to opt-5's ADOPT
verdict: every one of Section 6's `N` targets (30, 100, 250, or even the already-met
2) is unreachable on any known timeline while the arrival rate stays structurally
unmeasurable (Section 1) and while a vote's attribution to one article vs. a whole
cluster stays ambiguous (Section 2.2) — this is the one concrete next action that
unblocks measuring all of them.

```gate-manifest
{
  "observation_date": "2026-08-09",
  "profile_path_state": {
    "never_generated_as_of_date": true,
    "enable_learning_default_true": "run_digest.py:35",
    "accepted_gate": {
      "threshold": 3,
      "actual_accepted": 3,
      "anchor": "profile.py:88"
    },
    "collect_window_days": {
      "value": 14,
      "anchor": "learning.py:25"
    },
    "structural_insufficiency": "Even after the accepted_count>=3 gate passes, generate_profile_from_triage feeds raw titles into a single unvalidated LLM call with no held-out evaluation step, so the resulting prompt_injection text cannot be checked against future votes before it starts silently steering every scoring call; profile.py never re-generates or invalidates a stale profile, so one contrastive read of five titles would keep steering scoring indefinitely with no drift check."
  },
  "options": [
    {
      "id": "opt-1",
      "name": "Title-to-Prompt Injection (Preference-Profile Text Prompt)",
      "verdict": "defer",
      "required_labels": {
        "N": 30,
        "rounding": 0,
        "inputs": [
          {"name": "required_per_class", "value": 3, "provenance": "measured", "anchor": "profile.py:88"},
          {"name": "classes", "value": 2, "provenance": "assumed"},
          {"name": "triaged", "value": 5, "provenance": "measured", "anchor": "agent-vault/articles/*.json"},
          {"name": "overlap", "value": 1, "provenance": "measured", "anchor": "agent-vault/articles/*.json"}
        ],
        "formula": "required_per_class * classes * triaged / overlap"
      },
      "cost": {
        "unit_prices": [
          {"name": "gemini_in", "value": 1.5, "unit": "USD per 1M input tokens", "source": "cyris.toml:17"}
        ],
        "inputs": [
          {"name": "extra_tokens_per_call", "value": 100},
          {"name": "calls_per_run", "value": 5}
        ],
        "formula": "extra_tokens_per_call * calls_per_run * gemini_in / 1000000",
        "result": 0.00075,
        "baseline_per_run": 0.073
      },
      "handles_news_mismatch": "Prompt injection only reshapes the scoring system prompt, which never runs on news articles, so it cannot use the 4-of-5 news-heavy labels.",
      "uses_pipeline_verdicts": false,
      "requires_local_gpu": false,
      "requires_resident_model": false,
      "seam": "src/cyris/service_layer/ports.py",
      "touches_persistence": false
    },
    {
      "id": "opt-2",
      "name": "Embedding-Based Similarity Reranking (Workers AI bge-m3 + Vectorize)",
      "verdict": "defer",
      "required_labels": {
        "N": 100,
        "rounding": 0,
        "inputs": [
          {"name": "per_class_min", "value": 10, "provenance": "assumed"},
          {"name": "classes", "value": 2, "provenance": "assumed"},
          {"name": "triaged", "value": 5, "provenance": "measured", "anchor": "agent-vault/articles/*.json"},
          {"name": "overlap", "value": 1, "provenance": "measured", "anchor": "agent-vault/articles/*.json"}
        ],
        "formula": "per_class_min * classes * triaged / overlap"
      },
      "cost": {
        "unit_prices": [
          {"name": "bgem3", "value": 0.012, "unit": "USD per 1M input tokens", "source": "https://developers.cloudflare.com/workers-ai/platform/pricing/"}
        ],
        "inputs": [
          {"name": "per_run_articles", "value": 150},
          {"name": "tokens_per_article", "value": 250}
        ],
        "formula": "per_run_articles * tokens_per_article * bgem3 / 1000000",
        "result": 0.00045,
        "baseline_per_run": 0.073
      },
      "handles_news_mismatch": "Reranking runs over the same scored non-news population as the LLM scorer, so it inherits the identical news blind spot as opt-1.",
      "uses_pipeline_verdicts": true,
      "pipeline_verdict_risk": "Bootstrapping the accepted-centroid from score>=70 pipeline rows folds the pipeline's own bias back into the reranker, a self-reinforcing loop per triage_feedback.py:38-39.",
      "requires_local_gpu": false,
      "requires_resident_model": false,
      "seam": "src/cyris/adapters/embeddings.py",
      "touches_persistence": false
    },
    {
      "id": "opt-3",
      "name": "Retrieval-Augmented Generation (RAG) Exemplar Injection",
      "verdict": "reject",
      "required_labels": {
        "N": 250,
        "rounding": 0,
        "inputs": [
          {"name": "per_theme_min", "value": 5, "provenance": "assumed"},
          {"name": "themes", "value": 5, "provenance": "measured", "anchor": "profile.py:19"},
          {"name": "classes", "value": 2, "provenance": "assumed"},
          {"name": "triaged", "value": 5, "provenance": "measured", "anchor": "agent-vault/articles/*.json"},
          {"name": "overlap", "value": 1, "provenance": "measured", "anchor": "agent-vault/articles/*.json"}
        ],
        "formula": "per_theme_min * themes * classes * triaged / overlap"
      },
      "cost": {
        "unit_prices": [
          {"name": "gemini_in", "value": 1.5, "unit": "USD per 1M input tokens", "source": "cyris.toml:17"}
        ],
        "inputs": [
          {"name": "k_exemplars", "value": 5},
          {"name": "tokens_per_exemplar", "value": 150},
          {"name": "calls_per_run", "value": 5}
        ],
        "formula": "k_exemplars * tokens_per_exemplar * calls_per_run * gemini_in / 1000000",
        "result": 0.005625,
        "baseline_per_run": 0.073
      },
      "handles_news_mismatch": "Retrieval only injects exemplars into the non-news scoring prompt, so it shares the identical news exclusion as opt-1 and opt-2.",
      "uses_pipeline_verdicts": true,
      "pipeline_verdict_risk": "Filling the retrieval corpus with pipeline-scored articles means the LLM retrieves its own past verdicts as evidence, the same self-reinforcing-loop risk named at triage_feedback.py:38-39.",
      "requires_local_gpu": false,
      "requires_resident_model": false,
      "seam": "src/cyris/adapters/vectorstore.py",
      "touches_persistence": false
    },
    {
      "id": "opt-4",
      "name": "Rule-Based Negative-Class Filter (Regex on 今彩539-style Titles)",
      "verdict": "adopt",
      "required_labels": {
        "N": 2,
        "rounding": 0,
        "inputs": [
          {"name": "rejected", "value": 2, "provenance": "measured", "anchor": "agent-vault/articles/*.json"}
        ],
        "formula": "rejected"
      },
      "cost": {
        "unit_prices": [
          {"name": "gemini_in", "value": 1.5, "unit": "USD per 1M input tokens", "source": "cyris.toml:17"}
        ],
        "inputs": [
          {"name": "llm_calls_added", "value": 0}
        ],
        "formula": "llm_calls_added * gemini_in",
        "result": 0,
        "baseline_per_run": 0.073
      },
      "handles_news_mismatch": "This filter targets 中央社財經 wire-service titles directly, exactly the population the scorer skips, so it is the only option that can use the 4 news labels.",
      "uses_pipeline_verdicts": false,
      "requires_local_gpu": false,
      "requires_resident_model": false,
      "seam": "src/cyris/service_layer/ports.py",
      "touches_persistence": false
    },
    {
      "id": "opt-5",
      "name": "Signal-Capture Fix: Persist Vote Timestamp and Per-Article Attribution",
      "verdict": "adopt",
      "required_labels": {
        "N": 0,
        "rounding": 0,
        "inputs": [
          {"name": "prerequisite_labels", "value": 0, "provenance": "assumed"}
        ],
        "formula": "prerequisite_labels"
      },
      "cost": {
        "unit_prices": [
          {"name": "gemini_in", "value": 1.5, "unit": "USD per 1M input tokens", "source": "cyris.toml:17"}
        ],
        "inputs": [
          {"name": "engineering_only", "value": 0}
        ],
        "formula": "engineering_only * gemini_in",
        "result": 0,
        "baseline_per_run": 0.073
      },
      "handles_news_mismatch": "This is instrumentation, not a scorer change; it captures digest_date/ts and per-article identity regardless of whether the article is news, preserving rather than losing the mismatch as raw data.",
      "uses_pipeline_verdicts": false,
      "requires_local_gpu": false,
      "requires_resident_model": false,
      "seam": "src/cyris/adapters/promotions.py",
      "touches_persistence": true,
      "sync_protocol_ack": "ports.py:37 declares the SYNCHRONOUS ArticleRepository Protocol; promotions.py:97 already calls it that way, wrapped in asyncio.to_thread at run_digest.py:61, and any ts/digest_date or per-article extension must keep calling it the same synchronous, blocking way."
    }
  ],
  "experiment": {
    "inputs": ["agent-vault/articles/*.json"],
    "null_hypothesis": "The periodic draw-announcement pattern mined from the 2 known rejects is a one-off quirk of this single run, not a recurring category, i.e. fewer than 5 titles in the whole store match it.",
    "threshold": ">=5 matching titles across the whole store rejects the null; <5 fails to reject it.",
    "measured_quantity": "count of stored articles whose title matches the mined draw-announcement regex, a corpus prevalence count, not a scorer-quality measurement",
    "sample_size": 5618,
    "decisions": [
      {"outcome": ">=5 of the 5,618 stored titles match the mined pattern", "decision": "adopt the rule-based filter (opt-4) as specified, using the mined regex as its production rule"},
      {"outcome": "<5 of the 5,618 stored titles match the mined pattern", "decision": "defer opt-4, treat the two rejects as a one-off, and collect more reject labels before automating a rule"}
    ],
    "cannot_conclude": "This measures how common the draw-announcement title format is corpus-wide only; it says nothing about whether the LLM scorer would already down-rank equivalent junk, since news-tagged articles are never scored (run_digest.py:123) so the two populations are structurally disjoint."
  },
  "one_way_doors": [
    {
      "id": "vote_semantics",
      "opens": "Persisting ts/digest_date (promotions.py:17-23) and moving from cluster-fanned to per-article votes opens trustworthy arrival-rate and attribution measurement for the first time.",
      "closes": "Every future label's meaning changes shape at once; votes recorded under the current cluster/no-timestamp semantics cannot be reinterpreted after the fact, closing off options built on the old shape."
    },
    {
      "id": "embeddings_vectorize",
      "opens": "Adopting Workers AI embeddings plus Vectorize opens a whole similarity/RAG surface no current adapter provides.",
      "closes": "It locks in a model version and re-index migration path the moment any article is embedded, closing off a costless model change later."
    },
    {
      "id": "label_record",
      "opens": "A dedicated label record distinct from state+triaged_at opens room for multi-valued/weighted feedback today's two-field scheme cannot express.",
      "closes": "Every reader of StoredArticle and every consumer of triaged_at gains a second must-stay-in-sync source of truth, closing off today's single-field simplicity."
    }
  ],
  "recommendation": {
    "action": "Fix promotions.py to stop discarding PromotedArticle.ts/.digest_date, and move the vote-group click to per-article attribution instead of cluster-wide fan-out, before building any modeling option.",
    "traceable_to": "opt-5"
  }
}
```
