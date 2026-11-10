// Is this hour a digest hour? Asked by the Cron Trigger before it wakes the `run`
// container, which asks again (`cyris run --if-due`). It is the same question as
// `due_period` in src/cyris/service_layer/schedule.py, and test/schedule_cases.json
// holds both to one set of answers.
//
// The schedule is read from D1 `settings` over REST on every tick, never
// remembered, so a change on /settings applies at the next tick. Off a digest
// hour the tick also reads `digest_runs`, to catch up an hour whose run never
// finished (`planTick`). No workerd-only imports: vitest runs this under Node.

const ACCOUNTS_API = "https://api.cloudflare.com/client/v4/accounts";
const SCHEDULE_KEY = "general.digest_schedule";
const TIMEZONE_KEY = "general.timezone";

// `validate_schedule`: exactly two distinct whole hours, earliest first; null otherwise.
function digestHours(times) {
  if (!Array.isArray(times) || times.length !== 2) return null;
  const hours = [];
  for (const time of times) {
    const parts = String(time).split(":");
    const [hour, minute] = [parts[0], parts.slice(1).join(":")].map((part) =>
      /^\s*[+-]?\d+\s*$/.test(part) ? Number(part) : NaN
    );
    if (!(hour >= 0 && hour <= 23) || minute !== 0) return null;
    hours.push(hour);
  }
  if (hours[0] === hours[1]) return null;
  return hours.sort((a, b) => a - b);
}

// "morning" for the earlier hour, "evening" for the later, null for any other hour
// or a schedule no tick can honour. Throws RangeError on an unknown time zone.
export function duePeriod(now, times, timeZone) {
  const hours = digestHours(times);
  if (!hours) return null;
  const hour = Number(
    new Intl.DateTimeFormat("en-GB", { timeZone, hour: "2-digit", hourCycle: "h23" }).format(now)
  );
  if (hour === hours[0]) return "morning";
  if (hour === hours[1]) return "evening";
  return null;
}

// The rows one D1 query answers over REST, or null when there is no D1 to ask or it
// cannot answer; a failure warns as "<what> read failed".
async function queryD1({ env, fetchImpl, timeoutMs, sql, params, what }) {
  const account = env.CLOUDFLARE_ACCOUNT_ID;
  const database = env.CYRIS_STORE_DATABASE_ID;
  const token = env.CLOUDFLARE_API_TOKEN;
  if (!account || !database || !token) return null;
  try {
    const response = await fetchImpl(`${ACCOUNTS_API}/${account}/d1/database/${database}/query`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ sql, params }),
      signal: AbortSignal.timeout(timeoutMs),
    });
    // The status only: an error body may echo the request.
    if (!response.ok) {
      console.warn(`${what} read failed:`, response.status);
      return null;
    }
    const data = await response.json();
    const rows = data.success ? data.result?.[0]?.results : null;
    if (!Array.isArray(rows)) {
      console.warn(`${what} read failed: unsuccessful query`);
      return null;
    }
    return rows;
  } catch {
    console.warn(`${what} read failed`);
    return null;
  }
}

// The schedule and time zone as stored, or null when either cannot be read.
async function readSchedule({ env, fetchImpl, timeoutMs }) {
  const rows = await queryD1({
    env,
    fetchImpl,
    timeoutMs,
    sql: "SELECT key, value FROM settings WHERE key IN (?, ?)",
    params: [SCHEDULE_KEY, TIMEZONE_KEY],
    what: "schedule",
  });
  if (!rows) return null;
  let values;
  try {
    values = Object.fromEntries(rows.map((row) => [row.key, JSON.parse(row.value)]));
  } catch {
    console.warn("schedule read failed: a setting is not JSON");
    return null;
  }
  if (!(SCHEDULE_KEY in values) || !(TIMEZONE_KEY in values)) {
    console.warn("schedule read failed: no schedule or time zone in D1 settings");
    return null;
  }
  return { times: values[SCHEDULE_KEY], timeZone: values[TIMEZONE_KEY] };
}

