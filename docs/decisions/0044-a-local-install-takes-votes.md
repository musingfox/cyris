---
status: accepted
date: 2026-11-24
decision-makers: ""
---

# A local install takes the pages' votes in its own server, and applies them at once

## Context and Problem Statement

A local install serves its digest and raw pages from `cyris triage-ui` on a SQLite file
([ADR-0025](0025-a-local-install-keeps-its-state-in-sqlite.md)). Until now those pages had no
vote: the server answered the pages' probe of `/api/vote` with `{"authorized":true,"votes":false}`,
which keeps the vote buttons, the raw page's views and the Interest link closed, and routed no
`POST`. The reader voted only in the onboarding wizard or with `cyris articles accept|reject`.
`docs/install-local.md` listed it under "What a local install cannot do".

The cloud edition's vote goes from the page to the app Worker's `/api/vote`, on to the promote
Worker's KV queue, and the next run's `promote-sync` applies it (`apply_promotions`,
`src/cyris/adapters/promotions.py`). A hosted tenant's goes to its own `hosted_votes` table and its
next run applies it with the same function
([ADR-0039](0039-a-hosted-vote-is-queued-by-the-worker-and-applied-by-the-run.md)). Milestone M4 of
the edition-parity plan, which the owner approved on 2026-11-24, asks that a local install take
votes from the same pages exactly as the cloud does: the buttons and the Interest link show, and a
👍 stamps the article's `triaged_at` and state as a cloud vote does.

The question is where a local vote goes, and who writes what it means.

## Decision Drivers

* One writer of what a vote means. Only a human's action may stamp `triaged_at`, and the topic
  vote and its one-click window are subtle (`topic_votes`, `unrecorded`); ADR-0039 kept them in
  one Python function for that reason.
* The pages stay one rendering. A page must not need to know which edition serves it, so the
  local server answers the same request with the same answers as the app Worker.
* A local vote must land in the tables and columns a synced D1 vote lands in, so vote similarity,
  the vote order and the interest map read it without a branch.
* No Cloudflare account (ADR-0025), and no new home for state (`docs/architecture.md` §4).
* A local install has no sign-in (ADR-0025): only a page this server served may write to it.

## Considered Options

* The local server answers `/api/vote` as the app Worker does and hands each vote to
  `apply_promotions` at once
* The local server queues each vote in a table of the file, and the next run drains it, as a
  hosted run drains `hosted_votes`
* Run the promote Worker locally (Miniflare or `wrangler dev`) and point `promote-sync` at it
* Keep the pages' votes off, as before

## Decision Outcome

Chosen option: "the local server answers `/api/vote` as the app Worker does and hands each vote to
`apply_promotions` at once", because the server already is the run's Python on the run's own file:
the queue every other edition has exists to carry a vote from a Worker that cannot run that
Python to a run that can, and here there is nothing to carry it across.

**The route.** `TriageServer` answers `/api/vote` on every method, as `handleVote`
(`workers/app/src/router.js`) does, when its article store is the local SQLite file and it is
not behind the app Worker. `GET` and `HEAD` answer `{"authorized":true}`, the Worker's answer, so the probe opens the
vote buttons and every `.gated` control. A `POST` takes the body a page sends (`{url, vote,
digest_date}`), refuses what the promote Worker refuses, with its words (`missing url`,
`bad vote`), reads no vote as `deep`, as the promote Worker does, and answers `{"ok":true}`. Every
other method is `405`. `workers/app/test/vote_cases.json` holds these answers, and both suites
read it: `workers/app/test/vote_contract.test.js` drives the app Worker over the real promote
Worker, and `tests/test_local_votes.py` drives this server on a SQLite file.

**One writer.** The vote becomes the `PromotedArticle` the promote Worker would have queued, timed
by the server's clock as the Worker times its entry, and goes to `apply_promotions` with the
article store and the run's topic store on the same file. The stamp, the state, the
rejection reason and the topic vote, with its one-click window across a sand block's per-URL
posts, are written by the code that writes a synced or a hosted vote. A URL the store lacks is
answered `{"ok":true}` and stamps nothing, as the cloud's sync acknowledges it and stamps nothing.

**At once, not at the next run.** A queue would add a table to the file and a row to §4, and a
drain to every run, to buy a delay nobody wants. `hosted_votes` stays hosted-only, as
`hosted_schema.sql` says it should. The file takes two writers, the server and a run, as ADR-0025
measured, and a run's `update_states` leaves a stamped row alone, so a vote cast during a run
stands. A vote cast after a run loaded its candidates reaches the next run, as a cloud vote synced
before the next run does.

**Who may vote.** The server's Host and Origin check, which already guards every write, is the
whole gate: a page another site serves, or one on a rebound name, is refused `403` before the
route. A client that sends no `Origin`, such as curl on the same machine, may vote, as it may
already change every setting. Opening the server to other devices with `--host` opens voting with
it; that remains the reader's own choice, with no integration.

**Behind the app Worker** the container's server routes no vote `POST` and keeps answering its
probe `votes:false`; the Worker answers `/api/vote` itself and no request for it reaches the
container. A server with no article store answers `votes:false` too, so the probe script's branch
for it stays and is now only that server's answer.

