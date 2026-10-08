---
status: accepted
date: 2026-10-08
decision-makers: ""
---

# Each embedding model carries its own measured threshold, and production embeds with `gemini-embedding-001`

## Context and Problem Statement

Vote similarity suppresses a candidate whose cosine to its nearest downvoted title reaches a
threshold, unless it sits closer still to an upvoted one. Two embedding providers are wired
behind `ports.Embedder`: `gemini-embedding-001` and Workers AI `@cf/baai/bge-m3`. Two questions
had to be settled. Where does the threshold come from, and which model does production embed
with?

The cosine scales of the two models differ. `bge-m3`'s cosines run lower than Gemini's across
the board, and their top-10 neighbourhoods agree on only 0.482 of 500 random seeds, so nothing
measured on one model's space transfers to the other.

## Considered Options

* One threshold shared by every provider, set by the reader on `/settings` (grade D)
* A threshold per model, measured on the reader's votes and shipped with the code (grade A)
* Production on `bge-m3`, for co-location with the rest of the Cloudflare deployment
* Production on `gemini-embedding-001`

## Decision Outcome

Chosen option: "a threshold per model, measured and graded A", together with "production on
`gemini-embedding-001`".

**The threshold is a measured property of the model, not a preference.** The calibrated values
are 0.68 for `gemini-embedding-001` and 0.53 for `bge-m3`, and they live in
`src/cyris/provider_defaults.json`. Reusing one number across models does not shift the boundary
a little. Carrying 0.68 over to `bge-m3` suppresses nothing, so the feature silently stops, and
carrying 0.53 over to Gemini suppresses indiscriminately. That is why the threshold is grade A
while the provider and model are grade D: the reader chooses the model, and the threshold is
arithmetic about the model they chose. A model with no calibration gets no borrowed cutoff. The
run skips vote similarity and says why, and `cyris doctor` fails until `cyris.toml` sets one
(2026-10-07).

* **0.68 for Gemini (2026-08-10).** Over the whole 5,724-article store, seeded with the two real
  downvotes, the 71-article lottery class has an in-class minimum of 0.690 and an out-of-class
  maximum of 0.673. At 0.68 there are no false positives and no misses. At 0.62 there are 18 false
  positives, and at 0.70, the previous default, one lottery report is missed. The cut leans high
  inside that 0.017-wide gap because the two errors are not symmetric: a false positive silently
  deletes an article the reader might have wanted, and a miss only lets one through.
* **0.53 for `bge-m3` (2026-08-10).** On the same corpus and seeds it separates the corrected
  class perfectly too, with a gap from 0.5073 to 0.5438. The midpoint fell outside the
  pre-registered 0.65–0.75 band, as was predicted in writing before the run.

**Production embeds with `gemini-embedding-001` (from 2026-09-21).** On 2026-08-10 price,
storage, retrieval quality and data handling all measured flat, and co-location on Workers was
the only reason left for `bge-m3`. By 2026-09-18 the downvotes had grown from 2 in one class to 57
across several, and a re-measurement on 189 human votes (132 up, 57 down), read as AUC with 1,000
paired bootstrap resamples, separated the providers. `gemini-embedding-001` beat `bge-m3`
significantly in every column: +0.034 at 20 seeds, +0.015 with all other votes as seeds and
+0.032 with lottery excluded. Quality was no longer flat, and co-location buys nothing a reader
sees.

### Consequences

* Good, because changing the embedding model on `/settings` cannot silently disable or inflate
  suppression; an uncalibrated model is skipped and reported.
* Bad, because adopting a new model means a calibration on labelled votes before the feature
  works with it. `gemini-embedding-001` retires on 2028-05-14, and Google names
  `gemini-embedding-2` as its replacement, which therefore needs its own calibration.
* Bad, because both thresholds were calibrated against 7 upvote and 2 downvote seeds. An
  absolute cutoff over a growing seed list over-suppresses more with every downvote, so both are
  already stale. Why that follows from the cutoff's shape is recorded below, under *A fixed
  threshold is the wrong shape*. Replacing the absolute cutoff is tracked outside this
  repository and is not part of this decision.
* Neutral, because `cyris.toml.example` keeps `workers_ai`. Vote similarity ships off there, and
  `bge-m3` needs no key beyond the Cloudflare ones a deployment already has.
* Bad, because every measurement below embeds titles only. Content embeddings were never
  measured and may behave better or worse.
* Bad, because the measurements show retrieval, not ranking. That the right neighbours come back
  does not show that a digest reranked by vote similarity reads better; that needs a before and
  after on a real digest, which was never run.

### A fixed threshold is the wrong shape

This was found while collecting milestone M4's receipt, which was "`cyris vote-sim`
at ≈0.53 suppresses the same set it does today". It did not, and the reason was neither the new
provider nor a number that needed re-measuring.

The embedding model is general-purpose and knows nothing about this reader. A vote's only effect
is to add one more title vector to a seed list, and `domain/similarity.max_similarity` takes the
maximum cosine over that list. A maximum over a growing set never decreases, so every downvote can
only raise every candidate's `down_similarity`. A fixed absolute cutoff therefore suppresses more
each time the reader votes, by construction rather than by drift.

Measured on one 168-hour window of 1,112 candidates at a fixed 0.53, varying only the seed cap:

| `max_seeds` | seeds (up / down) | suppressed |
|---|---|---|
| 2 | 2 / 2 | 8 |
| 5 | 5 / 5 | 27 |
| 10 | 10 / 10 | 35 |
| 25 | 25 / 24 | 45 |
| 200 | 101 / 24 | 40 |

Downvote seeds drive suppression up steeply. Upvote seeds claw some back through the
`up < down` guard, which is the only thing keeping it bounded. Both published thresholds were
calibrated against 7 upvote and 2 downvote seeds, and at the time of the measurement there were
101 and 24, so Gemini at 0.68 was over-suppressing too.

