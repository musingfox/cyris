---
status: accepted
date: 2026-10-29
decision-makers: ""
---

# Votes go on topics, and the digest has three layers

## Context and Problem Statement

The 2026-10-28 morning issue showed a vote group for every source under every topic. A reader
had to open each article to vote, and the page read as too complex
(vault `pm/cyris/docs/digest-pebble-sand-water/baseline-2026-10-28-morning.html`).
[ADR-0022](0022-the-preference-order-extends-to-news-units.md) put that vote on each news unit and
ordered the units by their best member's preference inside an area skeleton
([digest-by-topic-and-area.md](../milestones/digest-by-topic-and-area.md), L1, L3, L4).

The owner redesigned the digest on 2026-10-28 and 2026-10-29. Cyris is the reader's editor: it
selects and explains, and does not analyse. The page does not show where an item came from;
sources are folded away until the reader cares. A reader votes on a *topic*, not on a source or
an article, and caring about one topic (Taiwan's election) must not turn into caring about its
kind (the Paris protests). The design, its open questions and every experiment are in the vault
at `pm/cyris/docs/digest-pebble-sand-water.md` (2026-10-29 version) and the folder beside it; the
plan is [digest-pebble-sand-water.md](../milestones/digest-pebble-sand-water.md). The repo base
for all of it is `9a7fa61b`.

What has to be decided is the unit a vote lands on, how a vote turns into an order, and what
shape the page takes around them.

## Decision Drivers

* A vote must reach what the reader meant (this story) and nothing of the same kind.
* A reader should not have to open an article to vote on it.
* The page selects and explains; it never synthesises one paragraph out of several stories.
* Every reader reads a different amount, so the page has no fixed size.

## Considered Options

* Topics with cross-issue ids carry the votes, and the page has three layers decided by lane and
  member count (chosen)
* Keep ADR-0022: a vote per news unit, units ordered by preference inside each area
* An area skeleton: one section per area, with a quota per area
* Rank by embedding similarity to the voted topics
* Let field and region tags carry preference as well as entity tags
* Let Clef spread a topic's vote to similar topics

## Decision Outcome

Chosen option: "topics carry the votes, and the page has three layers", because it is the only
option the experiments and reviews did not reject. The owner decided the points below on
2026-10-28 or 2026-10-29, and the parent vault card `pm/cyris/tasks/m-digest-pebble-sand-water.md`
lists them, except two that come from the design (§3.4, §4) without a separate ruling: the topic
lifetime `L`, and vote similarity staying as the intake gate.

**Every item is a topic, and the layer is presentation only.** Every article belongs to exactly
one topic, and a topic with one article is still a topic. The layer is decided by lane and by the
member count in this issue alone; a topic's history does not change it, and a score orders items
inside a layer and never moves one between layers:

| Layer | What it holds | Shape |
|---|---|---|
| pebble | one `read` or `follow` article | a card, never split or merged |
| sand | a topic's two or more `skim` articles in this issue | a topic block, with its sources folded |
| water | a topic's one `skim` article in this issue | a row |

Every `read` or `follow` article is its own pebble, even when its topic has other articles: two
`read` articles of one topic are two pebbles, and a topic's `skim` articles form its sand or water
beside them. A pebble links to the sand or water of its topic, and every vote among them goes to
the one topic id. A continuing topic with one `skim` article in this issue is water. A low-scoring
`read` article is still a pebble. A pebble shows its source name, because a `follow` source or a
newsletter is why the reader subscribed and a synthetic `newsletter:` URL has no link; sand and
water name a source only inside the fold (owner, 2026-10-29).

**A vote is on a topic id.** Every layer has one vote group, and it writes to the topic, even
where the topic has one article. The id carries across issues, and each vote's weight halves
every `H` days, starting at 7 (grade D), so a preference returns to neutral without the reader
undoing it. A topic ends when it has had no new article for `L` days, also starting at 7 and
grade D (design §3.4).

**Option I: only entity tags carry preference.** A topic carries 2–5 tags of three kinds: entity
(`Anthropic`), field (`AI`) and region (`日本`). Tags are not votable and match as normalized
exact strings. A vote spreads to the topic's entity tags at a 1/df discount, where df counts the
topics carrying that tag among the topics with a new article in the `L` days before the issue,
a population fixed when the issue is published, so the discount does not drift with the reader's
clock (design §3.3, §3.4). A topic's score is its own decayed votes plus the sum of its entity
tags' preferences, and a topic's own votes are not counted again through its tags. Field and region
tags are shown and may later be searched; they carry no preference. The owner adopted this for
now because a pebble already has its own layer and cannot be drowned out; better ordering for
sand and water is pursued separately (vault ticket `jev-auxiliary-ranking`).

**Embeddings leave the ranking.** The cosine order of `rank_by_preference` no longer orders the
issue. Vote similarity stays as the intake gate it is under
[ADR-0017](0017-suppression-is-a-relative-outlier-with-a-reason-and-a-clock.md). Embeddings'
place is topic assignment: article to article, an article's nearest other article is in the same
topic 88% of the time (78% across issues), vault
`pm/cyris/docs/digest-pebble-sand-water/centering-2026-10-29/notes.md`. That ground truth was
labelled from titles by one person, so the 88% may be high; it is not yet measured on a second
labelling. The one form tested so far, handing the LLM the topic of the nearest article from an
earlier batch, lowered precision (see "Known weakness"), so how embeddings help assignment is
still open.

**No output cap.** `max_articles_per_digest_output`, `max_featured`, `LOWER_ROWS_PER_AREA` and
`cap_unplaced_news` go. The intake cap `max_articles_per_digest` stays: it bounds one run's cost,
not the page's size.

**Clef does not spread topic votes.** Jev, which got every direction right in the same test,
is a candidate small-weight auxiliary for sand and water, not adopted here.

This ADR supersedes [ADR-0022](0022-the-preference-order-extends-to-news-units.md): the unit
vote and the unit order it records are replaced by the topic vote and the order above.

### Known weakness: topic assignment is not yet accurate enough

The whole vote model rests on the topic id. A feasibility run measured `claude-haiku-5-5`
assigning topics from titles alone, with a recent-topics list, to 275 articles of three issues,
against the thresholds set before the calls: pairwise precision ≥ 0.95, pairwise recall ≥ 0.90,
entity-link recall ≥ 0.80. No run met any threshold in either round (vault
`pm/cyris/docs/digest-pebble-sand-water/feasibility-2026-10-29/notes.md`).

* After the owner ruled that two merges the model made were right (the Taiwan stock market,
  the exchange rate and the central bank governor are one topic; the local elections and China's
  pressure on Taiwan are one topic), the LLM-only runs scored precision 0.84–0.88, recall
  0.49–0.61 and entity-link recall 0.55–0.73; 92–96% of articles landed in the right topic
  (`rescore_owner_merges.py`, `article_purity.py` in that folder). Few stories are wrongly
  merged.
* The gap is recall: one story is split into two names. Every second-round run split the local elections into
  "China's interference" and "the local elections". A split topic loses its votes: a vote stays on
  the id it was cast on, and the other name starts from zero.
* An end-of-issue call asking the model to merge synonymous topics returned nothing in 8 of 9
  calls and did not raise recall. In the first round, scored against the original answer key,
  adding each article's nearest earlier article as a candidate lowered average precision from
  0.762 to 0.505.

The owner accepted this state on 2026-10-29 and chose to build on it rather than wait. The
follow-up is vault ticket `topic-assignment-eval`: three new issues labelled by hand, ideally by
the owner or a second person; the owner's granularity rulings written into the prompt as
examples; article text in the input, not titles alone; and the production filter's output shape.
Its result decides the prompt `filter-assigns-topics` ships, or how the thresholds change.

### Still open

These are open in the design (§5) and are not decided here. Each belongs to the ticket named in
the plan.

* Where topics and votes live: topics, topic members, topic tags with their kind, and topic
  votes with the click time each need a row in [architecture.md](../architecture.md) §4 first;
  whether `stories` is rekeyed or a new table is added.
* Whether a topic vote also stamps each member URL's `triaged_at`, and if so how vote-similarity
  seeds are deduplicated by topic so one 15-article topic does not take 15 of `max_seeds`.
* Who writes sand's "what happened": topics form after the filter, so it needs one more call an
  issue. A pebble's explanation is written today by the analytic `SUMMARIZE_SYSTEM`, which has to
  change.
* The filter's batching and criteria: whether batches run in order and pass new topic names on,
  and changing "drop follow-ups with no new information" to "keep every article of a topic".
* Whether Clef stays at all. Only "Clef does not spread topic votes" is decided; Clef as a
  preference source is exclusive with the order above for now.
* How a topic vector is computed, if embeddings remain a safety net: the member mean pulls large
  topics towards every topic.
* An orphaned vote: a story that pauses longer than `L` and returns gets a new id while the old
  one still holds weight. Whether the recent-topics list grows to 21 days is decided in vault
  ticket `topic-store-and-votes`.

### Consequences

* Good, because a vote reaches the topic the reader voted on and, through entity tags only,
  little else: on the 2026-10-28 morning issue, 8 of 49 news topics could receive another topic's
  vote at all (vault `pm/cyris/docs/digest-pebble-sand-water/review-2026-10-29b/summary.md`).
* Good, because the reader votes on what the page shows, without opening an article.
* Bad, because preference does not generalise: a ↓ on the Paris protests leaves a new "French
  strikes" topic untouched unless the two share an entity. This trades generalisation for
  precision on purpose.
* Bad, because the topic id is a single point of failure, and today's assignment splits stories
  (see "Known weakness"). A wrong merge hurts more than a split: at 5% of topics wrong, a merge
  moved an unvoted topic up to 23 places and a split moved one at most 1 (vault
  `pm/cyris/docs/digest-pebble-sand-water/review-2026-10-29b/summary.md`,
  `experiments/split_merge.py`).
* Bad, because a page with no output cap grows with intake; only `max_articles_per_digest`
  bounds it.
* Neutral, because the area skeleton stays in the code for the mail and Discord until the
  tickets that retire it land. In #138 the web page stopped using it, and the news paragraphs
  and the output caps were removed.

### Confirmation

Nothing confirms this yet: the change that records it touches no code. The parent card's
acceptance criteria are the receipt: one production issue published in the three layers, rows in
D1's topic-vote table, and a topic voted ↑ ranking higher in the next issue. The tests that pin
the layer rule and the order arrive with vault tickets `digest-three-tiers-page` and
`topic-store-and-votes`.

## Pros and Cons of the Options

### Topics carry the votes, three layers by lane and member count

* Good, because the vote's target is what the reader sees as one item.
* Good, because the layer rule has no threshold to tune.
* Bad, because it depends on an LLM naming the same story the same way across issues.

### Keep ADR-0022: a vote per news unit, ordered by preference inside each area

* Bad, because a vote group per source under each topic is what made the 2026-10-28 issue read
  as too complex (vault `pm/cyris/docs/digest-pebble-sand-water/baseline-2026-10-28-morning.html`).
* Bad, because a unit lives one issue, so a vote cannot follow a story to the next issue. A
  topic id can: in the preference POC one ↑ on the local elections moved it from 24th to 1st in
  the next morning's issue (vault
  `pm/cyris/docs/digest-pebble-sand-water/preference-poc-2026-10-29/review/report.html` §1).
