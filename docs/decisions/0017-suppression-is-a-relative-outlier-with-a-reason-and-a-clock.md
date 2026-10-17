---
status: accepted
date: 2026-10-17
decision-makers: ""
---

# Suppression is a relative outlier, with a reason and a clock

## Context and Problem Statement

Vote similarity suppressed a candidate whose cosine to its nearest downvote reached a fixed
number. [ADR-0006](0006-embedding-thresholds-are-per-model-calibrations.md) measured why that
shape is wrong: a maximum over a growing seed list only rises, so every downvote suppressed more
articles on the same window, and no per-model number fixes that. Two further gaps followed from
the same design. A suppression left no record of which downvote it was near, so nobody could
audit it. And a suppression lasted only as long as the run that made it, so the next run
re-judged each candidate afresh and could let it through on a window that happened to lower its
score.

## Decision Drivers

* The count of suppressed articles must not rise with the number of votes alone.
* The cut must not depend on the embedding model's cosine scale, so a model change needs no
  calibration.
* Every suppression must say which downvote it was near and until when it holds.
* A suppression is not a vote: nothing may stamp `triaged_at` or reject the article.

## Considered Options

* Keep the absolute cutoff and re-measure it per model
* A rank cutoff: suppress the top k candidates of each window
* A margin over the window's own distribution, scored by a robust z-score
* The margin, with each suppression written to a table and read back for seven days

## Decision Outcome

Chosen option: "the margin, with each suppression written to a table and read back for seven
days", because a robust z-score adapts to the window's own spread, so a new model or a new vote
does not move the boundary by construction. A rank cutoff would suppress the same number of
articles whatever the window held, which is not what the reader's vote means.

**The cut.** A candidate is flagged when its robust z-score against the other candidates'
downvote similarities exceeds `OUTLIER_MARGIN` (3.5), and it is not closer to an upvote than to
a downvote. The z-score is `0.6745 * (down - median) / MAD`, where MAD is the median absolute
deviation over the window. When the MAD is zero, every candidate's similarity is equal or there
is no downvote seed, and nothing is flagged. A window of fewer than `MIN_OUTLIER_WINDOW` (10)
candidates gets no score and flags nothing: below about ten, the median and MAD rest on two
or three articles, so one article moves the cut. The floor counts candidates, not cosine, so it
does not go stale as votes grow.

**The record.** Each newly flagged candidate writes one `suppressions` row (domain
`suppression.Suppression`, store `adapters/store/suppressions.py`): the downvote it was nearest to,
the two cosines, the outlier score, `reason = topic_near_downvote`, and `until`, the run's start
plus `SUPPRESSION_CLOCK_DAYS` (7). A dry run writes nothing.

**The clock.** Each run reads the holds whose `until` is later than its start, and keeps those
candidates out of the digest even when its own window would not flag them. An expired hold no
longer applies, so a candidate can return after seven days. A candidate honoured from an active
hold is not recorded again. A human action on the article ends the hold's effect early: a
candidate whose `triaged_at` is set, as under `cyris run --force`, is neither dropped nor counted
by its hold.

**Not a vote.** A suppressed article stays `pending` with `triaged_at` NULL. No pass reads the
hold as a human decision, and `_voted` and the blind-label pool never see it as one.

**Failure.** A failed hold read or write is logged and reported in the run summary, and the run
continues on its own judging. An embedding failure does not stop honouring holds.

### Consequences

* Good, because a growing vote set no longer inflates the suppressed count on its own.
* Good, because a changed embedding model needs no cutoff measured for it.
* Good, because every suppression can be traced to the downvote it was near and to its expiry.
* Bad, because a window of fewer than ten candidates cannot produce an outlier, and a larger one
  only when its candidates have spread. Production windows are hundreds of candidates.
* Bad, because a hold of seven days outlives the votes that set it. The clock is a product choice,
  stated as such in `docs/architecture.md` §5.
* Neutral, because `[vote_similarity] threshold` still parses. It is ignored, and `cyris doctor`
  warns when it is set.

## Pros and Cons of the Options

### Keep the absolute cutoff and re-measure it per model

* Good, because it is the existing mechanism
* Bad, because the count rises with every vote, as ADR-0006 measured
* Bad, because each model needs its own calibration before it works

### A rank cutoff

* Good, because it is simple to state
* Bad, because it suppresses a fixed share of every window whatever its contents

### A margin, scored by a robust z-score

* Good, because the window sets its own spread
* Good, because no cosine scale appears in the rule
* Bad, because it needs spread among the candidates to flag anything

### The margin, with a record and a clock (chosen)

* Good, because the decision is recorded beside the verdict it came from
* Bad, because one more table to keep (`docs/architecture.md` §4)

## More Information

The margin's cut is the one Iglewicz and Hoaglin recommend for the modified z-score. The
measurement behind ADR-0006's shape is in [ADR-0006](0006-embedding-thresholds-are-per-model-calibrations.md),
under *A fixed threshold is the wrong shape*. The code is `src/cyris/domain/similarity.py`,
`src/cyris/domain/suppression.py` and `src/cyris/service_layer/run_digest.py`.
