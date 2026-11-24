---
status: accepted
date: 2026-11-24
decision-makers: ""
---

# A hosted tenant makes one digest a day unless the operator allows it two

> Amended by [ADR-0043](0043-one-settings-page-an-edition-locks-a-key.md) (2026-11-24). The
> settings page every edition shares always shows two hour controls; the second is locked for
> `plan-limit` while the tenant may have one. The hosted page ADR-0040 built keeps its one select
> until M3 replaces it. The rest of this ADR still stands.

## Context and Problem Statement

Every Cyris schedule was two whole hours, a morning and an evening issue, on both editions
(`validate_schedule`, `digestHours`). The hosted registry held each tenant's two hours in
`tenants.digest_times` ([ADR-0032](0032-hosted-runs-wait-in-a-queue-the-registry-holds.md)), and the
seed wrote 08:00 and 20:00 UTC into every tenant's settings. Each hosted run costs the operator a
container and its AI spend, and the hosted Worker had two container instances for every tenant.

On 2026-11-23 the product owner decided the following, and it is not open for redesign here:

* A hosted tenant has one digest time by default, and in the closed beta. A later paid tier may
  allow two. No tenant has more than two.
* One digest a day still covers the whole day: the article window defaults to 24 hours
  (`settings_starting_values.json`), and a run only takes pending articles (`run_digest.py`).
* The one daily issue is stored as period `morning`. The `(date, period)` key and the period
  names stay. The URL layer later maps morning to 1 and evening to 2.
* New tenants' default hour is spread across the day instead of every tenant sharing 08:00 UTC,
  and it avoids the daily backup's hour and its check's.
* `RUN_SLOTS` and `max_instances` go to 4, for a beta of about ten tenants at spread hours.
* A self-hosted deployment keeps exactly two digest times.

What this record decides is where the maximum lives, how each schedule check takes one hour, and
the rule that spreads the default hours.

## Decision Drivers

* A tenant's container must not be able to raise its own number of runs (ADR-0032), so the
  maximum cannot be a row the tenant's database holds.
* A self-hosted schedule must behave as before, on the Worker's tick and in the container.
* The rule that spreads tenants must give the same hour every time, from nothing but the tenant,
  and no state of its own.
* No new binding, product or table.

## Considered Options

* A registry column, `tenants.max_digest_times`, set on the operator's schedule route
* A constant for every tenant, one, raised by a deploy when a tier allows two
* A key in the tenant's own `settings`

## Decision Outcome

Chosen option: "a registry column", because the registry already holds the schedule and the run
cap out of every container's reach (ADR-0032), and a column with a default is added in place like
the others. A later tier sets one tenant's column, not a constant every tenant shares.

How it is built:

* **The editions take different counts.** `SELF_HOSTED_TIMES` is exactly two and `HOSTED_TIMES`
  is one or two, in `workers/app/src/schedule.js` and `src/cyris/service_layer/schedule.py`,
  which `workers/app/test/schedule_cases.json` holds to one set of answers. The earliest hour is
  the morning's, so a schedule of one hour has only a morning. The self-hosted tick, `--if-due`
  and `/settings` keep exactly two. The container's config takes one hour only when the
  environment names a tenant, `CYRIS_STORE_TENANT_ID` in a run container or
  `CYRIS_OPERATOR_TENANT` for the operator. Those are the only places `StoreConfig` reads a
  tenant from, so a self-hosted D1 row of one hour is still refused as before.
* **The maximum is the registry's.** `tenants.max_digest_times` is 1 unless the operator's
  `POST /hosted/tenants/<tenant>/schedule?...&max_digest_times=N` sets it, and N is 1 or 2.
  `checkSchedule` refuses more hours than the maximum named, or else the tenant's own. A schedule
  that names no maximum keeps the one the tenant has, as the cap is kept. A reader's settings save
  goes through the same check and cannot name one (`operator_keys`). The settings page offers one
  hour select unless the tenant is allowed two.
* **The tick plans with the hosted counts.** A tenant of one hour queues its morning issue at
  that hour, and a catch-up of that hour is the morning's.
* **A row from before the column keeps its first hour.** A tenant scheduled at two hours before
  this change gets the default maximum, 1, when the column is added. `registrySchedule` then
  answers its earliest hour only, the morning's. That follows the owner's default without the
  operator rescheduling every tenant first, and it never ticks a tenant on more hours than it is
  allowed.
* **The run cap follows one hour.** `DEFAULT_DAILY_RUN_CAP` is `DEFAULT_MAX_DIGEST_TIMES ×
  CATCH_UP_HOURS`, 3: the hour's tick and its two catch-ups, so a day of failures still fits. A
  tenant allowed two needs a cap of 6, which the operator names with its maximum. A row that
  already holds a cap keeps it.
