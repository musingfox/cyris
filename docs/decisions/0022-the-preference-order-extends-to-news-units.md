---
status: superseded by [ADR-0023](0023-votes-go-on-topics-and-the-digest-has-three-layers.md)
date: 2026-10-27
decision-makers: ""
---

> **SUPERSEDED**: This ADR has been superseded by [ADR-0023: Votes go on topics, and the digest
> has three layers](0023-votes-go-on-topics-and-the-digest-has-three-layers.md) (2026-10-29). The
> vote on each unit and the unit order below are replaced by a vote on a topic. The code still
> runs this ADR until the tickets in
> [digest-pebble-sand-water.md](../milestones/digest-pebble-sand-water.md) replace it.

# The preference order extends to news units

## Context and Problem Statement

[ADR-0019](0019-the-preference-order-extends-to-news-clusters.md) ordered the news clusters by
their best member's preference before the issue cap, so a reader's votes could save a cluster.
Stage L4 of [digest-by-topic-and-area.md](../milestones/digest-by-topic-and-area.md) replaces the
shape it ordered. The news lane no longer reaches the page as clusters plus one-line headline rows.
Every cluster and every filter-kept headline becomes a *unit*. Headlines the clusterer left alone
are grouped by title cosine (`group_by_cosine`, `domain/news_units.py`). Each area's units are
written as paragraphs, 40 units to a call, and the area quota (`cap_lower_layers`) cuts the
paragraphs, not the issue cap.

The decision ADR-0019 settled has to be restated for that unit: what score a cosine group takes,
and where in the new steps the order is applied.

## Considered Options

* Keep ADR-0019's rule and carry it to the new units unchanged: a unit's score is its best
  member's, and units are ordered before batching and the quota
* Order only the paragraphs after they are written
* Drop the order for news

## Decision Outcome

Chosen option: "keep ADR-0019's rule and carry it to the new units", because the owner decided
on 2026-10-27 that the rule does not change, only the unit it applies to.

* A unit's score is the highest preference among all its members: a cluster's members, or a
  cosine group's headlines.
* Each area's units are ordered before they are batched and before the area quota. The order
  decides which units share a paragraph call and which paragraphs the quota keeps.
* With no votes, with the order off, or with vote similarity off or skipped, every unit keeps its
  incoming order: clusters first in the model's order, then the filter's picks in its order. An
  unjudged unit keeps its slot. The order rejects nothing and stamps no `triaged_at`.
* News is not counted by the issue cap (`max_articles_per_digest_output`): the cap covers the
  read lane, and only each area's quota (`cap_lower_layers`) cuts news. The reason is the
  milestone's N1(c): every unit is written, and the quota, not an earlier cut, decides what is
  shown, so ordering before truncation can only mean ordering before the quota. The owner
  accepted this on 2026-10-27. The order therefore no longer runs inside
  `select_digest_articles` for news.
* News outside every named area, which no quota reaches (under "Other", in an issue with no
  area order, or when the stamp or the paragraph step failed), shares the issue cap with the
  read lane (`cap_unplaced_news`): the read lane's items and that news together count at most
  `max_articles_per_digest_output`, and news in a named area and the follow lane take no slot.
  The owner decided this basis on 2026-10-27.
* The quota cuts in the unit order. However the model orders its paragraphs, each is placed
  where its best-ranked unit stands, and a unit it left uncited keeps its own rank, so a voted
  unit is the last to be cut, not the first.
* A vote on a paragraph targets the whole unit (milestone question N4, option (a), the owner's
  answer on 2026-10-27). Each cited unit has its own vote control, and one vote is cast for
  every member URL of that unit through the same `/api/vote` path a cluster's vote took, so only
  that human vote stamps `triaged_at`.

This ADR supersedes ADR-0019's scope. ADR-0019's rule (the best member stands for the group,
every deployment, no vote-count threshold) still stands; what it ordered no longer exists in a
new issue.

### Consequences

* Good, because the vote order now reaches the paragraphs, which are what a news reader's issue
  is made of after L4.
* Good, because it is still one ordering rule for the whole issue.
* Bad, because news now passes the issue cap whole, so an issue can show more news articles
  than `max_articles_per_digest_output` before the area quota cuts them, and the setting caps the
  read lane only.
* Neutral, because `preference_source = clef` asks about the same articles as before: the
  pipeline already scored every filter-kept headline and every cluster member before the cap
  (`service_layer/digest_pipeline.py`).
* Bad, because the end-to-end test no longer sees the issue cap cut a news article. Its read
  lane fits under the cap, so its check that a cut article stays pending now runs on the area
  quota's cut, and no end-to-end receipt holds an issue-cap cut.
* Neutral, because a vote on a unit is one judgment written to several URLs, as a cluster's
  was: the fan-out risk `clef-preference-order.md` lists for clusters carries over to units.
* Bad, because the paragraph order is the reader's, not the model's judgment of importance:
  the prompt asks the model to keep the units' order, and the code enforces it.
* Neutral, because cosine groups exist only where an embedder does. With vote similarity off and
  no tracked topic, or on a model other than `gemini-embedding-001`, the units are the LLM's
  clusters and every other headline alone. This is a documented limit: building an embedder for
  grouping alone was not done.

### Confirmation

`run_summary` gains `preference_rank_moved_units` in place of `_headlines` and `_clusters`: the
number of unit positions the order changed. `tests/test_news_paragraphs.py` pins that a voted unit leads its area and survives the quota
whatever order the model writes,
that with no votes the units keep their order, and that the order decides which units share the
first batch. `tests/test_html_digest.py` pins that each cited unit's vote group carries every
member URL, the end-to-end check `check_rewritten_ids_match` pins it on a real run, and the
browser probe's `vote-click-news-paragraph` checks that one click posts a vote for each of them.

## More Information

* [architecture.md §3.2](../architecture.md) describes the paragraph step and the order.
* [ADR-0006](0006-embedding-thresholds-are-per-model-calibrations.md) is why the grouping
  cosine is measured per embedding model.
