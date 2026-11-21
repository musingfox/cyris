---
status: accepted
date: 2026-11-21
decision-makers: ""
---

# A hosted tenant's AI spend is metered in the registry, and a spent month stops the next run's AI

## Context and Problem Statement

The closed beta charges readers nothing, and the operator pays for every model call. Each tenant
needs a monthly AI allowance, a ceiling on what one reader can cost. The card set the first
allowance at 2.25 USD a month. That is enough for Haiku 5.5 to make two issues a day of 150
articles each, with every article kept. A run on production on 2026-11-13 averaged 68,393 input
and 20,052 output tokens, about 0.0168 USD.

What the system recorded before this card was not enough to enforce an allowance:

* `usage_log` is written only when a run succeeds (`run_digest`, after the pipeline). A run that
  raised after its model calls left no record of them.
* The run's `UsageStats` is summed from the stages that finished, so a failure loses the calls of
  the stage that raised.
* Embeddings were never priced. Gemini's `batchEmbedContents` reports no token count.
* `usage_log` has no tenant column. In hosted mode each tenant's `usage_log` is in its own Durable
  Object, which its run container can rewrite with any SQL (ADR-0028). So a tenant's own tables
  cannot be the authority on its spend, just as they could not be the authority on its schedule
  (ADR-0032).

The registry already had an empty `spend_rollup` table that ADR-0030 created for this card.
Several questions were open. Where the spend and the allowance live. How a run reports spend,
including a run that fails. When a run is checked against the allowance. What a run that finds
its month spent does.

## Decision Drivers

* A run container must not be able to raise its own allowance or erase its own spend.
* Spend must be counted as it is made, so that a failed run's calls are counted too.
* A spent allowance must still give the reader an issue, without AI, that says why.
* There must be no new Cloudflare product, binding or external service.
* The core must not change beyond optional `Deps` callables whose None changes nothing
  (`docs/architecture.md` §8).
* The operator must be able to list every tenant's month in one request.

## Considered Options

* The registry holds the allowance and the monthly spend. The run reaches it through an
  intercepted gateway, is checked as it starts, and reports a per-call meter as it ends.
* The run writes its spend to its own `usage_log`, and the Worker sums it.
* The LLM calls go through the hosted Worker, which meters each one.
* Check the allowance before each model call, not once per run.

## Decision Outcome

Chosen option: "the registry holds the allowance and the monthly spend, reached through an
intercepted gateway". This is the only option where no container can write its allowance, that
counts a failed run's spend, and that adds no product.

How it is built:

* **The ledger is the registry's.** `spend_rollup` keeps one row per tenant and UTC month. It
  gains `llm_usd`, `embedding_usd`, `clef_usd`, `input_tokens`, `output_tokens` and
  `unpriced_tokens` beside its `cost_usd` (the sum of the three dollar columns) and `runs`.
  `tenants` gains `monthly_ai_budget_usd`, the allowance the operator set, NULL until then. A
  NULL allowance is `DEFAULT_MONTHLY_AI_BUDGET_USD` (2.25) as the code reads it now. The column
  has no SQL default, which would freeze 2.25 into every row: a later release that changed the
  constant would then change no tenant, and a set allowance could not be told from the default
  (`budget_set`). All of these are added in place, as ADR-0032's columns were
  (`workers/app/src/ai_budget.js`).
* **A month is UTC `YYYY-MM`.** The allowance is the operator's loss ceiling, and providers bill
  in UTC months. One month for every tenant also lets one query list all of them. A month with no
  row has spent nothing, so every allowance comes back at 00:00 UTC on the first of the month,
  with no job to run. A run is charged to the month its report arrives in.
* **The run reaches the ledger only through `SpendGateway`.** It is registered for `spend.cyris`
  before every `start()`, bound to the tenant by `props`, like `db.cyris` and `ops.cyris`. It
  answers `GET /t/<own tenant>/budget` (the month and whether it is spent) and
  `POST /t/<own tenant>/spend`, which adds one run's report. A report with a negative or
  non-numeric field is refused. The gateway can only add to the tenant's own month. It cannot set
  an allowance or read another tenant.
