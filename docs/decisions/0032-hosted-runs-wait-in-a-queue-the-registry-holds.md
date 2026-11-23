---
status: accepted
date: 2026-11-20
decision-makers: ""
---

# Hosted runs wait in a queue the registry holds, on the registry's schedule

> Superseded in part by [ADR-0042](0042-a-hosted-tenant-makes-one-digest-a-day-unless-allowed-two.md)
> (2026-11-24). A tenant's `digest_times` is one whole hour, its morning issue, or two when its
> new `tenants.max_digest_times` allows them; the default daily cap is 3 (`1 × CATCH_UP_HOURS`),
> not 6; `max_instances` and `RUN_SLOTS` are 4, not 2; and the seed gives a tenant with no
> schedule one hour of its own, so the schedule route is no longer a step before the tick runs it.
> The rest of this ADR still stands.

## Context and Problem Statement

The hosted Worker `cyris-hosted` runs every tenant's digest from one container application,
whose `max_instances` is 2 (`wrangler.hosted.toml`). Until now its hourly tick read each tenant's
schedule from the tenant's own Durable Object and, on a digest hour, started that tenant's
container at once with no period, so the container's `cyris run --if-due` picked the period from
the clock it started on. A start that found every instance held was retried for 90 seconds and
then failed.

Measured runs take 196 to 378 seconds and peak at 283 to 503 MB. Traditional Chinese readers
mostly want their digest in the same one or two hours, so many tenants come due together. Three
problems follow:

* A third tenant due in the same hour as two others waits 90 seconds and fails. A start that only
  waited would run `--if-due` after its hour and find nothing due.
* On the trial account on 2026-11-19, bravo's start met "There is no container instance that can
  be provided to this Durable Object" three times, then looked granted. Its container stopped with
  the same error, but `POST /run` still answered 200, and bravo's `digest_runs` got no row. The
  run vanished.
* A tenant's schedule lived in the tenant's own database. Its run container can rewrite that
  database (`TenantDbGateway` runs any SQL on it), so a tenant's run decided how often its tenant
  ran, on the operator's container and LLM spend. The security review of 2026-11-19 asked for the
  schedule's authority to be the registry, or the Worker, with each tenant's share of the
  container instances and its spend capped by a limit stored in the registry.

How a tenant's runs are scheduled, queued and capped was undecided.

## Decision Drivers

* Every due issue is either made, with its own period and date, or recorded as failed. None is
  skipped, none overwrites another tenant's, and none vanishes.
* A tenant's container cannot change when or how often its tenant runs.
* No tenant can hold more than its share of the instances, and each has a cap the operator sets.
* No new Cloudflare product, binding or external service.
* The default Worker (`wrangler.toml`) and its tick behave as before.

## Considered Options

* A run queue in the registry Durable Object, started by the registry's alarm
* Each tenant's own run object waits and retries with an alarm of its own
* Cloudflare Queues between the tick and the run objects
* Keep starting at once, and rely on the 90-second retry and the next tick's catch-up

## Decision Outcome

Chosen option: "a run queue in the registry Durable Object, started by the registry's alarm".
The registry already holds the tenant list and the operator's limits (ADR-0030), and it is
strongly consistent. So it is the one place that can see every tenant's runs, share the
instances fairly, and hold a cap that no container can reach. It needs no new binding.

How it is built (`workers/app/src/run_queue.js`, `tenant_registry.js`):

* **The schedule is the registry's.** `tenants.digest_times`, two whole hours as
  `validate_schedule` takes them (one hour since ADR-0042, two when the tenant is allowed them),
  and a new `tenants.timezone` are set only by the operator's
  `POST /hosted/tenants/<tenant>/schedule?times=&timezone=[&daily_run_cap=]`. The registry
  checks them. The zone must be an IANA name, an area and its locations or one word such as UTC,
  that the runtime knows. It is stored as given, or in the runtime's casing when only the case
  differs, because the container's zoneinfo reads names case-sensitively. An offset such as
  `+08:00` is refused with 400: `Intl` takes it, the container's `_known_timezone` does not, and
  every run would fail. So is a link name given in another case. The runtime's own name for a
  zone is not stored, because it renames links (`Asia/Kolkata` becomes `Asia/Calcutta`).
  `workers/app/test/time_zone_cases.json` holds both sides to one set of answers. The tick reads the schedule from the
  registry row and never from the tenant's `settings`. A tenant with no schedule in the registry
  is polled and never queued. The route also copies the two values into the tenant's own
  `settings`, and so does the seed when the registry has a schedule. The run reads its window and
  dates its times in that zone, and the copy changes nothing the tick reads.
