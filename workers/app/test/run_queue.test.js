// The hosted run queue (run_queue.js) through the hosted Worker's entry itself, hosted.js: the
// registry's schedule wakes each tenant at its own hours, the registry's alarm starts the queued
// runs within RUN_SLOTS and one per tenant, and a start that gets no container instance waits
// in the queue instead of vanishing. TENANT_RUN records each start and holds it until a test
// ends it, as a container's monitor would, by writing the tenant's digest_runs row and telling
// the registry.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { env } from "cloudflare:workers";
import worker from "../src/hosted.js";
import { RUN_INACTIVITY_TIMEOUT_MS } from "../src/hosted_container.js";
import {
  DEFAULT_DAILY_RUN_CAP,
  DEFAULT_MAX_DIGEST_TIMES,
  DISPATCH_EVERY_MS,
  checkSchedule,
  MAX_START_ATTEMPTS,
  RUN_LEASE_MS,
  RUN_SLOTS,
} from "../src/run_queue.js";
import { CATCH_UP_HOURS } from "../src/schedule.js";
import ZONES from "./time_zone_cases.json" with { type: "json" };
import { REGISTRY_NAME, REGISTRY_SCHEMA, TenantRegistry, tenantRegistry } from "../src/tenant_registry.js";
import { SCHEMA_SQL, alarmStorage, drainAlarms, namespace, registryNamespace, sqlStorage } from "./sql_storage.js";

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const NO_INSTANCE_ERROR = "Error: There is no container instance that can be provided to this Durable Object, try again later";

const stamp = (ms) => `${new Date(ms).toISOString().slice(0, 19)}+00:00`;
const at = (iso) => vi.setSystemTime(new Date(iso));

// `refuse(tenant, attempt)`, when it answers an error, is how that start of the tenant fails.
function bindings({ refuse = () => null } = {}) {
  const starts = [];
  const TENANT_RUN = {
    getByName(tenant) {
      return {
        startRun: async ({ period, date }) => {
          const attempt = starts.filter((start) => start.tenant === tenant).length + 1;
          starts.push({ tenant, period, date, at: stamp(Date.now()) });
          const refusal = refuse(tenant, attempt);
          if (refusal) throw refusal;
          return { started: true, tenant };
        },
      };
    },
  };
  const bound = { TENANT_DB: namespace(), TENANT_RUN, starts };
  bound.REGISTRY = registryNamespace(bound);
  return bound;
}

const registryOf = (bound) => bound.REGISTRY.getByName(REGISTRY_NAME);

// The tick, then the registry's alarm, which starts what the tick queued.
async function tick(bound) {
  await worker.scheduled();
  await drainAlarms(registryOf(bound));
}

// A tenant registered and scheduled in the registry, its database seeded with the stub seed's
// own schedule, 08:00 and 20:00 UTC, which the tick must not read.
async function tenant(bound, id, times, timeZone, { dailyRunCap } = {}) {
  await tenantRegistry(env).register(id);
  const answer = await tenantRegistry(env).setSchedule(id, { times, timeZone, dailyRunCap, maxDigestTimes: times.length });
  expect(answer.refused).toBeUndefined();
  const db = bound.TENANT_DB.getByName(id);
  db.query(SCHEMA_SQL);
  for (const [key, value] of [
    ["general.digest_schedule", ["08:00", "20:00"]],
    ["general.timezone", "UTC"],
  ]) {
    db.query("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)", [key, JSON.stringify(value), stamp(Date.now())]);
  }
}

const runsOf = (bound, id) => registryOf(bound).runs(id);
const held = (bound, id) => runsOf(bound, id).find((run) => run.state === "running");
// The run `id`'s held start ran, as its run object names it in the stop report.
const ran = (bound, id) => {
  const run = held(bound, id);
  return { id: run.id, attempt: run.attempts };
};

// `id`'s held run finishes: its container wrote its digest_runs row, exited 0, and its monitor
// told the registry, which hands the slot on.
async function finish(bound, id) {
  const run = held(bound, id);
  expect(run, `${id} holds no run`).toBeDefined();
  bound.TENANT_DB.getByName(id).query(
    "INSERT INTO digest_runs (finished_at, status, period, dry_run, fetched) VALUES (?, 'ok', ?, 0, 1)",
    [stamp(Date.now()), run.period],
  );
  await registryOf(bound).runEnded(id, { exitCode: 0, run: ran(bound, id) });
  await drainAlarms(registryOf(bound));
}

