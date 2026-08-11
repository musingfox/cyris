# Vote-Signal: What The Embedding Measurement Actually Showed

> Measured 2026-08-09 with `gemini-embedding-001` (3072-dim, multilingual) over the
> live store. Thresholds were pre-registered before any vector was computed.
> **This supersedes the option ranking in [`vote-signal.md`](vote-signal.md) §6–§8.**
> That document's §1–§5 (label reality, the news/scorer mismatch, cluster attribution,
> the PreferenceProfile ruling, the cost baseline) remain valid.

## Why this ran

Two rounds of analysis reached opposite-but-both-wrong conclusions about whether
embeddings can consume the vote signal:

- Round 1 ranked options by *how many labels they need today*, concluded everything
  needing labels was unreachable, and recommended a hand-written regex.
- Round 2 rejected that frame, but then justified embeddings via an invented
  "routine numeric wire boilerplate" class — a union of five unrelated regexes
  (lottery draws ∪ TAIEX open/close ∪ TWD moves ∪ institutional flows ∪ futures).

Neither round tested the one claim both depended on: **is that class actually an
embedding neighbourhood?** It costs almost nothing to find out, so we did.

## Results

### T1 — The invented five-family class is not one neighbourhood. Confirmed.

Silhouette over the five-family partition: **+0.3116** (pre-registered: ≥0.10 means
distinct clusters). The five families are five clusters, not one. A centroid seeded
from lottery titles cannot reach TAIEX or TWD items, and round 2's central
construction is therefore unsound.

**But that class was the analysis's invention, not the vote's meaning.** The human
downvoted lottery draw announcements. Nothing in the signal asked for the other four
families to come along.

### T2 — The vote's actual class is retrieved perfectly, from two labels.

Seeded with the two real downvoted titles and ranked over all 1,252 中央社財經 titles:

| metric | value |
|---|---|
| precision@69 | **1.000** |
| recall@69 | **1.000** |
| first non-lottery result | rank **70** |

The corrected lottery class is **69** articles. The hand-written regex
`第\d+期.*(開獎|中獎)` found only **50** — it misses every 「頭獎槓龜」 phrasing.
The embedding found all 69 from two examples, with no false positive before the
class boundary.

This inverts round 1's premise directly: the mechanism that supposedly needed
dozens of labels needed two, and it outperformed the rule that was recommended
*because* labels were scarce.

### T3 — It also works on the non-templated case, across sources and languages.

Lottery titles are heavily templated, so T2 alone proves little about real
preferences. The upvotes are the harder test.

Seed: one TechCrunch article on an Amazon data centre's emissions. Top neighbours:

```
 2. [Hacker News]  New Amazon Data Center Is Set to Have the Most Pollu…
 3. [The Verge]    An Amazon data center could have the worst polluting…
 4. [中央社 科技]   為新資料中心供電　亞馬遜投資德州燃氣發電廠
 5. [WIRED]        Two Fossil Fuel Companies Are Betting Big on Data Ce…
 6. [TechCrunch]   New York State halts construction of all new data ce…
 7. [中央社 國際]   反對大舉興建資料中心　全美42州串聯抗議
 8. [Ars Technica] AI firms want more data centers; Trump's EPA may giv…
```

Ranks 2–3 are the *same story* from other outlets — cross-source deduplication as
a free side effect. Ranks 4 and 7 are Chinese coverage retrieved from an English
seed. One click produced a coherent topical cluster spanning seven sources and two
languages.

The other seed (荷莫茲海峽, two Chinese titles from one cluster click) returned ten
further 荷莫茲/伊朗/油輪 stories, all on-topic.

## What this does and does not establish

**Established.** Vote-seeded similarity retrieves the right neighbourhood at the
granularity the human actually voted at — narrower than a feed, wider than one
article — from the labels that already exist. It generalises to phrasings no rule
was written for, and across language and source.

**Not established, and not to be read in:**

- **Retrieval quality is not ranking quality.** That good neighbours come back does
  not show that reranking a digest by vote-similarity improves what the reader sees.
  That needs an actual before/after on a real digest.
- **T4 was underpowered and is unanswered.** Whether similarity can separate analysis
  from boilerplate *inside* one topic (e.g. 「投顧：短線回穩須看3訊號」 vs 「台股開盤跌
  xx點」) had n=2 in the analysis class — far below any level supporting a verdict.
  Measured gap was +0.018, reported for the record only; no decision rests on it.
- **Titles only.** Every vector here is a title embedding. Content embeddings may
  behave differently, better or worse.