* **A run names its issue when it is queued.** The tick, its catch-up and the operator's run
  route each queue `{period, date}`: a digest hour's period and its date in the tenant's zone, a
  missed hour's, or the period the operator names, dated today in the tenant's zone unless a date
  is given. The run container starts with `CYRIS_RUN_PERIOD` and `CYRIS_RUN_DATE`, so it runs
  `cyris run --period --date` whenever it starts. No hosted run uses `--if-due`.
* **The queue is the registry's `runs` table.** Each row is one run: its tenant, period, date,
  origin (`tick`, `catch-up` or `operator`), state, attempts and times. A partial unique index
  allows one live (`queued`, `starting` or `running`) row per tenant and issue. A second trigger
  of a live issue is refused, and the run route answers it with 409. A different issue of the
  same tenant is queued behind it.
* **The registry's alarm starts the runs.** Queueing a run, and every stop report, set the alarm
  for now. Each alarm pass first expires every lease that is over. A held run's lease is
  `RUN_LEASE_MS`, 20 minutes from its start. That is longer than the container's 15-minute
  inactivity timeout, so the container has stopped by then. The pass then starts queued runs
  until `RUN_SLOTS` are held. `RUN_SLOTS` equals `max_instances`, and
  `tests/test_hosted_config.py` holds them equal. A tenant holds at most one slot. The tenant
  that started a run least recently goes first, a tenant that never ran before any of them, and
  ties go to the run queued first. Last, the pass sets the next alarm: a minute on while a run
  waits, and a second past the earliest held run's lease end while one is held. So a run whose
  stop is never reported is expired on time even when nothing else happens.
* **An expired lease queues the run again.** This covers a deploy that replaces a run object
  mid-run, and a stop report whose call failed and was only logged. If the tenant's
  `digest_runs` holds an ok run of that period since the run started, the run made its issue
  and only its report was lost, so it ends as `done` and nothing starts. Otherwise the row ends
  as `lost`, and a new row of the same issue is queued with its attempts, within
  `MAX_START_ATTEMPTS`. The run that has had its last start ends as `failed`. The lost row
  keeps its start time, so it counts against the cap, and the tenant waits behind tenants that
  have not run. Queueing a run also expires any lease already over first, so a catch-up of that
  issue is queued instead of refused as live.
* **A stop is reported.** `Lifecycle` (`container.js`) takes an optional `onStop`, which the
  default Worker does not pass. The tenant's run object passes one, which tells the registry
  how its container stopped. Exit 0 ends the run as `done`. A stop with "no container instance"
  queues the run again, like a start that threw it after its 90-second retry. Any other exit, or
  any other start error, ends the run as `failed`, and the next tick's catch-up applies as
  before. A run gets `MAX_START_ATTEMPTS`, four starts, before it is `failed`. Starts that met no
  instance and starts whose lease ran out both count. Every state is in the tenant's report
  (`runs`) and in the logs (`run queued`, `run started`, `run done`, `run failed`, `run lost`,
  `run queued again`).
  * The report names the run it is about. The alarm hands `startRun` the row's id and start
    number (`{ id, attempt }`), the run object keeps them with that start (its storage key `run`),
    and `Lifecycle` hands them back to `onStop` with that start's stop. The registry ends that
    row only while it still holds that start. A report of any other start, or one that names no
    run, ends nothing and is logged as `container stopped with no run held`. So a report heard
    after the lease already ended its run cannot end the run queued in its place, whose
    container may be running.