The numbers were left at their published values. Re-tuning them to make M4's own receipt pass
would have shaped the check to fit what was built, and a new constant would go stale the same
way for the same reason. The fix is a different shape: a relative cutoff, by rank or by a margin
over the window's own distribution, rather than an absolute cosine.

The field that names the embedder is also part of this record. `vote_similarity.provider` and
`.model` are grade-D keys inside `[vote_similarity]`, not a separate `[embedding]` table, because
the embedder has one consumer and a split would rename keys in every fork's `cyris.toml`.

### The measurements behind the two numbers

Both calibrations ran on 2026-08-10 over the whole store of 5,724 titles, seeded with the
reader's two downvoted titles and judged by the nearest-seed rule. The truth class is 71 lottery
draw reports: the 69 the regex under *More Information* matches, plus two it misses because
neither carries 第N期 (「大樂透頭獎9.1億元1注獨得」 and 「大樂透頭獎連19槓」).

`gemini-embedding-001` at 3,072 dimensions:

| threshold | false positives | lottery reports missed |
|---|---|---|
| 0.62 | 18 | 0 |
| 0.65 | 8 | 0 |
| 0.68 | 0 | 0 |
| 0.70 | 0 | 1 |
| 0.72 | 0 | 2 |

Every title near the boundary is from 中央社財經. The false positives below 0.68 are an adjacent
class the reader never voted on: seven 統一發票千萬獎 titles at 0.657–0.673 and 台股漲/跌
headlines at 0.640. Where the cut falls decides their fate, so the threshold has more leverage
over what is suppressed than the choice of model does.

The two models on the same corpus and seeds. Precision and the first non-lottery rank count
against the regex's 69; `in-min` is the lowest cosine inside the 71 and `out-max` the highest
outside them:

| arm | precision@69 | first non-lottery rank | in-min | out-max | gap |
|---|---|---|---|---|---|
| `gemini-embedding-001`, 3,072d | 1.000 | 70 | 0.6897 | 0.6732 | +0.0164 |
| `gemini-embedding-001`, 1,024d | 1.000 | 70 | 0.6660 | 0.6461 | +0.0199 |
| `gemini-embedding-001`, 768d | 1.000 | 70 | 0.6718 | 0.6525 | +0.0193 |
| `bge-m3`, 1,024d | 0.971 | 68 | 0.5438 | 0.5073 | +0.0365 |

`bge-m3`'s two misses inside the top 69 are the two reports the regex misses, ranked 68 and 69,
so against the 71 it separates the class perfectly too. **0.68 was calibrated at 3,072
dimensions.** At 1,024 and 768 dimensions the in-class minimum above falls below it, so wiring
`GeminiEmbedder`'s `output_dimensions`, which nothing sets today, calls for a calibration first. Data handling did not
separate the providers either: this project's Gemini key is on the paid tier (confirmed
2026-08-10), which does not use inputs to improve Google's products, the same as Workers AI.

The 2026-09-18 re-measurement on 189 human votes (132 up, 57 down). A fixed seed draws 10 up and
10 down as examples and the other 169 are the test set. An article's score is its cosine to the
nearest upvote minus its cosine to the nearest downvote, read as AUC:

| model | 20 seeds | all other votes as seeds | all other votes, lottery excluded |
|---|---|---|---|
| `gemini-embedding-001` | 0.971 | 0.992 | 0.983 |
| `gemini-embedding-2` | 0.956 | 0.986 | 0.969 |
| `bge-m3` | 0.937 | 0.977 | 0.950 |

* `gemini-embedding-001`'s lead over `bge-m3` has these confidence intervals, from 1,000 paired
  bootstrap resamples: +0.010 to +0.066, +0.002 to +0.034 and +0.002 to +0.070, by column.
* `gemini-embedding-2` cannot be told apart from either.
* The task prefixes Google documents (`task: classification`, `sentence similarity`, and 001's
  `CLASSIFICATION` / `SEMANTIC_SIMILARITY`) did not help and were slightly worse.
* Adding the excerpt to the title did not help Gemini (0.992 against 0.991) and helped `bge-m3` a
  little (0.977 against 0.984).

## More Information

The section *A fixed threshold is the wrong shape* was added on 2026-10-08 from
`docs/architecture.md:927-958` and `docs/architecture.md:1068` (§7 row #17) at commit `2b31535`.

The lottery reports in the truth class are the titles this regex matches, plus the two named
under *The measurements behind the two numbers*:

    (今彩539|大樂透|威力彩|雙贏彩|[34]星彩|39樂合彩|運彩).*第\d+期|第\d+期.*(開獎|中獎|槓龜)

Extracted on 2026-10-08 from these sources at commit `c353d3f`. The measurement document was
deleted afterwards; the tables under *The measurements behind the two numbers* are the part of it
this decision rests on:

* `docs/vote-signal-measurement.md:136-169` (*Recalibration — 2026-08-10, full corpus*).
* `docs/vote-signal-measurement.md:198-295` (*Head-to-head: `@cf/baai/bge-m3`* and *They also
  agree on every article ever stored*).
* `docs/vote-signal-measurement.md:297-335` (*Re-measured on 189 votes — 2026-09-18*).
* `docs/architecture.md:505-515` (*Provider defaults, and why these values*, the thresholds).
* `docs/architecture.md:481` (§5, the *Embedding threshold* row) and
  `docs/architecture.md:1058-1063` (the provider reversal).
* The comments at `src/cyris/domain/similarity.py:24-29` and `src/cyris/config.py:316-319`.

Related: [ADR-0005](0005-no-vector-index-and-no-embedding-cache.md) records why no vector is
stored between runs.