- **One reader, one week, 3 clicks.** Two of the three are one cluster click.

## Built, and run against the live store

`cyris vote-sim` previews the effect without touching the pipeline. Over a 168h
window, 1,238 candidates, seeded from the 3 human votes that exist (**run at the
0.70 default of the time**; re-run at today's 0.68 below):

```
WOULD SUPPRESS (12):
  0.916  今彩539第115191期開獎
  0.892  今彩539第115190期　頭獎3注中獎
  0.773  威力彩第115062期　頭獎槓龜
  0.756  大樂透第115077期　頭獎槓龜
  …
```

The window holds 14 lottery-pattern articles, 2 of which are the seeds themselves
(excluded — an article already ruled on must not be re-judged, or it matches its
own seed at 1.0 and reports a decision already made). **12 of 12 recall, no false
positives.**

> **Correction (2026-08-10).** This section originally added: *"Lowering the threshold
> to 0.62 returns the same 12, so nothing non-lottery sits near the boundary; the live
> margin is wider than the calibration implied."* That is true of this 168h window and
> false in general — the window simply contained none of the near-boundary articles.
> Over the whole 5,724-article store, **0.62 suppresses 18 unvoted articles**: the
> entire 統一發票千萬獎 cluster (0.657–0.673) and 台股漲/跌 headlines (0.640). The real
> gap is **0.017 wide**, not comfortable. See "Recalibration" below.

Both generalisations the seeds could not have known are present: 大樂透 and 威力彩
(the seeds are 今彩539 only) and every 「頭獎槓龜」 phrasing, which the mined regex
misses entirely.

Shipped off by default (`[vote_similarity] enabled = false`). It changes what
reaches the digest and the threshold is calibrated on one reader's three votes.

## Recalibration — 2026-08-10, full corpus

The 2026-08-09 pass scored inside 中央社財經 only, mirroring T2. Production
(`judge_by_votes`) scores every pending candidate from every source, so a ceiling
measured inside one source cannot say whether a threshold is safe. Re-measured over
all **5,724** articles, with two lottery reports the regex misses (「大樂透頭獎9.1億元1注
獨得」, 「大樂透頭獎連19槓」 — neither carries 第N期) folded into a **71**-item truth class:

| threshold | false positives | missed lottery |
|---|---|---|
| 0.62 | **18** | 0 |
| 0.65 | 8 | 0 |
| **0.68** | **0** | **0** |
| 0.70 (previous default) | 0 | 1 |
| 0.72 | 0 | 2 |

**The default is now 0.68.** No other source comes near the seeds — the full-corpus
ceiling (0.673) is *lower* than the single-source one, and everything at the boundary
is 中央社財經.

Observed, not just computed: `cyris vote-sim --hours 168` at the new default suppresses
**the same 12 items, all lottery, no false positive** — scores 0.744 to 0.916, with the
next candidate far below. The lowered threshold changes nothing about what this window
does; it buys margin for the two draw reports that carry no 第N期.

The near-boundary set is the finding worth keeping: seven 統一發票千萬獎 titles at
0.657–0.673 are an *adjacent* class — "someone won a large sum in a draw", not a
lottery draw report — that the reader has never voted on. Threshold choice decides
their fate. It has more leverage over what gets suppressed than the choice of
embedding model does.

For the third time, the regex was the thing that was wrong, not the model: the one
item above 0.70 that the regex called non-lottery is a lottery report. The model's
measured precision is understated, not inflated.

## Consequence for the option ranking

Round 1's `opt-rule-filter` ADOPT does not survive: the rule it recommends is
measurably worse (50 of 69) than the mechanism it was preferred over, on the very
class it was written for. Round 2's `opt-embedding-rerank` DEFER does not survive
either — its stated blocker was a validation that has now run.

The label-scarcity framing that drove both rounds was the error. Similarity does not
need a training set; it needs *examples*, and two were enough.

## Reproducing

```bash
uv run --with numpy python <script>   # see the session scratchpad
```

Inputs are the live store (`agent-vault/articles/*.json`) and `GEMINI_API_KEY`.
Total embedding cost for all 5,618 titles was negligible (~170k tokens, one pass).
The corrected lottery ground truth is:

```
(今彩539|大樂透|威力彩|雙贏彩|[34]星彩|39樂合彩|運彩).*第\d+期|第\d+期.*(開獎|中獎|槓龜)
```

Pinning this literal matters: the earlier "193-row class" could not be reproduced —
three independent attempts gave 260, 193 and 188 — which is why it was unfit to serve
as a pre-registered experiment's ground truth.
