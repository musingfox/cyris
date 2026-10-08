import { describe, it, expect, vi, afterEach } from "vitest";
import cases from "./schedule_cases.json" with { type: "json" };
import { duePeriod, shouldStartRun } from "../src/schedule.js";

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

describe("shouldStartRun", () => {
  it("starts the container on a digest hour", async () => {
    const fetchImpl = d1(() => Response.json(settings(SCHEDULE)));
    expect(await shouldStartRun({ env: ENV, fetchImpl, now: () => TAIPEI_8AM })).toBe(true);
    const [{ url, init }] = fetchImpl.calls;
    expect(url).toBe("https://api.cloudflare.com/client/v4/accounts/acct-1/d1/database/db-1/query");
    expect(init.method).toBe("POST");
    expect(init.headers.Authorization).toBe("Bearer tok");
    expect(JSON.parse(init.body).params).toEqual(["general.digest_schedule", "general.timezone"]);
  });

  it("leaves the container asleep off the hour", async () => {
    const fetchImpl = d1(() => Response.json(settings(SCHEDULE)));
    expect(await shouldStartRun({ env: ENV, fetchImpl, now: () => TAIPEI_9AM })).toBe(false);
  });

  it("reads the schedule on every tick, so a change applies at the next one", async () => {
    let schedule = SCHEDULE;
    const fetchImpl = d1(() => Response.json(settings(schedule)));
    const tick = () => shouldStartRun({ env: ENV, fetchImpl, now: () => TAIPEI_9AM });
    expect(await tick()).toBe(false);
    schedule = { ...SCHEDULE, "general.digest_schedule": ["09:00", "21:00"] };
    expect(await tick()).toBe(true);
    expect(fetchImpl.calls).toHaveLength(2);
  });

  it("leaves a malformed schedule to never fire, as the container would", async () => {
    const fetchImpl = d1(() =>
      Response.json(settings({ ...SCHEDULE, "general.digest_schedule": ["08:30", "20:00"] }))
    );
    expect(await shouldStartRun({ env: ENV, fetchImpl, now: () => TAIPEI_8AM })).toBe(false);
  });

  // Starting costs one idle container tick; skipping a digest hour costs an issue.
  describe("starts the container whenever the schedule cannot be read", () => {
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
        vi.spyOn(console, "warn").mockImplementation(() => {});
        expect(await shouldStartRun({ env: ENV, fetchImpl: make(), now: () => TAIPEI_9AM })).toBe(true);
      });
    }

    it("no D1 to read", async () => {
      const fetchImpl = d1(() => Response.json(settings(SCHEDULE)));
      const env = { ...ENV, CYRIS_STORE_BACKEND: "json" };
      expect(await shouldStartRun({ env, fetchImpl, now: () => TAIPEI_9AM })).toBe(true);
      expect(await shouldStartRun({ env: {}, fetchImpl, now: () => TAIPEI_9AM })).toBe(true);
      expect(fetchImpl.calls).toHaveLength(0);
    });
  });

  it("never logs the token", async () => {
    const logged = [];
    vi.spyOn(console, "warn").mockImplementation((...args) => logged.push(...args.map(String)));
    const fetchImpl = d1(() => new Response("Bearer tok echoed", { status: 401 }));
    await shouldStartRun({ env: ENV, fetchImpl, now: () => TAIPEI_9AM });
    expect(logged.join(" ")).not.toContain("tok");
  });
});