const digestRuns = (bound, id) =>
  bound.TENANT_DB.getByName(id).query("SELECT period, finished_at, status FROM digest_runs ORDER BY id").rows;

describe("the hosted run queue", () => {
  let bound;
  let logs;
  const use = (made) => {
    bound = made;
    Object.assign(env, bound);
  };
  beforeEach(() => {
    use(bindings());
    vi.useFakeTimers({ toFake: ["Date"] });
    logs = [];
    vi.spyOn(console, "log").mockImplementation((...args) => logs.push(args));
    vi.spyOn(console, "warn").mockImplementation(() => {});
  });
  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
    for (const key of Object.keys(env)) delete env[key];
  });

  it("wakes three tenants at their own digest hours, each run finishing in its hour, by the registry's schedule", async () => {
    at("2026-11-17T23:30:00Z");
    await tenant(bound, "alpha", ["06:00", "18:00"], "UTC");
    await tenant(bound, "bravo", ["08:00", "20:00"], "Asia/Taipei");
    await tenant(bound, "charlie", ["10:00", "22:00"], "Europe/London");
    // charlie's evening of the 17th is done, so midnight has nothing of it to catch up.
    bound.TENANT_DB.getByName("charlie").query(
      "INSERT INTO digest_runs (finished_at, status, period, dry_run, fetched) VALUES ('2026-11-17T22:05:00+00:00', 'ok', 'evening', 0, 1)",
    );

    const day = Date.parse("2026-11-18T00:00:00Z");
    for (let hour = 0; hour < 24; hour++) {
      vi.setSystemTime(day + hour * HOUR);
      await tick(bound);
      vi.setSystemTime(day + hour * HOUR + 5 * MINUTE);
      for (const id of ["alpha", "bravo", "charlie"].filter((id) => held(bound, id))) await finish(bound, id);
    }

    expect(bound.starts).toEqual([
      { tenant: "bravo", period: "morning", date: "2026-11-18", at: "2026-11-18T00:00:00+00:00" },
      { tenant: "alpha", period: "morning", date: "2026-11-18", at: "2026-11-18T06:00:00+00:00" },
      { tenant: "charlie", period: "morning", date: "2026-11-18", at: "2026-11-18T10:00:00+00:00" },
      { tenant: "bravo", period: "evening", date: "2026-11-18", at: "2026-11-18T12:00:00+00:00" },
      { tenant: "alpha", period: "evening", date: "2026-11-18", at: "2026-11-18T18:00:00+00:00" },
      { tenant: "charlie", period: "evening", date: "2026-11-18", at: "2026-11-18T22:00:00+00:00" },
    ]);
    // Each tenant's digest_runs: one row a digest hour, finished in that hour.
    expect(digestRuns(bound, "alpha").map((row) => [row.period, row.finished_at])).toEqual([
      ["morning", "2026-11-18T06:05:00+00:00"],
      ["evening", "2026-11-18T18:05:00+00:00"],
    ]);
    expect(digestRuns(bound, "bravo").map((row) => [row.period, row.finished_at])).toEqual([
      ["morning", "2026-11-18T00:05:00+00:00"],
      ["evening", "2026-11-18T12:05:00+00:00"],
    ]);
    expect(digestRuns(bound, "charlie").map((row) => [row.period, row.finished_at])).toEqual([
      ["evening", "2026-11-17T22:05:00+00:00"],
      ["morning", "2026-11-18T10:05:00+00:00"],
      ["evening", "2026-11-18T22:05:00+00:00"],
    ]);
    // The schedule each tenant's own database holds, 08:00 and 20:00 UTC, woke none of them.
    expect(bound.starts.filter((start) => /T(08|20):/.test(start.at))).toEqual([]);
  });

  it("wakes a tenant of one digest hour once a day at it, and makes its issue the morning's", async () => {
    at("2026-11-17T23:30:00Z");
    // 21:00 in Taipei is 13:00 UTC: the day's one issue is still the morning's (ADR-0042).
    await tenant(bound, "alpha", ["21:00"], "Asia/Taipei");
    expect(await tenantRegistry(env).tenant("alpha")).toMatchObject({ digest_times: '["21:00"]', max_digest_times: 1 });

    const day = Date.parse("2026-11-18T00:00:00Z");
    for (let hour = 0; hour < 24; hour++) {
      vi.setSystemTime(day + hour * HOUR);
      await tick(bound);
      vi.setSystemTime(day + hour * HOUR + 5 * MINUTE);
      if (held(bound, "alpha")) await finish(bound, "alpha");
    }

    expect(bound.starts).toEqual([{ tenant: "alpha", period: "morning", date: "2026-11-18", at: "2026-11-18T13:00:00+00:00" }]);
    expect(digestRuns(bound, "alpha").map((row) => [row.period, row.finished_at])).toEqual([["morning", "2026-11-18T13:05:00+00:00"]]);
  });

  it("catches up a tenant's one digest hour as the morning's, once it has missed it", async () => {
    use(bindings({ refuse: (id, attempt) => (attempt === 1 ? new Error("container crashed") : null) }));
    at("2026-11-18T07:00:00Z");
    await tenant(bound, "alpha", ["08:00"], "UTC");
    at("2026-11-18T08:00:00Z");
    await tick(bound);
    at("2026-11-18T09:00:00Z");
    await tick(bound);

    expect(bound.starts.map((start) => [start.period, start.date, start.at])).toEqual([
      ["morning", "2026-11-18", "2026-11-18T08:00:00+00:00"],
      ["morning", "2026-11-18", "2026-11-18T09:00:00+00:00"],
    ]);
    expect(runsOf(bound, "alpha").map((run) => [run.origin, run.state])).toEqual([
      ["catch-up", "running"],
      ["tick", "failed"],
    ]);
  });

  it("ticks a row scheduled at two hours before the maximum existed on its first hour only", async () => {
    at("2026-11-18T07:00:00Z");
    await tenant(bound, "alpha", ["08:00", "20:00"], "UTC");
    // As a registry from before the column gets it: two times, and the default maximum of one.
    registryOf(bound).ctx.storage.sql.exec("UPDATE tenants SET max_digest_times = 1 WHERE id = 'alpha'");

    for (const hour of ["08", "20"]) {
      at(`2026-11-18T${hour}:00:00Z`);
      await tick(bound);
      at(`2026-11-18T${hour}:05:00Z`);
      if (held(bound, "alpha")) await finish(bound, "alpha");
    }

    expect(bound.starts.map((start) => [start.period, start.at])).toEqual([["morning", "2026-11-18T08:00:00+00:00"]]);
  });

  it("runs two tenants due in one hour both, each to its own digest_runs row, when one start finds no instance", async () => {
    at("2026-11-18T07:00:00Z");
    await tenant(bound, "alpha", ["08:00", "20:00"], "UTC");
    await tenant(bound, "bravo", ["08:00", "20:00"], "UTC");
    at("2026-11-18T08:00:00Z");
    await tick(bound);
    expect(bound.starts.map((start) => start.tenant)).toEqual(["alpha", "bravo"]);

    // The trial account's failure: bravo's start looked granted, then its container stopped
    // with "no container instance". Its run goes back in the queue and starts again.
    at("2026-11-18T08:00:20Z");
    await registryOf(bound).runEnded("bravo", { reason: NO_INSTANCE_ERROR, run: ran(bound, "bravo") });
    await drainAlarms(registryOf(bound));
    expect(bound.starts.map((start) => [start.tenant, start.period, start.date])).toEqual([
      ["alpha", "morning", "2026-11-18"],
      ["bravo", "morning", "2026-11-18"],
      ["bravo", "morning", "2026-11-18"],
    ]);

    at("2026-11-18T08:06:00Z");
    await finish(bound, "alpha");
    await finish(bound, "bravo");

    for (const id of ["alpha", "bravo"]) {
      expect(digestRuns(bound, id).map((row) => [row.period, row.status])).toEqual([["morning", "ok"]]);
    }
    expect(runsOf(bound, "alpha").map((run) => [run.state, run.attempts])).toEqual([["done", 1]]);
    expect(runsOf(bound, "bravo").map((run) => [run.state, run.attempts])).toEqual([["done", 2]]);
  });

  it("records a run whose starts never get an instance as failed, in the report, after its last start", async () => {
    use(bindings({ refuse: (id) => (id === "bravo" ? new Error(NO_INSTANCE_ERROR) : null) }));
    at("2026-11-18T07:00:00Z");
    await tenant(bound, "alpha", ["08:00", "20:00"], "UTC");
    await tenant(bound, "bravo", ["08:00", "20:00"], "UTC");
    at("2026-11-18T08:00:00Z");
    await tick(bound);

    // Each refused start goes back in the queue, and the alarm tries again a minute on.
    for (let pass = 1; pass < MAX_START_ATTEMPTS; pass++) {
      expect(runsOf(bound, "bravo")[0]).toMatchObject({ state: "queued", attempts: pass, outcome: "no container instance" });
      vi.setSystemTime(Date.now() + DISPATCH_EVERY_MS);
      await drainAlarms(registryOf(bound));
    }

    expect(bound.starts.filter((start) => start.tenant === "bravo")).toHaveLength(MAX_START_ATTEMPTS);
    expect(runsOf(bound, "bravo")[0]).toMatchObject({
      state: "failed",
      outcome: `no container instance, after ${MAX_START_ATTEMPTS} starts`,
    });
    expect(logs.filter(([line]) => line === "run failed").map(([, detail]) => detail.tenant)).toEqual(["bravo"]);
    // Nothing is left queued; alpha was never held up, and the alarm waits for its lease's end.
    expect(held(bound, "alpha")).toMatchObject({ period: "morning" });
    expect(registryOf(bound).ctx.storage.alarm).toBe(Date.parse("2026-11-18T08:00:00Z") + RUN_LEASE_MS + 1_000);
  });

  it("refuses a second trigger of one tenant's issue while it is live, and queues another issue behind it", async () => {
    at("2026-11-18T07:00:00Z");
    await tenant(bound, "alpha", ["08:00", "20:00"], "UTC");
    at("2026-11-18T08:00:00Z");
    await tick(bound);
    // The cron firing twice in the hour, and the operator asking for the same issue.
    await tick(bound);
    const again = await tenantRegistry(env).queueRun("alpha", { period: "morning", date: "2026-11-18", origin: "operator" });

    expect(again).toMatchObject({ code: "live", refused: "alpha's morning issue of 2026-11-18 is already running" });
    expect(logs.filter(([line]) => line === "run not queued")).toHaveLength(1);
    expect(bound.starts).toHaveLength(1);

    // Another issue waits for the tenant's one slot, though a second slot is free.
    const other = await tenantRegistry(env).queueRun("alpha", { period: "evening", date: "2026-11-17", origin: "operator" });
    await drainAlarms(registryOf(bound));
    expect(other.queued).toMatchObject({ state: "queued" });
    expect(bound.starts).toHaveLength(1);

    at("2026-11-18T08:06:00Z");
    await finish(bound, "alpha");
    expect(bound.starts.map((start) => [start.period, start.date])).toEqual([
      ["morning", "2026-11-18"],
      ["evening", "2026-11-17"],
    ]);
  });

  it("starts a run that waited past its hour for a slot with the issue it was queued for", async () => {
    // Twelve tenants due at 08:00 and two slots: at 15 minutes a run, the last two start at 09:15.
    at("2026-11-18T07:00:00Z");
    const ids = Array.from({ length: 12 }, (_, i) => `t${String(i + 1).padStart(2, "0")}`);
    for (const id of ids) await tenant(bound, id, ["08:00", "20:00"], "UTC");
    const day = Date.parse("2026-11-18T08:00:00Z");
    for (let quarter = 0; quarter < 6; quarter++) {
      vi.setSystemTime(day + quarter * 15 * MINUTE);
      // The runs held at the quarter hour end; the ones their slots start run the next quarter.
      for (const id of ids.filter((id) => held(bound, id))) await finish(bound, id);
      // The 09:00 tick finds the waiting tenants' morning with no ok run: already queued.
      if (quarter % 4 === 0) await tick(bound);
    }

    expect(RUN_SLOTS).toBe(2);
    expect(bound.starts).toHaveLength(12);
    expect(bound.starts.slice(-2).map((start) => [start.period, start.date, start.at])).toEqual([
      ["morning", "2026-11-18", "2026-11-18T09:15:00+00:00"],
      ["morning", "2026-11-18", "2026-11-18T09:15:00+00:00"],
    ]);
    expect(logs.filter(([line]) => line === "run not queued")).toHaveLength(4);
    for (const id of ids) expect(runsOf(bound, id).map((run) => run.origin)).toEqual(["tick"]);
  });

  it("dates a run that waited past midnight by the issue it was queued for", async () => {
    // No instance can be had for an hour; 23:00 in Taipei is 15:00 UTC, and 16:15 UTC is the next day there.
    use(bindings({ refuse: (id, attempt) => (attempt < MAX_START_ATTEMPTS ? new Error(NO_INSTANCE_ERROR) : null) }));
    at("2026-11-18T14:00:00Z");
    await tenant(bound, "delta", ["08:00", "23:00"], "Asia/Taipei");
    at("2026-11-18T15:00:00Z");
    await tick(bound);
    for (const time of ["15:25", "15:50", "16:15"]) {
      at(`2026-11-18T${time}:00Z`);
      if (time === "16:15") await tick(bound);
      await drainAlarms(registryOf(bound));
    }

    expect(bound.starts.map((start) => [start.period, start.date, start.at])).toEqual([
      ["evening", "2026-11-18", "2026-11-18T15:00:00+00:00"],
      ["evening", "2026-11-18", "2026-11-18T15:25:00+00:00"],
      ["evening", "2026-11-18", "2026-11-18T15:50:00+00:00"],
      ["evening", "2026-11-18", "2026-11-18T16:15:00+00:00"],
    ]);
    expect(runsOf(bound, "delta").map((run) => [run.origin, run.state, run.attempts])).toEqual([["tick", "running", 4]]);
  });

  it("gives a free slot to the tenant that started a run least recently, whoever was queued first", async () => {
    at("2026-11-18T07:00:00Z");
    for (const id of ["alpha", "bravo", "charlie"]) await tenant(bound, id, ["08:00", "20:00"], "UTC");
    const sql = registryOf(bound).ctx.storage.sql;
    // Yesterday alpha started last, bravo before it; charlie has never run.
    for (const [id, started] of [
      ["bravo", "2026-11-17T20:00:00+00:00"],
      ["alpha", "2026-11-17T20:10:00+00:00"],
    ]) {
      sql.exec(
        "INSERT INTO runs (tenant_id, period, date, origin, state, attempts, queued_at, started_at, ended_at, outcome) " +
          "VALUES (?, 'evening', '2026-11-17', 'tick', 'done', 1, ?, ?, ?, 'exit 0')",
        id,
        started,
        started,
        started,
      );
    }

    for (const id of ["alpha", "bravo", "charlie"]) {
      await tenantRegistry(env).queueRun(id, { period: "morning", date: "2026-11-18", origin: "operator" });
    }
    await drainAlarms(registryOf(bound));

    expect(bound.starts.map((start) => start.tenant)).toEqual(["charlie", "bravo"]);
    at("2026-11-18T07:06:00Z");
    await finish(bound, "bravo");
    expect(bound.starts.map((start) => start.tenant)).toEqual(["charlie", "bravo", "alpha"]);
  });

  it("holds each tenant to the registry's daily cap of runs, which the tenant's own database cannot raise", async () => {
    at("2026-11-18T07:00:00Z");
    await tenant(bound, "alpha", ["08:00", "20:00"], "UTC", { dailyRunCap: 2 });
    const queue = (period, date) => tenantRegistry(env).queueRun("alpha", { period, date, origin: "operator" });

    expect((await queue("morning", "2026-11-18")).queued).toBeDefined();
    await drainAlarms(registryOf(bound));
    at("2026-11-18T07:10:00Z");
    await finish(bound, "alpha");
    // One run done and one running count against the cap.
    expect((await queue("evening", "2026-11-17")).queued).toBeDefined();
    await drainAlarms(registryOf(bound));
    expect(await queue("morning", "2026-11-17")).toMatchObject({ code: "cap", refused: "alpha has reached its cap of 2 runs in 24 hours" });
    at("2026-11-18T07:20:00Z");
    await finish(bound, "alpha");

    // A container that rewrites its own schedule changes neither the cap nor its hours.
    bound.TENANT_DB.getByName("alpha").query(
      "UPDATE settings SET value = ? WHERE key = 'general.digest_schedule'",
      [JSON.stringify(["07:00", "19:00"])],
    );
    at("2026-11-18T19:00:00Z");
    await tick(bound);
    expect(bound.starts).toHaveLength(2);
    expect(logs.filter(([line]) => line === "run queued")).toHaveLength(2);
    expect(await tenantRegistry(env).tenant("alpha")).toMatchObject({ digest_times: '["08:00","20:00"]', daily_run_cap: 2 });

    // A day after the first run started, the cap has room again.
    at("2026-11-19T07:05:00Z");
    expect((await queue("morning", "2026-11-19")).queued).toBeDefined();
    // One digest time a day by default, tried by its tick and its catch-ups.
    expect(DEFAULT_MAX_DIGEST_TIMES).toBe(1);
    expect(DEFAULT_DAILY_RUN_CAP).toBe(DEFAULT_MAX_DIGEST_TIMES * CATCH_UP_HOURS);
  });

  it("frees the slots of runs no stop was reported for at their lease's end, and queues them behind the rest", async () => {
    at("2026-11-18T07:00:00Z");
    for (const id of ["alpha", "bravo", "charlie"]) await tenant(bound, id, ["08:00", "20:00"], "UTC");
    at("2026-11-18T08:00:00Z");
    await tick(bound);
    expect(bound.starts.map((start) => start.tenant)).toEqual(["alpha", "bravo"]);

    // Neither container is heard from. The alarm comes due a minute on, while charlie waits, and
    // the leases are not over yet; it comes due at their end, and charlie, never run, goes first.
    for (let minute = 1; minute < RUN_LEASE_MS / MINUTE; minute++) {
      vi.setSystemTime(Date.parse("2026-11-18T08:00:00Z") + minute * MINUTE);
      await drainAlarms(registryOf(bound));
    }
    expect(bound.starts).toHaveLength(2);
    vi.setSystemTime(Date.parse("2026-11-18T08:00:00Z") + RUN_LEASE_MS + 1_000);
    await drainAlarms(registryOf(bound));

    expect(bound.starts.map((start) => start.tenant)).toEqual(["alpha", "bravo", "charlie", "alpha"]);
    expect(runsOf(bound, "alpha").map((run) => [run.state, run.attempts, run.outcome])).toEqual([
      ["running", 2, null],
      ["lost", 1, "no stop reported within 20 minutes; queued again"],
    ]);
    expect(runsOf(bound, "bravo").map((run) => run.state)).toEqual(["queued", "lost"]);
    // The lease outlasts the container's own inactivity timeout, so a lost run's container is gone.
    expect(RUN_LEASE_MS).toBeGreaterThan(RUN_INACTIVITY_TIMEOUT_MS);
  });

  it("makes an issue whose run was never reported, though nothing else is queued (review repro R1)", async () => {
    at("2026-11-18T07:00:00Z");
    await tenant(bound, "alpha", ["08:00", "20:00"], "UTC");
    for (const hour of ["08", "09"]) {
      at(`2026-11-18T${hour}:00:00Z`);
      await tick(bound);
    }

    expect(bound.starts.map((start) => [start.tenant, start.period, start.date])).toEqual([
      ["alpha", "morning", "2026-11-18"],
      ["alpha", "morning", "2026-11-18"],
    ]);
  });

  it("expires a lease on its alarm alone, with no tick and no other run", async () => {
    at("2026-11-18T07:00:00Z");
    await tenant(bound, "alpha", ["08:00", "20:00"], "UTC");
    at("2026-11-18T08:00:00Z");
    await tick(bound);
    const registry = registryOf(bound);
    expect(registry.ctx.storage.alarm).toBe(Date.now() + RUN_LEASE_MS + 1_000);

    vi.setSystemTime(Date.now() + RUN_LEASE_MS + 1_000);
    await drainAlarms(registry);

    expect(bound.starts.map((start) => start.at)).toEqual(["2026-11-18T08:00:00+00:00", "2026-11-18T08:20:01+00:00"]);
    expect(registry.ctx.storage.alarm).toBe(Date.now() + RUN_LEASE_MS + 1_000);
  });

  it("ends a run whose lease ran out as done when its issue's ok run is in digest_runs, starting nothing", async () => {
    at("2026-11-18T07:00:00Z");
    await tenant(bound, "alpha", ["08:00", "20:00"], "UTC");
    at("2026-11-18T08:00:00Z");
    await tick(bound);
    // The run finished and wrote its row, but its stop report was lost.
    bound.TENANT_DB.getByName("alpha").query(
      "INSERT INTO digest_runs (finished_at, status, period, dry_run, fetched) VALUES ('2026-11-18T08:06:00+00:00', 'ok', 'morning', 0, 1)",
    );
    at("2026-11-18T08:20:01Z");
    await drainAlarms(registryOf(bound));

    expect(bound.starts).toHaveLength(1);
    expect(runsOf(bound, "alpha")[0]).toMatchObject({ state: "done", outcome: "its morning run is in digest_runs; no stop was reported" });
    expect(registryOf(bound).ctx.storage.alarm).toBeNull();
  });

  it("fails a run whose leases keep running out once it has had its last start", async () => {
    at("2026-11-18T07:00:00Z");
    await tenant(bound, "alpha", ["08:00", "20:00"], "UTC", { dailyRunCap: 24 });
    at("2026-11-18T08:00:00Z");
    await tick(bound);
    for (let lease = 1; lease <= MAX_START_ATTEMPTS; lease++) {
      vi.setSystemTime(Date.parse("2026-11-18T08:00:00Z") + lease * (RUN_LEASE_MS + 1_000));
      await drainAlarms(registryOf(bound));
    }

    expect(bound.starts).toHaveLength(MAX_START_ATTEMPTS);
    expect(runsOf(bound, "alpha")[0]).toMatchObject({
      state: "failed",
      attempts: MAX_START_ATTEMPTS,
      outcome: `no stop reported within 20 minutes, after ${MAX_START_ATTEMPTS} starts`,
    });
    expect(registryOf(bound).ctx.storage.alarm).toBeNull();
  });

  it("records a start the container refuses for any other reason as failed, at once", async () => {
    use(bindings({ refuse: () => new Error("tenant alpha is not an active registered tenant: paused") }));
    at("2026-11-18T07:00:00Z");
    await tenant(bound, "alpha", ["08:00", "20:00"], "UTC");
    at("2026-11-18T08:00:00Z");
    await tick(bound);

    expect(runsOf(bound, "alpha")[0]).toMatchObject({ state: "failed", attempts: 1 });
    expect(runsOf(bound, "alpha")[0].outcome).toContain("not an active registered tenant");
    expect(registryOf(bound).ctx.storage.alarm).toBeNull();
  });

  it("records a run whose container exited non-zero as failed, and leaves its catch-up to the next tick", async () => {
    at("2026-11-18T07:00:00Z");
    await tenant(bound, "alpha", ["08:00", "20:00"], "UTC");
    at("2026-11-18T08:00:00Z");
    await tick(bound);
    at("2026-11-18T08:04:00Z");
    await registryOf(bound).runEnded("alpha", { exitCode: 1, reason: "Error: exited with 1", run: ran(bound, "alpha") });
    at("2026-11-18T09:00:00Z");
    await tick(bound);

    expect(runsOf(bound, "alpha").map((run) => [run.origin, run.state])).toEqual([
      ["catch-up", "running"],
      ["tick", "failed"],
    ]);
    expect(bound.starts.map((start) => [start.period, start.date])).toEqual([
      ["morning", "2026-11-18"],
      ["morning", "2026-11-18"],
    ]);
  });

  it("ends only the start a stop report names: one naming none, or another start, leaves the held run running", async () => {
    at("2026-11-18T07:00:00Z");
    await tenant(bound, "alpha", ["08:00", "20:00"], "UTC");
    at("2026-11-18T08:00:00Z");
    await tick(bound);
    const { id, attempt } = ran(bound, "alpha");

    at("2026-11-18T08:04:00Z");
    for (const run of [undefined, { id: id + 1, attempt }, { id, attempt: attempt + 1 }]) {
      expect(await registryOf(bound).runEnded("alpha", { exitCode: 1, reason: "Error: exited with 1", run })).toBeNull();
    }
    expect(runsOf(bound, "alpha").map((run) => [run.id, run.state])).toEqual([[id, "running"]]);

    expect(await registryOf(bound).runEnded("alpha", { exitCode: 0, run: { id, attempt } })).toMatchObject({ id, state: "done" });
  });
});