* Bad, because its cosine preference source leaks, as the embedding option below shows.

### An area skeleton

* Bad, because one story appeared under several area sections in the POCs, so the areas
  themselves made the overlap (vault `pm/cyris/docs/digest-pebble-sand-water/poc1-one-judgment-per-entry.html`,
  `poc2-stories.html`, `poc3-pebble-sand-water.html`; design §6).
* Bad, because both reviewers agreed with the direction, including dropping the area sections
  (vault
  `pm/cyris/docs/digest-pebble-sand-water/review-2026-10-29/comparison.md`, row 1).
* Bad, because its news paragraphs packed several topics into one paragraph (design §2).

### Rank by embedding similarity

* Bad, because a title embedding sees the kind of news, not the story. With similarity on, one ↑
  on the local elections turned 26 of 27 sand topics positive and gave the Paris protests 36% of
  a vote (vault `pm/cyris/docs/digest-pebble-sand-water/preference-poc-2026-10-29/review/real/real.py`,
  `results.json`).
* Bad, because the production outlier cutoff of 3.5 is not reached: the highest z was 2.07, so
  the cutoff turns it off (same source).
* Bad, because the member mean favours large topics: member count and median cosine have a
  Spearman of 0.845 over 118 topics (vault
  `pm/cyris/docs/digest-pebble-sand-water/review-2026-10-29b/experiments/size_bias.py`).