// The deployment's time zone as stored, or null when it cannot be read: what a
// manual run's named date is checked against (router.js).
export async function readTimeZone({ env, fetchImpl, timeoutMs = 5000 }) {
  const schedule = await readSchedule({ env, fetchImpl, timeoutMs });
  return schedule ? schedule.timeZone : null;
}

// How long after a digest hour begins the tick still runs that hour's issue when
// D1 holds no ok run of it. Cloudflare can replace the `run` container's Durable
// Object mid-run, which kills the run and loses the issue; the ticks after it
// retry, and a persistent failure is retried twice before it is left to a
// manual POST /run.
export const CATCH_UP_HOURS = 3;

const HOUR_MS = 60 * 60 * 1000;

// `instant` on the wall clock of `timeZone`: its date as YYYY-MM-DD, and how far
// into its hour it is.
function wallClock(instant, timeZone) {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    })
      .formatToParts(instant)
      .map(({ type, value }) => [type, value])
  );
  const intoHourMs =
    (Number(parts.minute) * 60 + Number(parts.second)) * 1000 + instant.getUTCMilliseconds();
  return { date: `${parts.year}-${parts.month}-${parts.day}`, intoHourMs };
}

// The latest digest hour begun before `now`'s own hour and less than
// CATCH_UP_HOURS before `now`, or null. Stepping back whole hours from `now` lands
// once in every hour of that window, so the first step that lands in a digest
// hour finds the latest one. `since` is the hour's start as digest_runs stores
// `finished_at`; `date` is the hour's own, which past midnight is not `now`'s.
function missedDigestHour(now, times, timeZone) {
  for (let back = 1; back < CATCH_UP_HOURS; back++) {
    const probe = new Date(now.getTime() - back * HOUR_MS);
    const period = duePeriod(probe, times, timeZone);
    if (!period) continue;
    const { date, intoHourMs } = wallClock(probe, timeZone);
    const start = new Date(probe.getTime() - intoHourMs);
    return { period, date, since: `${start.toISOString().slice(0, 19)}+00:00` };
  }
  return null;
}

// What the Cron Trigger should start now, as `startRun`'s arguments:
// - `{}` on a digest hour: wake the `run` container and let `--if-due` pick the
//   period. Also when the schedule cannot be read, since the tick then cannot tell
//   a digest hour: a woken container costs one idle tick, a skipped digest hour
//   costs an issue.
// - `{ period, date }` to catch up the last digest hour, while it is less than
//   CATCH_UP_HOURS old and `digest_runs` holds no ok, non-dry run of its period
//   since it began. An error row is not done, so this retries it too.
// - null to leave the container asleep, which is also the answer when
//   `digest_runs` cannot be read: a missed catch-up loses nothing the lost run
//   had not already lost.
export async function planTick({ env, fetchImpl, now = () => new Date(), timeoutMs = 5000 }) {
  const at = now();
  const schedule = await readSchedule({ env, fetchImpl, timeoutMs });
  if (!schedule) return {};
  let missed;
  try {
    if (duePeriod(at, schedule.times, schedule.timeZone) !== null) return {};
    missed = missedDigestHour(at, schedule.times, schedule.timeZone);
  } catch {
    console.warn("schedule read failed: unknown time zone");
    return {};
  }
  if (!missed) return null;
  const runs = await queryD1({
    env,
    fetchImpl,
    timeoutMs,
    sql: "SELECT status, dry_run FROM digest_runs WHERE period = ? AND finished_at >= ?",
    params: [missed.period, missed.since],
    what: "digest_runs",
  });
  if (!runs) return null;
  const done = runs.some((row) => row.status === "ok" && Number(row.dry_run) === 0);
  return done ? null : { period: missed.period, date: missed.date };
}