describe("the registry's schedule check", () => {
  for (const { zone, stored } of ZONES.cases) {
    it(`${stored ? "stores" : "refuses"} the time zone ${JSON.stringify(zone)}${stored && stored !== zone ? ` as ${stored}` : ""}`, () => {
      const checked = checkSchedule({ times: ["08:00", "20:00"], timeZone: zone, maxDigestTimes: 2 });

      if (stored) expect(checked.schedule.timeZone).toBe(stored);
      else expect(checked.refused).toBe(`not an IANA time zone name, as Asia/Taipei: ${zone}`);
    });
  }

  it("keeps a tenant's cap when it is scheduled again with none named", () => {
    const registry = new TenantRegistry({ storage: alarmStorage() }, {});
    registry.register("alpha");
    registry.setSchedule("alpha", { times: ["08:00", "20:00"], timeZone: "UTC", maxDigestTimes: 2, dailyRunCap: 2 });

    const { tenant } = registry.setSchedule("alpha", { times: ["09:00", "21:00"], timeZone: "UTC", maxDigestTimes: 2 });

    expect(tenant).toMatchObject({ digest_times: '["09:00","21:00"]', daily_run_cap: 2 });
    expect(registry.setSchedule("alpha", { times: ["09:00", "21:00"], timeZone: "UTC", maxDigestTimes: 2, dailyRunCap: "4" }).tenant.daily_run_cap).toBe(4);
    registry.register("bravo");
    expect(registry.setSchedule("bravo", { times: ["09:00", "21:00"], timeZone: "UTC", maxDigestTimes: 2 }).tenant.daily_run_cap).toBe(DEFAULT_DAILY_RUN_CAP);
  });

  it("takes one whole hour, and two only for a tenant whose maximum is two", () => {
    const registry = new TenantRegistry({ storage: alarmStorage() }, {});
    registry.register("alpha");

    expect(registry.setSchedule("alpha", { times: ["13:00"], timeZone: "UTC" }).tenant).toMatchObject({
      digest_times: '["13:00"]',
      max_digest_times: DEFAULT_MAX_DIGEST_TIMES,
    });
    // Two times for a tenant allowed one are refused, and nothing is written.
    expect(registry.setSchedule("alpha", { times: ["08:00", "20:00"], timeZone: "UTC" }).refused).toBe(
      "this tenant may have 1 digest time a day: 08:00,20:00",
    );
    expect(registry.tenant("alpha").digest_times).toBe('["13:00"]');
    // The operator allows two; scheduled again with none named, the tenant keeps its maximum.
    expect(registry.setSchedule("alpha", { times: ["08:00", "20:00"], timeZone: "UTC", maxDigestTimes: "2" }).tenant.max_digest_times).toBe(2);
    expect(registry.setSchedule("alpha", { times: ["07:00", "19:00"], timeZone: "UTC" }).tenant).toMatchObject({
      digest_times: '["07:00","19:00"]',
      max_digest_times: 2,
    });
    // Lowering it to one with two times named is refused; with one time it is taken.
    expect(registry.setSchedule("alpha", { times: ["07:00", "19:00"], timeZone: "UTC", maxDigestTimes: 1 }).refused).toBeDefined();
    expect(registry.setSchedule("alpha", { times: ["07:00"], timeZone: "UTC", maxDigestTimes: 1 }).tenant.max_digest_times).toBe(1);
    // Never more than two times, nor a maximum past two.
    expect(checkSchedule({ times: ["07:00", "12:00", "19:00"], timeZone: "UTC", maxDigestTimes: 2 }).refused).toMatch(/^digest times are one whole hour/);
    for (const most of [0, 3, 1.5, "x"]) {
      expect(checkSchedule({ times: ["07:00"], timeZone: "UTC", maxDigestTimes: most }).refused).toMatch(/most digest times a day/);
    }
  });
});

describe("the registry's schedule columns", () => {
  it("are added to a registry created before them, with the default cap", () => {
    const sql = sqlStorage();
    sql.exec(
      "CREATE TABLE tenants (id TEXT PRIMARY KEY, status TEXT NOT NULL, digest_times TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    );
    sql.exec("INSERT INTO tenants VALUES ('alpha', 'active', NULL, 'then', 'then')");

    const registry = new TenantRegistry({ storage: alarmStorage(sql) }, {});

    expect(registry.tenant("alpha")).toMatchObject({
      timezone: null,
      daily_run_cap: DEFAULT_DAILY_RUN_CAP,
      max_digest_times: DEFAULT_MAX_DIGEST_TIMES,
    });
    // A second start finds them and adds nothing.
    expect(() => new TenantRegistry({ storage: alarmStorage(sql) }, {})).not.toThrow();
    expect(REGISTRY_SCHEMA).toContain("CREATE TABLE IF NOT EXISTS runs");
  });
});
