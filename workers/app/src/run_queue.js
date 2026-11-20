// The hosted run queue: every run of a tenant's digest, whether the tick, its catch-up or the
// operator asked for it, is a row of the registry's `runs`, and the registry's alarm starts the
// queued ones (tenant_registry.js). Why a queue in the registry, and why these rules:
// docs/decisions/0032-hosted-runs-wait-in-a-queue-the-registry-holds.md.
//
// - A run names its issue, period and date, when it is queued, so it makes that issue whenever it
//   starts; `--if-due` would read the clock it started on and find nothing due.
// - At most RUN_SLOTS runs hold a container at once, and a tenant holds at most one; a tenant
//   has at most one live run of an issue, so a second trigger of it is refused.
// - The tenant that started a run least recently goes first, then the run queued first.
// - A tenant starts at most its own `daily_run_cap` runs in 24 hours, a registry column.
// - A run whose container got no instance goes back in the queue, and so does one whose lease ran
//   out with no stop report, within MAX_START_ATTEMPTS; any other end is recorded.
// - While a run is held, the registry's alarm is set for its lease's end, so a stop report that
//   never comes cannot hold the issue live for good.
// - An end only this queue decides is handed to `tell` (the registry's, which alerts the
//   operator through alertOperator, ADR-0035): a run failed by its start, or by its last start,
//   and a run whose lease ran out, lost or failed. A run its container's exit failed is not: the
//   tenant's run object, or the run's own alert, tells the operator of that (hosted_container.js).
//
// The schedule a tenant is ticked on is the registry's too (`digest_times` and `timezone`),
// checked here before it is written. No workerd-only imports: vitest runs this under Node.

import { NO_INSTANCE } from "./container.js";
import { CATCH_UP_HOURS, digestHours } from "./schedule.js";
import { LOCATION_HINT } from "./tenant_db.js";
import { ACTIVE } from "./tenant_status.js";

// As many runs at once as the container application has instances, `max_instances` in
// wrangler.hosted.toml, which tests/test_hosted_config.py holds equal to this: a start beyond
// them would only wait for one. Raise both together as the beta grows.
export const RUN_SLOTS = 2;

// A tenant's cap when the operator names none: two digest hours, each started by its tick and by
// up to CATCH_UP_HOURS - 1 catch-ups, so a day of failed runs still fits and only runs something
// else starts (the operator's, or a tenant rewriting its own digest_runs) can reach it.
export const DEFAULT_DAILY_RUN_CAP = 2 * CATCH_UP_HOURS;
// One run an hour, the tick's own cadence: a cap above it is a typo, not a plan.
export const MAX_DAILY_RUN_CAP = 24;

// How many starts a run gets in all when its container is given no instance; each start already
// retried for START_RETRY's 90 s (hosted_container.js).
export const MAX_START_ATTEMPTS = 4;

// While a run waits, the registry's alarm looks for a free slot again this often.
export const DISPATCH_EVERY_MS = 60_000;

// A run with no stop report this long after its start is taken as lost, and its slot is freed:
// its container's inactivity timeout (15 minutes, hosted_container.js) has stopped it by then.
// The measured runs took 196 to 378 seconds. The registry's alarm is set for each held run's
// lease end (`nextPassAt`), and queueing a run expires any lease already over.
export const RUN_LEASE_MS = 20 * 60_000;

const DAY_MS = 24 * 60 * 60_000;

export const QUEUED = "queued";
export const STARTING = "starting";
export const RUNNING = "running";
// A run's end: its container exited 0, or its issue's ok run is in the tenant's digest_runs when
// its lease ran out (`done`); it failed to start, exited otherwise, or used its last start
// (`failed`); or no stop was reported within RUN_LEASE_MS, and a new row of the same issue was
// queued in its place (`lost`).
export const DONE = "done";
export const FAILED = "failed";
export const LOST = "lost";

// The registry's ledger of runs. At most one live row per tenant and issue, which the partial
// index holds even if two requests race.
export const RUNS_SCHEMA = `
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL,
  period TEXT NOT NULL,
  date TEXT NOT NULL,
  origin TEXT NOT NULL,
  state TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  queued_at TEXT NOT NULL,
  started_at TEXT,
  ended_at TEXT,
  outcome TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_live_issue ON runs(tenant_id, period, date)
  WHERE state IN ('queued', 'starting', 'running');
CREATE INDEX IF NOT EXISTS idx_runs_tenant ON runs(tenant_id, started_at);
`;

const RUN_COLUMNS = "id, tenant_id, period, date, origin, state, attempts, queued_at, started_at, ended_at, outcome";
const LIVE = `state IN ('${QUEUED}', '${STARTING}', '${RUNNING}')`;
const HELD = `state IN ('${STARTING}', '${RUNNING}')`;