* **Every call is metered as it is made.** In a hosted run (not on the operator's machine),
  `build_deps` wraps the LLM client in `MeteredLLM`, the embedder in `MeteredEmbedder` and Clef
  in `MeteredClef` (`src/cyris/adapters/hosted_spend.py`).
  * Each LLM response is priced at its model's rate in `domain/models.py`. A Workers AI response
    is priced by its neurons.
  * A model with no rate card is charged at the dearest input and output rates that table holds
    (`UNPRICED_USD_PER_MTOK`), and its tokens are also counted as `unpriced_tokens`, so the
    operator sees which spend is a guess. Charging it nothing would give its tenant AI without
    limit, since the allowance compares dollars. Refusing to run its AI was the other choice. It
    was turned down because it costs the reader their issue's AI for a gap in a table that the
    operator closes with one rate card, where the dearest rate only spends the allowance sooner.
  * A response that was billed and then raised on is counted too: a Gemini model that spent its
    whole output budget thinking, or a blocked prompt. The error carries the response's usage
    (`BilledError` in `adapters/gemini_client.py`).
  * A Clef question is priced at `clef-flash`'s rate per input token, from the tokens its answer
    reports. When it reports none, or the call raised, the tokens are estimated from the bytes
    it sent. A tenant whose `digest.preference_source` is `clef` spends Workers AI on every run,
    and an unmetered call would never reach its month.
  * Embedded texts are counted batch by batch, including in a call that raised. Gemini reports no
    tokens, so they are estimated at one token per three UTF-8 bytes. That is about right for
    Chinese and about a third high for English: a ceiling should err high. They are priced at
    gemini-embedding-001's list price.
* **The run is checked once, as it is wired.** `bootstrap.build_deps` asks the ledger before it
  wires anything. If the month is spent, it wires no LLM, embedder or Clef, so the run takes the
  path a missing client takes and makes the plain list. The `HtmlDigestWriter` it wires carries
  an `AiPause`, so the page of every issue that run starts says the allowance is used up and
  when it returns, in place of the degraded notice. An allowance of 0 is always spent, and no
  new month brings it back, so its page says the tenant's AI is off and promises nothing.
  `run_summary`'s `ai_spend` records `ai_budget` as `spent` or `off`.
* **The run reports as it ends, failed or not.** `run_digest`'s `finally` calls
  `deps.record_spend`, which posts the meter to the ledger with the store's retry rules and puts
  it in `run_summary` as `ai_spend`. So `digest_runs.summary` holds the same numbers for every
  run. Those rules retry a timeout or a 5xx, which can come after the registry added the
  report, so each report carries an id the run draws once (`report_id`). The registry adds one
  id once per tenant, keeping it a day in `spend_reports`. The queue's run id and attempt would
  name the same thing, since each attempt is one container start and one process, but the
  container is not told them, and a start the queue did not make would have none.
* **The operator reads and sets it.** `GET /hosted/spend[?month=YYYY-MM]` lists every registered
  tenant's month, and a tenant that spent nothing is listed with zeros.
  `POST /hosted/tenants/<tenant>/budget?usd=N` sets an allowance from 0 to
  `MAX_MONTHLY_AI_BUDGET_USD` (100), checked against from the next run. The tenant's report shows
  its month. `scripts/hosted.sh spend [MONTH]` and `budget TENANT USD` call both routes. Both
  routes are behind Access, and the budget route refuses another site's post.

### Consequences

* Good, because no container can raise its allowance or lower its spend. It can only add to its
  own month, through a gateway bound to it.
* Good, because a failed run's calls are counted. The meter counts each response, not each stage
  that finished.
* Good, because a tenant whose month is spent still gets its issue, and the page says why.
* Good, because the month resets with no job: a new month simply has no row yet.
* Bad, because a run is checked only as it starts. The run that crosses the allowance finishes
  with its AI. With two runs a day, the overshoot is at most one run, about 0.02 USD at today's
  measured size. A per-call check would cost a gateway round trip per call. It would also leave
  half-scored issues, which the reader cannot tell from a fault.
* Bad, because the ledger fails open. A run that cannot read its month may spend, and a run
  whose report is lost leaves the month's total short by what it spent. Failing closed would
  cost every tenant its AI for as long as the ledger is down, which was judged worse than one
  run's spend. So that an outage cannot pass unseen, which would leave every tenant spending
  without limit, the run then sends the operator one alert (`ai_ledger_unreachable`) through
  its operator gateway (ADR-0035): one per run, however many of its ledger calls failed.
* Bad, because a run killed before its `finally` (OOM, SIGKILL) reports nothing. Its spend is
  lost from the ledger. The run object's abnormal-stop alert still tells the operator that the
  run died.
* Bad, because the embedding cost is an estimate. Gemini reports no tokens, so it cannot be
  measured from the response. The card bounds it at about 0.17 USD per tenant per month.
* Neutral, because the meter wraps the LLM client and embedder only in a hosted run.
  Self-hosted runs, and the operator's `cyris --tenant` commands, are unchanged.
* Neutral, because the core changes by one optional `Deps` callable, `record_spend`, which is
  None outside a hosted run, and then nothing changes (`docs/architecture.md` §8). The pause is
  all wiring: `build_deps` leaves the clients out and words the page through the writer.
* Bad, because the core cannot tell a paused run from one whose provider key is missing. Its
  `digest_runs.degraded` is 1, and its log carries the missing-client warning after the line
  `build_deps` writes about the allowance; `ai_spend.ai_budget` is what tells the two apart.
  And the notice is not stored with the issue: a later re-render of it, which in hosted mode
  only a failed publish's republish makes, shows the degraded notice. Storing it would need a
  `DigestContent` field, which is the core change §8 rules out.
* Bad, because spend outside a run is not metered: the settings page's model calls and the
  Worker's Gemini probe. A hosted tenant has no settings page yet. When it gets one, its calls
  must go through the same meter and ledger.

### Confirmation

* `workers/app/test/hosted_spend.test.js` checks four things.
  * The registry keeps each tenant's month apart and lists every tenant, with zeros for one that
    spent nothing. The allowance is spent once the spend reaches it, and an allowance of 0 is
    always spent.
  * A new UTC month starts every allowance again and keeps last month's row. A report that would
    credit the month is refused, and an old registry gains the columns in place.
  * `SpendGateway` answers only its own tenant's two routes. A report its run retried after the
    registry added it is added once.
  * The operator's list, month filter, budget route, cross-site refusal and report.
* `tests/test_hosted_spend.py` checks a hosted run end to end over the fake gateways.
  * Its LLM, embedding and Clef spend are charged once to its own tenant. A model with no rate card
    is charged at the dearest one.
  * A run whose month is spent calls no LLM and no embedder. It still stores and publishes the
    plain list, and its page carries the allowance notice and not the degraded one. An allowance
    of 0 says AI is off, never that the month brings it back.
  * A run that raises after its model calls still charges them, and so does a Gemini or AI
    Gateway response that was billed and then raised on.
  * A ledger that cannot be read lets the run spend and still charges it, and the operator gets
    one alert for the run, however many of its ledger calls failed.
  * Only a hosted run is metered.
* `tests/test_hosted_script.py` checks the `spend` and `budget` subcommands.
* The human's check on the trial account is in `docs/hosted-tenants.md`: run a tenant, read its
  month with `scripts/hosted.sh spend`, set its allowance to 0, and see the next issue's notice.

## Pros and Cons of the Options

### The registry holds it, reached through an intercepted gateway

* Good, because it is the registry ADR-0030 named for this, strongly consistent, out of every
  container's reach.
* Good, because it reuses the interception pattern of `db.cyris` and `ops.cyris`, with no new
  binding.
* Bad, because the registry takes two more calls per run.

### The run writes its own `usage_log`, and the Worker sums it

* Good, because `usage_log` already exists in every tenant's database.
* Bad, because the container can rewrite its own database, so the spend it holds is the
  tenant's to erase.
* Bad, because the list across tenants would need one read of every tenant's object.

### The LLM calls go through the hosted Worker

* Good, because the Worker would see every token, and a container could neither under-report
  nor skip its report.
* Bad, because every provider client would need a Worker route, which means a proxy for
  Anthropic, Gemini, OpenAI and Workers AI. That is a much larger change than the card.
* Neutral, because Cloudflare AI Gateway could be that proxy. It is a product decision and a
  per-deployment setup step, and the card did not ask for one.

### Check before each model call

* Good, because the allowance would never be passed by more than one call.
* Bad, because each call would pay a round trip, and an issue cut mid-stage would be half
  scored or half summarised, which reads as a fault.

## More Information

* `docs/architecture.md` §2 names the two callables. §4's registry row describes the ledger, §5
  grades the allowance, the bounds and the prices, and §6 describes the gateway. §8 lists the
  callables.
* The registry is [ADR-0030](0030-the-operator-registry-is-one-sqlite-durable-object.md). The
  gateway pattern is [ADR-0028](0028-one-sqlite-durable-object-per-hosted-tenant.md) and
  [ADR-0035](0035-a-hosted-runs-alerts-go-to-the-operator-through-the-hosted-worker.md). The
  daily run cap, the other limit the registry holds, is
  [ADR-0032](0032-hosted-runs-wait-in-a-queue-the-registry-holds.md).
* The task card is `hosted-ai-budget-meter` in the owner's Obsidian vault.