**On D1, not behind the app Worker** (a `cyris triage-ui` run on a host with `[store] backend =
"d1"`) the server takes no vote either: it answers its probe `votes:false` and routes no `POST`,
and the vote stays the cloud's, through the app Worker and the promote queue. Two reasons. A D1
write is several synchronous REST round trips (`D1Client`), so each click would hold the
server's event loop for all of them, where a SQLite write is a local lock. And the cloud's queue
still drains into the same D1: `apply_promotions` stamps a synced vote with the sync's time, so a
vote queued before a direct one here would be applied after it and stamped later, and the older
vote would win. On the SQLite file no queue feeds the store, so neither arises.

### Consequences

* Good, because a local install's pages show the vote buttons, the raw page's views and the
  Interest link, and a vote on any issue, an earlier version's included, is taken.
* Good, because one Python function still writes every vote of every edition, and a local vote
  lands in the same `stored_articles` columns and `topic_votes` rows a synced D1 vote does, so
  the next run's vote similarity reads it the same way (`tests/test_local_votes.py`).
* Good, because no table, setting or home is added: §4 gains a writer, not a row.
* Good, because a local vote takes effect at once, and its `triaged_at` is the click's time,
  where a cloud vote's is the sync's (`docs/architecture.md` §3.2).
* Bad, because anything on the machine that can send an HTTP request without an `Origin` can
  vote. It can already change the settings and spend the LLM key through `/settings`, so this
  adds no new reach, but it is no sign-in.
* Bad, because the server writes the store while a run may be writing it. ADR-0025's two-writer
  test covers the file; a vote's write runs on the server's event loop, as `/labels`' does, so the
  server waits for SQLite's lock during it.
* Bad, because the local route is a second copy of the promote Worker's request checks, in
  Python. The case file is what keeps the two equal; a check added to one without a case drifts.
* Neutral, because the two editions still differ where no page sends the request: a body that is
  not JSON is the app Worker's `502` (it parses before forwarding) and this server's `400
  missing url` (it reads it as the promote Worker does), and a `url` that is not a string, which
  the promote Worker queues, is refused here.

### Confirmation

* `workers/app/test/vote_contract.test.js` and `tests/test_local_votes.py` run every case of
  `workers/app/test/vote_cases.json` against the app Worker and against `TriageServer` on a
  SQLite file; the second reads each row back through its own connection to the file.
* `tests/test_local_votes.py` also holds that an upvote stamps `triaged_at` and accepts, a downvote
  rejects as `not_interested`, a change of mind ends on the later vote, a sand block's three posts
  are one topic vote, a write from another origin, a cross-site fetch or a foreign Host stamps
  nothing, a server behind the app Worker, on D1 or with no store takes no vote, and a failed write
  answers `500`. Its last test gives the same clicks to this server and, through the real
  `sync_promotions`, to a second file as the promote Worker would have queued them, and requires
  the same verdict columns, the same topic votes and the same vote-similarity seeds and
  suppression from both.
* `tests/test_local_pages.py` runs the pages' real probe script on this server's answer: with a
  store it opens the vote buttons and the gated controls; without one it leaves them closed.
* `tests/test_cli_triage_ui.py` holds that `cyris triage-ui` gives the server the topic store on
  the article store's file, and that the server it builds takes the pages' votes on a SQLite
  store and answers `votes:false` and refuses the `POST` on D1.
* Not verified here: a click in a real browser on a running `cyris triage-ui`. The sandbox this was
  written in binds no port; it is the first check after merge.

## Pros and Cons of the Options

### The local server answers as the app Worker does and applies at once

* Good, because the pages, the write rule and the tables are all unchanged.
* Good, because it needs no queue, drain or table.
* Bad, because the request checks exist twice, in JavaScript and Python, held equal by a case file.

### The local server queues, and the next run drains

* Good, because it has ADR-0039's shape exactly.
* Bad, because it adds `hosted_votes`, or a twin of it, to a file that `hosted_schema.sql` keeps
  it out of, and a §4 row, for a vote that waits for no reason.
* Bad, because a vote cast while no run is due waits up to half a day to count.

### Run the promote Worker locally

* Good, because the vote path would be the cloud's, byte for byte.
* Bad, because a local install would need Node, Wrangler or Miniflare and a token beside the
  Python it already runs, which ADR-0025's edition exists to avoid.

### Keep the votes off

* Good, because it costs nothing.
* Bad, because it leaves the parity milestone undone: the reader votes only in the wizard or the
  CLI, and the Interest link stays hidden.

## More Information

* Extends [ADR-0039](0039-a-hosted-vote-is-queued-by-the-worker-and-applied-by-the-run.md): its
  one writer, `apply_promotions`, now writes a third edition's votes, with no queue in front.
* Amends the consequences of [ADR-0025](0025-a-local-install-keeps-its-state-in-sqlite.md): votes
  on the pages are no longer a cloud-only feature, and the Host and Origin check now gates them.
* `docs/architecture.md` §3.2 says when each edition's vote takes effect and what its stamp's time
  is, §4 names this server among the writers of `topic_votes`, and §6 has the probe's answers.
  `docs/install-local.md` no longer lists votes among what a local install cannot do.