// The time a row is written at, as every registry row is (seconds, UTC offset), so they sort.
const stamp = (ms) => `${new Date(ms).toISOString().slice(0, 19)}+00:00`;

const rows = (cursor) => cursor.toArray().map((row) => ({ ...row }));
const one = (cursor) => rows(cursor)[0] ?? null;

// A tenant's run container, from Worker-side code that holds TENANT_RUN, with the tenant
// databases' hint: only an object's first get() honours one.
export const tenantRun = (env, tenant) => env.TENANT_RUN.getByName(tenant, { locationHint: LOCATION_HINT });

// The registry row's schedule, `{ times, timeZone }`, or null when it has none to tick on.
export function registrySchedule(row) {
  if (!row?.digest_times || !row.timezone) return null;
  try {
    return { times: JSON.parse(row.digest_times), timeZone: row.timezone };
  } catch {
    return null;
  }
}

// An IANA zone's name: an area and its locations, or one word such as UTC, each part starting
// with a letter. Never an offset such as +08:00, which the runtime takes and the container's
// zoneinfo (`_known_timezone` in config.py) does not.
const IANA_ZONE = /^[A-Za-z][A-Za-z0-9_+-]*(?:\/[A-Za-z][A-Za-z0-9_+-]*)*$/;
const CAPITALISED = /^[A-Z][^/]*(?:\/[A-Z][^/]*)*$/;

// The zone the registry stores for `timeZone`, the name the container will read, or null to
// refuse it. The runtime must know it; then it is the name as given, in the runtime's casing when
// only the case differs, since zoneinfo reads names case-sensitively. The runtime's own name is
// not taken otherwise: it renames links (US/Eastern, Asia/Kolkata) to zones of its choosing. A
// link given in another case is refused, as there is then no name both sides are known to read.
// test/time_zone_cases.json holds this and the container's check to one set of answers.
function storedZone(timeZone) {
  if (typeof timeZone !== "string" || !IANA_ZONE.test(timeZone)) return null;
  let resolved;
  try {
    resolved = new Intl.DateTimeFormat("en-US", { timeZone }).resolvedOptions().timeZone;
  } catch {
    return null;
  }
  if (resolved.toLowerCase() === timeZone.toLowerCase()) return resolved;
  return CAPITALISED.test(timeZone) ? timeZone : null;
}

// The schedule the operator names, as the registry stores it, or `{refused}`: two distinct whole
// hours as `validate_schedule` takes them, an IANA zone both the runtime and the container read
// (`storedZone`), and a whole cap in 1..MAX_DAILY_RUN_CAP. No cap named is `dailyRunCap: null`,
// which `setSchedule` reads as the tenant's own cap, kept.
export function checkSchedule({ times, timeZone, dailyRunCap }) {
  const hours = digestHours(times);
  if (!hours) return { refused: `digest times are two distinct whole hours, as 08:00,20:00: ${times}` };
  const zone = storedZone(timeZone);
  if (!zone) return { refused: `not an IANA time zone name, as Asia/Taipei: ${timeZone}` };
  const cap = dailyRunCap === undefined || dailyRunCap === null ? null : Number(dailyRunCap);
  if (cap !== null && (!Number.isInteger(cap) || cap < 1 || cap > MAX_DAILY_RUN_CAP)) {
    return { refused: `a daily run cap is a whole number from 1 to ${MAX_DAILY_RUN_CAP}: ${dailyRunCap}` };
  }
  const digestTimes = hours.map((hour) => `${String(hour).padStart(2, "0")}:00`);
  return { schedule: { digestTimes, timeZone: zone, dailyRunCap: cap } };
}

// Queues one run of `tenant`'s (its registry row) issue `{ period, date }`, asked for by `origin`,
// and answers `{queued}`, the row, or `{refused, code}`: `inactive` for a tenant not registered
// as active, `live` when that issue already has a live run, `cap` when the tenant's runs in the
// last 24 hours, with its live ones, have reached its cap.
export function queueRun(sql, tenant, { period, date, origin }, nowMs) {
  if (tenant?.status !== ACTIVE) {
    return { refused: `${tenant?.id ?? "the tenant"} is not an active registered tenant`, code: "inactive" };
  }
  const live = one(
    sql.exec(
      `SELECT ${RUN_COLUMNS} FROM runs WHERE tenant_id = ? AND period = ? AND date = ? AND ${LIVE}`,
      tenant.id,
      period,
      date,
    ),
  );
  if (live) return { refused: `${tenant.id}'s ${period} issue of ${date} is already ${live.state}`, code: "live", run: live };
  const { n } = one(
    sql.exec(
      `SELECT count(*) AS n FROM runs WHERE tenant_id = ? AND (${LIVE} OR started_at >= ?)`,
      tenant.id,
      stamp(nowMs - DAY_MS),
    ),
  );
  if (n >= tenant.daily_run_cap) {
    return { refused: `${tenant.id} has reached its cap of ${tenant.daily_run_cap} runs in 24 hours`, code: "cap" };
  }
  const run = one(
    sql.exec(
      `INSERT INTO runs (tenant_id, period, date, origin, state, queued_at) VALUES (?, ?, ?, ?, ?, ?) RETURNING ${RUN_COLUMNS}`,
      tenant.id,
      period,
      date,
      origin,
      QUEUED,
      stamp(nowMs),
    ),
  );
  console.log("run queued", { tenant: tenant.id, period, date, origin });
  return { queued: run };
}