* Bad, because subtracting the mean or the first principal components did not bring it back:
  no variant improved the off-topic percentiles, same-topic retrieval and entity matching
  together. Subtracting the mean pushed the Paris protests from the 75th percentile to the 62nd
  and improved entity matching, but raised the lottery to the 84th; subtracting principal
  components pushed the Paris protests to the 20th but made retrieval and entity matching worse
  (vault
  `pm/cyris/docs/digest-pebble-sand-water/centering-2026-10-29/notes.md`, `centering.py`).

### Let field and region tags carry preference

* Bad, because shared broad tags carry a vote to unrelated stories: one ↑ on the local elections
  lifted the lottery from 23rd to 3rd, through the shared `台灣` tag and the POC's tag-overlap
  similarity together, and one ↓ on Brazil's
  election pushed the local elections down through `選舉` (vault
  `pm/cyris/docs/digest-pebble-sand-water/preference-poc-2026-10-29/review/report.html` §1,
  `breaks.py`).
* Bad, because the discount shrinks the leak from broad tags but does not remove it; with
  similarity off, only dropping their preference (variant B) or counting pairs of shared tags
  (variant C, a second similarity) zeroed it (vault
  `pm/cyris/docs/digest-pebble-sand-water/preference-poc-2026-10-29/review/variants.py`, report §2).
* Good, because a reader's real interest in a field would carry. Rejected for now, with the
  owner accepting that loss.

### Let Clef spread a topic's vote

* Bad, because all four pre-set checks failed: ↓ on Brazil raised Brazil, and ↑ on the local
  elections raised the lottery by 0.61 (vault
  `pm/cyris/docs/digest-pebble-sand-water/clef-2026-10-29/notes.md`, `clef_topics.py`,
  `answers.json`). With no votes two runs were identical, so this is Clef's response, not noise.
* Neutral, because the test gave titles only, while production Clef reads the first 1000
  characters; the version with text is untested.

## More Information

* Supersedes [ADR-0022](0022-the-preference-order-extends-to-news-units.md).
* The plan and its tickets: [digest-pebble-sand-water.md](../milestones/digest-pebble-sand-water.md).
* The design, its open questions and the folder of experiments: vault
  `pm/cyris/docs/digest-pebble-sand-water.md` (2026-10-29 version) and
  `pm/cyris/docs/digest-pebble-sand-water/`.
* [ADR-0017](0017-suppression-is-a-relative-outlier-with-a-reason-and-a-clock.md) is the intake
  gate vote similarity keeps.