* **Each tenant has a cap in the registry.** A new column, `tenants.daily_run_cap`, is the most
  runs a tenant may start in any 24 hours. Its live runs count against it too. A run past the cap
  is refused, and the run route answers 429. The default is 6, `2 × CATCH_UP_HOURS`. That is two
  digest hours, each tried by its tick and its catch-ups, so a day of failures still fits. (Since
  ADR-0042 it is 3, one digest hour's.) Only
  something else, such as the operator or a tenant rewriting its own `digest_runs` to ask for
  catch-ups, can reach it. The operator's schedule route sets another cap, from 1 to 24.
  Scheduling a tenant again without naming a cap keeps the one it has.
* **The registry's new columns are added in place.** ADR-0030 left the registry without a version
  table. Both new columns are additive and have a default, so the registry checks its own
  `tenants` columns on start and adds any that are missing. A version table waits for the first
  change that is not additive.
* `max_instances` stays 2 (4 since ADR-0042). Raising it for a larger beta is a choice about
  money and the beta's size. It takes one line of `wrangler.hosted.toml` and `RUN_SLOTS`. Throughput is about two runs
  every six minutes, so twenty tenants due in one hour all start within it.

### Consequences

* Good, because a due issue is made with its own period and date, even when it starts hours after
  its hour or past midnight in its zone.
* Good, because a start that gets no instance goes back in the queue instead of vanishing. A run
  that cannot start at all is recorded as `failed`, with its reason, where the operator reads it.
* Good, because a tenant's container cannot change when or how often it runs. A tenant that
  forges its own `digest_runs` to ask for catch-ups is stopped by the dedupe and its registry cap.
* Good, because instances are shared, one per tenant and least recently started first, so a
  crowd at one hour waits its turn instead of failing.
* Bad, because the registry, one object, now takes a call per queued run and per stop besides the
  tick's read. It also holds an alarm pass that awaits each start, up to the 90-second retry.
  This is far below one object's limits at beta sizes. Login traffic is still the load to watch.
* Bad, because the registry's alarm calls a tenant's run object, which calls the registry back to
  check the tenant's status. This relies on a Durable Object taking other calls while it awaits
  one of its own. That is how the runtime is documented to behave, but it has not been run on the
  trial account yet.
* Bad, because a run lost with no stop report, when its object was replaced mid-run, holds its
  slot until its lease ends, 20 minutes after its start, before it is queued again.
* Neutral, because a stop report is matched to the run and start its container ran, not to the
  tenant's newest held run. A report delayed past its run's lease ends nothing: the lease has
  already ended that run as `lost`, and the run queued in its place ends by its own report or
  lease. Until 2026-11-20 it was matched to the newest held run, and the integration review
  showed a report held up by a stalled alert ending the next start's run.
* Bad, because a tenant needs one more operator step, the schedule route, before the tick runs
  it. A registry created before this change keeps its tenants unscheduled until the operator sets
  each one.
* Neutral, because the operator's run route now answers 202 with the queued run, not 200 with
  `started`. A run's fate is in the report's `runs`.

### Confirmation

* `workers/app/test/run_queue.test.js` drives `hosted.js` itself over the registry, the tenant
  databases and a fake `TENANT_RUN`, and checks the following:
  * Three tenants in three zones are each woken at their own hours by the registry's schedule,
    never by the one in their own databases, and each digest hour gets one `digest_runs` row
    finished in it.
  * Two tenants due in one hour both finish, one row each, when one start's container stops
    with "no container instance". A run whose starts never get one is `failed` after four.
  * A second trigger of a live issue is refused, and another issue waits for the tenant's slot.
  * Twelve tenants due at 08:00 start in pairs, and the last pair starts at 09:15 with the
    morning issue. A run that waited past midnight in Taipei keeps the date it was queued with.
  * A free slot goes to the tenant that started a run least recently.
  * The daily cap refuses a run past it and is untouched by the tenant's own database.
  * A run with no stop report is expired by its own alarm at its lease end, with no tick and no
    other run, and is queued again behind tenants that have not run. The lease is longer than
    the inactivity timeout.
  * One tenant whose 08:00 run is never reported gets its morning issue started again: the
    reviewer's repro R1. A run whose issue is already in `digest_runs` ends as `done` and starts
    nothing. A run whose leases keep running out is `failed` after four starts.
  * A zone such as `+08:00` is refused, and every case in `time_zone_cases.json` is stored or
    refused as listed. `tests/test_hosted_config.py` checks the same cases against the
    container's own check.
  * Scheduling a tenant again with no cap named keeps its cap.
  * The registry gains its new columns in place.
* `workers/app/test/hosted.test.js` checks that a tenant's run object reports its container's
  stops to the registry: a "no container instance" stop requeues the run, and exit 0 ends it. It
  also checks the run and schedule routes' answers. A stop heard after the lease ended its run
  ends nothing of the tenant's next run, whose container keeps running.
* `workers/app/test/run_queue.test.js` checks that a stop report naming no run, another row or
  another start of the held row leaves the held run running, and that one naming it ends it.
* `workers/app/test/container.test.js` checks `onStop` and that a failed report is logged.
* `tests/test_hosted_config.py` holds `RUN_SLOTS` equal to `max_instances`.
* `tests/test_hosted_script.py` checks `scripts/hosted.sh schedule` and that a 202 is not a failure.
* The human's check on the trial account is `docs/hosted-tenants.md`: schedule each tenant at
  different hours, then read each tenant's `digest_runs.finished_at` and `runs` in its report.

## Pros and Cons of the Options

### A run queue in the registry, started by its alarm

* Good, because one strongly consistent object sees every tenant's runs, so it can share the
  instances and enforce one live run per issue.
* Good, because the cap and the schedule live where no container can reach them.
* Bad, because the registry serialises one more kind of request.

### Each tenant's run object waits and retries with its own alarm

* Good, because the registry takes no new load.
* Bad, because the run objects cannot see each other: they would race for instances with no
  fairness, and a cap would still need the registry.

### Cloudflare Queues

* Good, because retries, batching and dead-letter queues come with it.
* Bad, because it is a new product and binding to provision and pay for per deployment, which is
  the operator's decision to make, not this card's.
* Bad, because a consumer still needs the instance count and the fairness rule, so the registry
  would still decide which run goes next.

### Start at once, and rely on the retry and the catch-up

* Good, because it needs no code.
* Bad, because it is what lost bravo's run on 2026-11-19, and a third tenant in one hour fails.
* Bad, because a delayed start runs `--if-due` after its hour and makes nothing.

## More Information

* `docs/architecture.md` §4 has the registry's `runs` and the new `tenants` columns. §5 grades
  the run slots, the cap's default and bounds, the start attempts, the lease and the alarm's
  pace. §6 describes the hosted tick and the queue.
* The registry is [ADR-0030](0030-the-operator-registry-is-one-sqlite-durable-object.md). The
  90-second start retry and the "no container instance" measurement are
  [ADR-0028](0028-one-sqlite-durable-object-per-hosted-tenant.md). The tick's poll comes first, as
  [ADR-0031](0031-a-hosted-tenants-rss-is-polled-by-the-hosted-worker.md) set out, and the run it
  queues reads the hour's entries whenever it starts.
* [ADR-0035](0035-a-hosted-runs-alerts-go-to-the-operator-through-the-hosted-worker.md) is how a
  run's end reaches the operator. With both in one Worker, the queue tells the operator, through
  that ADR's `alertOperator` and naming the tenant, of each run it ends as `failed` by a start
  (a start error, or the last of `MAX_START_ATTEMPTS`) and of each run whose lease ends it as
  `lost` or `failed`. It tells nothing of a run its container's exit failed, which the run object
  or the run's own alert has told, and no lease end the run object says it already told
  (`TenantContainer.endTold`). The run object leaves a start the queue holds, and a no-instance
  stop of a held run, to the queue, so a start the queue will try again alerts nobody. The run
  object's abnormal-stop alert is sent before the stop is reported, and is bounded by that ADR's
  `ALERT_TIMEOUT_MS`, so a stalled webhook holds a stop report for 5 seconds at most, far inside
  the lease.
