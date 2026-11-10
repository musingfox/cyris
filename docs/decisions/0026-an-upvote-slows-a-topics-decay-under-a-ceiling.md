---
status: accepted
date: 2026-11-10
decision-makers: ""
---

# An upvote slows a topic's decay on the recent list, under a ceiling of three lifetimes

## Context and Problem Statement

The filter offers each batch the recent topics, the ones an article of this issue may join
([ADR-0023](0023-votes-go-on-topics-and-the-digest-has-three-layers.md)). Until this decision the
list was every topic last seen in the `digest.topic_lifetime_days` (`L`) before the run, at most
`MAX_RECENT_TOPICS` (300) of them and the most recently seen first. ADR-0023 says a topic ends
after `L` days with no new article, and leaves open an orphaned vote: a story that pauses longer
than `L` returns under a new id while the old one still holds the reader's vote.

The settings page already told the reader "a topic you voted up stays current longer", which was
false: `recent_topic_list` in `service_layer/topic_order.py` added back topics whose decayed vote
was above `RECENT_VOTE_CUTOFF`, but nothing called it, since the rule was not decided (owner,
2026-10-30). The owner's concern was that voted topics pile up until the list means nothing. On
2026-11-10 the owner chose "Rule D+C" from a written evaluation of the candidate rules (its
section 4), with its parameters open to tuning later. The evaluation is not kept in the repo; the
figures below are restated from it.

## Decision Drivers

* The list must stay bounded whatever the reader votes, so voted topics cannot crowd out live ones.
* A vote's effect on the list should fade as votes do, by the vote half-life `H`, which the owner
  trusts more than a fixed extension.
* A vote cast on an archived page lands on its article's topic like any other, and must not revive
  a story long gone.
* With no vote the list is what it was.

## Considered Options

* D+C: an upvote stretches the lifetime by 2 to the power of the decayed vote, floored at no
  stretch and capped at `K` lifetimes (chosen)
* C alone: an upvoted topic stays `K` lifetimes after its last article
* D alone: the same stretch with no ceiling
* D+C with a downvote that shortens a topic below `L`
* Wiring `recent_topic_list` as it stood

## Decision Outcome

Chosen option: "D+C", because it is the only option that both fades with the vote and bounds the
list independently of the votes.

Let `p` be the topic's net vote at the run's start, each vote halving every `H` days, as
`topic_preferences` in `domain/topic_rank.py` computes it, floored at 0 for this purpose. A topic
is on the recent list while the days since its `last_seen` are less than
`min(L × 2 ** p, K × L)`, with `K` = `MAX_TOPIC_LIFETIMES` = 3 in `domain/topics.py`, grade A. With
no vote that is `L`, the rule before this decision. The base 2 is structure: one fresh upvote
doubles the lifetime, and only `K` is a setting. `topic_expiry` and `recent_expiry` in
`domain/topics.py` hold the rule; `recent_topic_list` reads the `K × L` days before the run and
keeps what the rule holds, and `run_digest` offers that list to the filter.

* The 300 cap keeps the topics that stay longest: `TopicBook` sorts by each topic's expiry, the
  moment the rule drops it, latest first, and of two leaving together the more recently seen first.
  A name two listed topics share joins the one that stays longest. With no vote the expiry is
  `last_seen + L`, so the order is the one before.
* A vote's stretch fades because `p` does. At `L` = `H` = 7 one upvote cast at the topic's last
  article keeps it 9.24 days, and only 13 or more such upvotes reach the ceiling of 21 days.
* An upvote cast on an archived page counts like any other, but the ceiling is from the last
  article, so no vote keeps a topic past `K × L` days after it last appeared.
* The list before the cap holds at most the topics of `K × L` days, however much the reader votes.
  `K` = 3 is a starting point, not a measurement: the only figure on record is 21 days at `L` = 7,
  an inference in the 2026-10-29 review, not a measured horizon.
* Only the list changes. The population a shared entity tag is discounted over stays the topics
  last seen in `L`, and `RECENT_VOTE_CUTOFF` still bounds which voted topics the order looks up.

### Consequences

* Good, because the list's bound depends on topic volume and `L`, not on the reader's votes.
* Good, because a vote's effect on the list fades with `H`, like its effect on the order.
* Good, because an archived page's vote cannot revive a topic past `K × L`.
* Bad, because one upvote at the defaults buys 2.24 more days, so a story that pauses 10 days, the
  one measured example in the 2026-10-29 review, returns under a new id unless the reader voted
  more than once or it kept appearing.
* Bad, because the list now moves with `H`: a reader who shortens the half-life to make the order
  react faster also shortens the stretch.
* Neutral, because the run reads `K` lifetimes of topics, with their tags, to keep the ones the
  rule holds.

### Confirmation

`tests/test_topic_decay.py` pins the rule at `L` = `H` = 7: no vote leaves at `L`, one fresh upvote
stays past `L` and leaves before `K × L`, many upvotes leave at exactly `K × L`, a downvote leaves
at `L`, and an archive upvote on a topic last seen `2 × L` ago does not keep it past `K × L`.
`tests/test_topics.py` pins that the cap keeps a vote-extended topic over a fresher one leaving
sooner, and that `K` has its row in `docs/architecture.md` §5.
`tests/test_filter_topics.py` runs `run_digest` and asserts the filter is offered the extended
topic and not the expired one or the one past the ceiling.

## Pros and Cons of the Options

### D+C

* Good, because it keeps C's bound and D's fading.
* Bad, because it adds a grade A constant whose value is not measured.

### C alone

* Good, because its day boundaries are exact: `last_seen` plus `K × L`.
* Bad, because one upvote buys the whole `K × L` whatever its age, so the vote's effect on the
  list does not fade, which is the half-life the owner preferred.

### D alone

* Good, because a vote's effect fades with `H`.
* Bad, because no vote count reaches a slowest decay: the horizon rises about one `H` per
  doubling of the votes, so only the 300 cap bounds the list.
* Bad, because `n` fresh upvotes on an archived page revive a topic last seen up to `L × 2 ** n`
  days ago, with no new article.

### A downvote that shortens a topic below `L`

* Good, because it is the literal reading of a vote that speeds decay up.
* Bad, because a topic that leaves early cannot be joined, so the downvoted story's next article
  opens a new topic with no votes, and the downvote stays on the old id. The downvote would make
  the story come back sooner, under an id that does not carry it. Floored at 0, a downvote cancels
  an extension and never shortens a topic below `L`.

### Wiring `recent_topic_list` as it stood

* Bad, because its horizon follows `H`, not `L`: one upvote kept a topic while its vote was above
  0.1, about 3.32 × `H` after the click and 1,212 days at `H`'s ceiling of 365, and each doubling
  of the votes added another `H`.
* Bad, because the 300 cap would undo it on a busy week: its extra topics were last seen before
  the lifetime, so the newest-first sort put them last and the cap dropped them first.

## More Information

* Supersedes in part [ADR-0023](0023-votes-go-on-topics-and-the-digest-has-three-layers.md): its
  sentence "A topic ends when it has had no new article for `L` days". A topic with no vote still
  ends there; an upvoted one ends by the rule above. The rest of ADR-0023 stands.
* The help sentence on `/settings`, onboarding, the prototype and `settings.example.toml` says the
  same: an upvote slows how fast a topic leaves the list new articles can join, the effect fades
  with the vote half-life, and never past three times the lifetime after the topic last appeared.