// A run back in the queue for another start, or failed once it has had MAX_START_ATTEMPTS, which
// `tell` hears of as a start that ended it.
function requeue(sql, run, why, nowMs, tell) {
  if (run.attempts >= MAX_START_ATTEMPTS) {
    const ended = end(sql, run, FAILED, `${why}, after ${run.attempts} starts`, nowMs);
    tell(ended, "start");
    return ended;
  }
  sql.exec("UPDATE runs SET state = ?, started_at = NULL, outcome = ? WHERE id = ?", QUEUED, why, run.id);
  console.log("run queued again", { tenant: run.tenant_id, period: run.period, date: run.date, why });
  return { ...run, state: QUEUED, started_at: null, outcome: why };
}

function end(sql, run, state, outcome, nowMs) {
  sql.exec("UPDATE runs SET state = ?, ended_at = ?, outcome = ? WHERE id = ?", state, stamp(nowMs), outcome, run.id);
  console.log(`run ${state}`, { tenant: run.tenant_id, period: run.period, date: run.date, outcome });
  return { ...run, state, ended_at: stamp(nowMs), outcome };
}

// Ends each held run whose lease is over, and answers how many it queued again. `finished(run)`
// answers whether the tenant's digest_runs holds an ok run of that period since the run started:
// then the run made its issue and only its report was lost, and it is `done`. Otherwise it is
// `lost` and a new row of the same issue is queued in its place, with its attempts, unless it has
// had MAX_START_ATTEMPTS, when it is `failed`; `tell` hears of either as a lease that ended it.
// The lost row keeps its start, so it counts against the cap and the tenant waits behind those
// that have not run. A row that changed while `finished` was awaited is left to whoever changed it.
export async function expireLeases(sql, nowMs, finished = async () => false, tell = () => {}) {
  const minutes = RUN_LEASE_MS / 60_000;
  const stale = rows(sql.exec(`SELECT ${RUN_COLUMNS} FROM runs WHERE ${HELD} AND started_at < ?`, stamp(nowMs - RUN_LEASE_MS)));
  let requeued = 0;
  for (const run of stale) {
    let made = false;
    try {
      made = await finished(run);
    } catch {
      made = false;
    }
    const still = one(sql.exec(`SELECT ${RUN_COLUMNS} FROM runs WHERE id = ? AND ${HELD} AND started_at = ?`, run.id, run.started_at));
    if (!still) continue;
    if (made) {
      end(sql, still, DONE, `its ${run.period} run is in digest_runs; no stop was reported`, nowMs);
    } else if (still.attempts >= MAX_START_ATTEMPTS) {
      tell(end(sql, still, FAILED, `no stop reported within ${minutes} minutes, after ${still.attempts} starts`, nowMs), "lease");
    } else {
      tell(end(sql, still, LOST, `no stop reported within ${minutes} minutes; queued again`, nowMs), "lease");
      sql.exec(
        "INSERT INTO runs (tenant_id, period, date, origin, state, attempts, queued_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        still.tenant_id,
        still.period,
        still.date,
        still.origin,
        QUEUED,
        still.attempts,
        stamp(nowMs),
      );
      requeued += 1;
    }
  }
  return requeued;
}

// When the registry's alarm must next run, or null when nothing waits or is held: a minute on
// while a run waits, and a second past the earliest held run's lease end, so a run whose stop is
// never reported is expired on time even when nothing else happens.
export function nextPassAt(sql, nowMs) {
  const { queued } = one(sql.exec("SELECT count(*) AS queued FROM runs WHERE state = ?", QUEUED));
  const { oldest } = one(sql.exec(`SELECT min(started_at) AS oldest FROM runs WHERE ${HELD}`));
  const times = [];
  if (queued > 0) times.push(nowMs + DISPATCH_EVERY_MS);
  if (oldest) times.push(Date.parse(oldest) + RUN_LEASE_MS + 1_000);
  return times.length ? Math.max(nowMs, Math.min(...times)) : null;
}