* **The seed spreads the default hour.** A tenant the registry holds no schedule for is given one
  hour in UTC, the seed's own zone, in the registry and in its settings copy. So a seeded tenant
  is ticked without a further operator step, and the schedule route replaces the hour. The hour is
  the 32-bit FNV-1a hash of the tenant id, modulo the count of candidate hours, indexing them. The
  candidates are every UTC hour but those from `BACKUP_HOUR_UTC` (04:00) to its check,
  `BACKUP_CHECK_HOURS` later (06:00) (`spreadHour` and `seedSchedule` in
  `workers/app/src/hosted_seed.js`, and `backupHours` in `hosted_backup.js`). 200 ids land on
  all 21 candidate hours. `alpha` gets 08:00 and `bravo` 00:00. A tenant that already has a
  schedule keeps it when it is seeded again.
* **Four slots.** `RUN_SLOTS` and `max_instances` are 4. With tenants at spread hours, four start
  together whenever up to four are due in one hour, and with runs of 196 to 378 seconds four slots
  clear far more than ten tenants within the hour.

### Consequences

* Good, because a beta tenant costs one run a day, not two, and its issue still covers the day.
* Good, because no container can raise its tenant's maximum: the column is the registry's.
* Good, because a self-hosted deployment's schedule is checked exactly as before on both sides.
* Good, because tenants seeded together are not all due in one hour, so the queue rarely makes
  one wait.
* Bad, because on deploy a tenant scheduled at two hours drops its evening issue until the
  operator allows it two. That is the default the owner chose, and the runbook says how to keep
  two (`docs/hosted-tenants.md`).
* Bad, because a seeded tenant now runs daily without the operator's schedule step, on the
  operator's spend. The seed already needed the operator, so no tenant starts running without the
  operator's action.
* Neutral, because a hash spreads ids only about evenly: two of ten ids can share an hour, which
  four slots absorb.
* Neutral, because the operator's run route still takes `evening`, so an operator can still make
  a second issue of a day by hand.

### Confirmation

* `workers/app/test/schedule_cases.json`, run by `workers/app/test/schedule.test.js` and
  `tests/test_schedule_gate.py`: a hosted hour is the morning's, two hosted hours keep morning and
  evening, three are never due, and a self-hosted schedule of one hour is never due.
* `tests/test_hosted_schedule_config.py`: a run container and the operator's way in load a
  one-hour schedule, and a self-hosted load counts it as missing.
* `workers/app/test/run_queue.test.js`: a tenant of one hour in Taipei is woken once a day and its
  `digest_runs` row is `morning`; its catch-up is the morning's; a two-hour row from before the
  column is ticked on its first hour; the registry takes one hour, refuses two at the default
  maximum, takes two once the maximum is 2 and keeps that maximum; and the column is added in
  place. Queue tests count in `RUN_SLOTS`, which is 4.
* `workers/app/test/hosted.test.js`: `spreadHour` never gives a backup hour, gives the same hour to
  the same id, and covers every candidate hour.
* `workers/app/test/tenant_registry.test.js`: the seed route gives an unscheduled tenant its hour
  in the registry and keeps an operator's schedule.
* `workers/app/test/hosted_settings.test.js`: the page offers one hour select at the default
  maximum and two at 2, and a reader's two hours are refused at the default, writing nothing. A
  tenant allowed two who holds one hour sees "no second digest" selected, the page's script sends
  one hour for it, and a save of another setting sends no schedule, so the registry's hours stay
  as they are.
* `tests/test_hosted_config.py`: the seed file names no hour, every hour the seed can give is a
  valid hosted schedule, and `RUN_SLOTS` equals `max_instances`.
* `tests/test_hosted_script.py`: `scripts/hosted.sh schedule` sends one hour, and two hours with
  their cap and maximum.

## Pros and Cons of the Options

### A registry column

* Good, because it sits beside the schedule and cap it limits, out of every container's reach.
* Good, because one tenant can be allowed two without a deploy.
* Bad, because the registry's `tenants` row has one more column to export and restore.

### A constant for every tenant

* Good, because it needs no column.
* Bad, because a tier that allows one tenant two would need a deploy that allows every tenant two.

### A key in the tenant's own settings

* Good, because the settings page already reads the tenant's settings.
* Bad, because the tenant's run container can rewrite its own settings (ADR-0032), so a tenant
  could raise its own runs.

## More Information

* ADR-0032's statements that a tenant's schedule is two hours, that the default cap is 6, that
  `max_instances` stays 2, and that a tenant needs the schedule route before it is ticked are
  superseded in part by this record. The rest of ADR-0032 stands.
* `docs/architecture.md` §4 has the column, §5 grades the default maximum, the cap, the slots and
  the seed's hour, and §6 describes the tick.
