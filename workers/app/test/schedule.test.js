import { describe, it, expect, vi, afterEach } from "vitest";
import cases from "./schedule_cases.json" with { type: "json" };
import { CATCH_UP_HOURS, duePeriod, planTick, readTimeZone } from "../src/schedule.js";

const ENV = { CLOUDFLARE_ACCOUNT_ID: "acct-1", CYRIS_STORE_DATABASE_ID: "db-1", CLOUDFLARE_API_TOKEN: "tok" };

// D1's REST answer holding `settings` rows, values JSON-encoded as the store writes them.
const settings = (values) => ({
  success: true,
  result: [
    {
      success: true,
      results: Object.entries(values).map(([key, value]) => ({ key, value: JSON.stringify(value) })),
    },
  ],
});

function d1(answer) {
  const fetchImpl = async (url, init) => {
    fetchImpl.calls.push({ url, init });
    return answer(url, init);
  };
  fetchImpl.calls = [];
  return fetchImpl;
}

const TAIPEI_8AM = new Date("2026-08-27T00:00:00Z");
const TAIPEI_9AM = new Date("2026-08-27T01:00:00Z");
const SCHEDULE = { "general.digest_schedule": ["08:00", "20:00"], "general.timezone": "Asia/Taipei" };

afterEach(() => vi.restoreAllMocks());

describe("duePeriod agrees with service_layer/schedule.py's due_period", () => {
  for (const c of cases) {
    it(`${c.at} in ${c.timezone}, schedule ${JSON.stringify(c.schedule)} → ${c.due}`, () => {
      expect(duePeriod(new Date(c.at), c.schedule, c.timezone)).toBe(c.due);
    });
  }
});

// D1's REST answer to the tick's `digest_runs` read: the rows of `table` with the
// asked period and a `finished_at` at or after the asked instant, as the query selects.
const runsSince = (table) => (init) => {
  const [period, since] = JSON.parse(init.body).params;
  const rows = table.filter((row) => row.period === period && row.finished_at >= since);
  return Response.json({ success: true, result: [{ success: true, results: rows }] });
};

// One D1 for the whole tick: `settings` reads get `schedule`, `digest_runs` reads get `runs`.
function tickD1({ schedule = SCHEDULE, runs = runsSince([]) } = {}) {
  return d1((url, init) =>
    JSON.parse(init.body).sql.includes("digest_runs") ? runs(init) : Response.json(settings(schedule))
  );
}

const isRunsRead = ({ init }) => JSON.parse(init.body).sql.includes("digest_runs");

const TAIPEI_10AM = new Date("2026-08-27T02:00:00Z");
const TAIPEI_11AM = new Date("2026-08-27T03:00:00Z");
const MORNING_CATCH_UP = { period: "morning", date: "2026-08-27" };
const run = (fields) => ({ period: "morning", dry_run: 0, status: "ok", ...fields });