// One pass of the queue: expires the leases that are over (`expireLeases`, with `finished`), then
// starts the queued runs that fit, one per tenant, through `start(run)`, which answers
// `TenantContainer.startRun`'s `{started}` or throws. A row is claimed (`starting`) before its
// start is awaited, so a stop report or another pass that lands meanwhile sees it held; each
// answer is applied only to a row still `starting`. Answers `{ started, waiting }`: the runs this
// pass started, and how many still wait. `now` is read again once the starts have answered,
// which can be minutes later. `tell` hears of each run this pass ended as failed or lost.
export async function dispatchRuns(sql, start, { now = Date.now, finished, tell = () => {} } = {}) {
  await expireLeases(sql, now(), finished, tell);
  const nowMs = now();
  const { held } = one(sql.exec(`SELECT count(*) AS held FROM runs WHERE ${HELD}`));
  // A tenant with a held run waits; the rest go least recently started first, a tenant never
  // started before all of them, then by when they were queued.
  const waiting = rows(
    sql.exec(
      `SELECT ${RUN_COLUMNS}, (SELECT max(s.started_at) FROM runs s WHERE s.tenant_id = q.tenant_id) AS last_start
       FROM runs q WHERE q.state = ? AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.tenant_id = q.tenant_id AND r.${HELD})
       ORDER BY last_start IS NOT NULL, last_start, q.queued_at, q.id`,
      QUEUED,
    ),
  );
  const picks = [];
  for (const { last_start: _last, ...run } of waiting) {
    if (picks.length >= RUN_SLOTS - held) break;
    if (picks.some((pick) => pick.tenant_id === run.tenant_id)) continue;
    sql.exec("UPDATE runs SET state = ?, started_at = ?, attempts = attempts + 1 WHERE id = ?", STARTING, stamp(nowMs), run.id);
    picks.push({ ...run, state: STARTING, started_at: stamp(nowMs), attempts: run.attempts + 1 });
  }
  const answers = await Promise.allSettled(picks.map((run) => start(run)));
  const started = [];
  answers.forEach((answer, i) => {
    const run = one(sql.exec(`SELECT ${RUN_COLUMNS} FROM runs WHERE id = ?`, picks[i].id));
    if (run?.state !== STARTING) return;
    if (answer.status === "fulfilled" && answer.value?.started) {
      sql.exec("UPDATE runs SET state = ? WHERE id = ?", RUNNING, run.id);
      console.log("run started", { tenant: run.tenant_id, period: run.period, date: run.date, attempt: run.attempts });
      started.push({ ...run, state: RUNNING });
    } else if (answer.status === "fulfilled") {
      // Its container was already up with a run this queue did not start; try again later.
      requeue(sql, run, "its container was already running", now(), tell);
    } else if (NO_INSTANCE.test(String(answer.reason))) {
      requeue(sql, run, "no container instance", now(), tell);
    } else {
      tell(end(sql, run, FAILED, String(answer.reason?.message ?? answer.reason).slice(0, 500), now()), "start");
    }
  });
  const { n } = one(sql.exec("SELECT count(*) AS n FROM runs WHERE state = ?", QUEUED));
  return { started, waiting: n };
}

// `tenant`'s run the queue holds, `starting` or `running`, or null when it holds none.
export const heldRun = (sql, tenant) =>
  one(sql.exec(`SELECT ${RUN_COLUMNS} FROM runs WHERE tenant_id = ? AND ${HELD} ORDER BY id DESC LIMIT 1`, tenant));

// The stop of `tenant`'s container, as its object's monitor saw it, applied to the tenant's held
// run: `done` on exit 0, back in the queue when no instance was ever given (the start looked
// granted, then the container stopped with NO_INSTANCE), `failed` otherwise. Answers the run, or
// null when the tenant had no held run (a container this queue did not start). Only a run that
// used its last start is handed to `tell`: any other exit is the run object's to tell.
export function endRun(sql, tenant, { exitCode, reason } = {}, nowMs, tell = () => {}) {
  const run = heldRun(sql, tenant);
  if (!run) {
    console.log("container stopped with no run held", { tenant, exitCode });
    return null;
  }
  if (NO_INSTANCE.test(String(reason ?? ""))) return requeue(sql, run, "no container instance", nowMs, tell);
  if (exitCode === 0) return end(sql, run, DONE, "exit 0", nowMs);
  return end(sql, run, FAILED, `exit ${exitCode ?? "unknown"}: ${String(reason ?? "")}`.slice(0, 500), nowMs);
}

// `tenant`'s most recent runs, newest first, for the operator's report.
export const recentRuns = (sql, tenant, limit = 20) =>
  rows(sql.exec(`SELECT ${RUN_COLUMNS} FROM runs WHERE tenant_id = ? ORDER BY id DESC LIMIT ?`, tenant, limit));
