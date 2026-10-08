// Is this hour a digest hour? Asked by the Cron Trigger before it wakes the `run`
// container, which asks again (`cyris run --if-due`). It is the same question as
// `due_period` in src/cyris/service_layer/schedule.py, and test/schedule_cases.json
// holds both to one set of answers.
//
// The schedule is read from D1 `settings` over REST on every tick, never
// remembered, so a change on /settings applies at the next tick. No workerd-only
// imports: vitest runs this under Node.

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

// The schedule and time zone as stored, or null when either cannot be read.
async function readSchedule({ env, fetchImpl, timeoutMs }) {
  const account = env.CLOUDFLARE_ACCOUNT_ID;
  const database = env.CYRIS_STORE_DATABASE_ID;
  const token = env.CLOUDFLARE_API_TOKEN;
  if (env.CYRIS_STORE_BACKEND === "json" || !account || !database || !token) return null;
  try {
    const response = await fetchImpl(`${ACCOUNTS_API}/${account}/d1/database/${database}/query`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        sql: "SELECT key, value FROM settings WHERE key IN (?, ?)",
        params: [SCHEDULE_KEY, TIMEZONE_KEY],
      }),
      signal: AbortSignal.timeout(timeoutMs),
    });
    // The status only: an error body may echo the request.
    if (!response.ok) {
      console.warn("schedule read failed:", response.status);
      return null;
    }
    const data = await response.json();
    const rows = data.success ? data.result?.[0]?.results : null;
    const values = Object.fromEntries((rows ?? []).map((row) => [row.key, JSON.parse(row.value)]));
    if (!(SCHEDULE_KEY in values) || !(TIMEZONE_KEY in values)) {
      console.warn("schedule read failed: no schedule or time zone in D1 settings");
      return null;
    }
    return { times: values[SCHEDULE_KEY], timeZone: values[TIMEZONE_KEY] };
  } catch {
    console.warn("schedule read failed");
    return null;
  }
}

// Whether the Cron Trigger should wake the `run` container now. When the schedule
// cannot be read it starts anyway and leaves the question to `--if-due`: a woken
// container costs one idle tick, a skipped digest hour costs an issue.
export async function shouldStartRun({ env, fetchImpl, now = () => new Date(), timeoutMs = 5000 }) {
  const schedule = await readSchedule({ env, fetchImpl, timeoutMs });
  if (!schedule) return true;
  try {
    return duePeriod(now(), schedule.times, schedule.timeZone) !== null;
  } catch {
    console.warn("schedule read failed: unknown time zone");
    return true;
  }
}