describe("planTick", () => {
  it("starts the container on a digest hour and lets --if-due pick the period", async () => {
    const fetchImpl = tickD1();
    expect(await planTick({ env: ENV, fetchImpl, now: () => TAIPEI_8AM })).toEqual({});
    const [{ url, init }] = fetchImpl.calls;
    expect(url).toBe("https://api.cloudflare.com/client/v4/accounts/acct-1/d1/database/db-1/query");
    expect(init.method).toBe("POST");
    expect(init.headers.Authorization).toBe("Bearer tok");
    expect(JSON.parse(init.body).params).toEqual(["general.digest_schedule", "general.timezone"]);
    expect(fetchImpl.calls).toHaveLength(1);
  });

  it("leaves the container asleep off the hour once the last digest hour is done", async () => {
    const fetchImpl = tickD1({ runs: runsSince([run({ finished_at: "2026-08-27T00:03:15+00:00" })]) });
    expect(await planTick({ env: ENV, fetchImpl, now: () => TAIPEI_9AM })).toBeNull();
  });

  it("reads the schedule on every tick, so a change applies at the next one", async () => {
    let schedule = SCHEDULE;
    const fetchImpl = d1((url, init) =>
      isRunsRead({ init }) ? runsSince([])(init) : Response.json(settings(schedule))
    );
    const tick = () => planTick({ env: ENV, fetchImpl, now: () => TAIPEI_11AM });
    expect(await tick()).toBeNull();
    schedule = { ...SCHEDULE, "general.digest_schedule": ["11:00", "21:00"] };
    expect(await tick()).toEqual({});
    expect(fetchImpl.calls).toHaveLength(2);
  });

  it("leaves a malformed schedule to never fire, as the container would", async () => {
    const fetchImpl = tickD1({ schedule: { ...SCHEDULE, "general.digest_schedule": ["08:30", "20:00"] } });
    expect(await planTick({ env: ENV, fetchImpl, now: () => TAIPEI_8AM })).toBeNull();
    expect(await planTick({ env: ENV, fetchImpl, now: () => TAIPEI_9AM })).toBeNull();
    expect(fetchImpl.calls.filter(isRunsRead)).toHaveLength(0);
  });

  // A run the platform killed leaves no ok row; the next ticks run that issue again.
  describe("catches up the last digest hour while it has no ok run", () => {
    it.each([
      ["one hour after", TAIPEI_9AM],
      ["two hours after", TAIPEI_10AM],
    ])("%s the morning hour", async (_, at) => {
      const fetchImpl = tickD1();
      expect(await planTick({ env: ENV, fetchImpl, now: () => at })).toEqual(MORNING_CATCH_UP);
      // One schedule read and one digest_runs read, never two of either.
      expect(fetchImpl.calls).toHaveLength(2);
      const [query] = fetchImpl.calls.filter(isRunsRead);
      expect(JSON.parse(query.init.body).params).toEqual(["morning", "2026-08-27T00:00:00+00:00"]);
    });

    it.each([
      ["only an error row", [run({ status: "error", finished_at: "2026-08-27T00:01:00+00:00" })]],
      ["only a dry run", [run({ dry_run: 1, finished_at: "2026-08-27T00:01:00+00:00" })]],
      ["an ok row from before the hour", [run({ finished_at: "2026-08-26T00:04:00+00:00" })]],
      ["an ok row for the other period", [run({ period: "evening", finished_at: "2026-08-27T00:30:00+00:00" })]],
    ])("with %s", async (_, table) => {
      const fetchImpl = tickD1({ runs: runsSince(table) });
      expect(await planTick({ env: ENV, fetchImpl, now: () => TAIPEI_9AM })).toEqual(MORNING_CATCH_UP);
    });

    it("but not with an ok row since the hour, beside an error", async () => {
      const table = [
        run({ status: "error", finished_at: "2026-08-27T00:01:00+00:00" }),
        run({ finished_at: "2026-08-27T01:00:40+00:00" }),
      ];
      const fetchImpl = tickD1({ runs: runsSince(table) });
      expect(await planTick({ env: ENV, fetchImpl, now: () => TAIPEI_10AM })).toBeNull();
    });

    it(`but not ${CATCH_UP_HOURS} hours or more after it`, async () => {
      expect(CATCH_UP_HOURS).toBe(3);
      const fetchImpl = tickD1();
      expect(await planTick({ env: ENV, fetchImpl, now: () => TAIPEI_11AM })).toBeNull();
      const later = new Date("2026-08-27T05:00:00Z");
      expect(await planTick({ env: ENV, fetchImpl, now: () => later })).toBeNull();
      expect(fetchImpl.calls.filter(isRunsRead)).toHaveLength(0);
    });

    it("dates an evening issue by its hour, past local midnight", async () => {
      const schedule = { ...SCHEDULE, "general.digest_schedule": ["08:00", "23:00"] };
      const fetchImpl = tickD1({ schedule });
      // 00:00 on 2026-08-28 in Taipei, an hour after the 23:00 evening hour.
      const midnight = new Date("2026-08-27T16:00:00Z");
      expect(await planTick({ env: ENV, fetchImpl, now: () => midnight })).toEqual({
        period: "evening",
        date: "2026-08-27",
      });
      const [query] = fetchImpl.calls.filter(isRunsRead);
      expect(JSON.parse(query.init.body).params).toEqual(["evening", "2026-08-27T15:00:00+00:00"]);
    });

    it("finds the hour's start in a half-hour time zone", async () => {
      // Kolkata is UTC+5:30, so the :00 UTC tick lands at :30 local.
      const schedule = { "general.digest_schedule": ["08:00", "20:00"], "general.timezone": "Asia/Kolkata" };
      const fetchImpl = tickD1({ schedule });
      const at = new Date("2026-08-27T05:00:00Z"); // 10:30 local
      expect(await planTick({ env: ENV, fetchImpl, now: () => at })).toEqual(MORNING_CATCH_UP);
      const [query] = fetchImpl.calls.filter(isRunsRead);
      expect(JSON.parse(query.init.body).params).toEqual(["morning", "2026-08-27T02:30:00+00:00"]);
      const tooLate = new Date("2026-08-27T06:00:00Z"); // 11:30 local, 3h30m after 08:00
      expect(await planTick({ env: ENV, fetchImpl, now: () => tooLate })).toBeNull();
    });
  });

  // A missed catch-up costs nothing new: the issue is already lost without it.
  describe("starts nothing off the hour when digest_runs cannot be read", () => {
    const failing = {
      "an error status": () => new Response("no", { status: 500 }),
      "a failed request": () => {
        throw new TypeError("network down");
      },
      "an unsuccessful query": () => Response.json({ success: false, result: [] }),
    };
    for (const [name, runs] of Object.entries(failing)) {
      it(name, async () => {
        const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
        const fetchImpl = tickD1({ runs });
        expect(await planTick({ env: ENV, fetchImpl, now: () => TAIPEI_9AM })).toBeNull();
        expect(warn).toHaveBeenCalled();
      });
    }
  });

  // Starting costs one idle container tick; skipping a digest hour costs an issue.
  // Off the hour that start is the --if-due one, never a catch-up: without the
  // schedule the tick cannot name a period, and the container finds no digest hour.
  describe("starts the container whenever the schedule cannot be read, and catches up nothing", () => {
    const unreadable = {
      "an error status": () => d1(() => new Response("no", { status: 403 })),
      "a failed request": () =>
        d1(() => {
          throw new TypeError("network down");
        }),
      "an unsuccessful query": () => d1(() => Response.json({ success: false, result: [] })),
      "a missing schedule row": () =>
        d1(() => Response.json(settings({ "general.timezone": "Asia/Taipei" }))),
      "a missing timezone row": () =>
        d1(() => Response.json(settings({ "general.digest_schedule": ["08:00", "20:00"] }))),
      "an unknown timezone": () =>
        d1(() => Response.json(settings({ ...SCHEDULE, "general.timezone": "Mars/Olympus" }))),
    };
    for (const [name, make] of Object.entries(unreadable)) {
      it(name, async () => {
        const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
        const fetchImpl = make();
        expect(await planTick({ env: ENV, fetchImpl, now: () => TAIPEI_9AM })).toEqual({});
        expect(warn).toHaveBeenCalled();
        expect(fetchImpl.calls).toHaveLength(1);
      });
    }

    it("no D1 to read", async () => {
      const fetchImpl = tickD1();
      expect(await planTick({ env: {}, fetchImpl, now: () => TAIPEI_9AM })).toEqual({});
      expect(fetchImpl.calls).toHaveLength(0);
    });
  });

  it("never logs the token", async () => {
    const logged = [];
    vi.spyOn(console, "warn").mockImplementation((...args) => logged.push(...args.map(String)));
    const echo = () => new Response("Bearer tok echoed", { status: 401 });
    await planTick({ env: ENV, fetchImpl: d1(echo), now: () => TAIPEI_9AM });
    await planTick({ env: ENV, fetchImpl: tickD1({ runs: echo }), now: () => TAIPEI_9AM });
    expect(logged.length).toBeGreaterThan(1);
    expect(logged.join(" ")).not.toContain("tok");
  });
});

describe("readTimeZone", () => {
  it("reads the stored time zone", async () => {
    const fetchImpl = d1(() => Response.json(settings(SCHEDULE)));
    expect(await readTimeZone({ env: ENV, fetchImpl })).toBe("Asia/Taipei");
  });

  it("is null when D1 cannot answer or there is no D1", async () => {
    const failing = d1(() => new Response("no", { status: 500 }));
    vi.spyOn(console, "warn").mockImplementation(() => {});
    expect(await readTimeZone({ env: ENV, fetchImpl: failing })).toBeNull();
    const unused = d1(() => Response.json(settings(SCHEDULE)));
    expect(await readTimeZone({ env: {}, fetchImpl: unused })).toBeNull();
    expect(unused.calls).toHaveLength(0);
  });
});
