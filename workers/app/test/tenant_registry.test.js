import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";
import { env } from "cloudflare:workers";
import { generateKeyPair, exportJWK, SignJWT } from "jose";
import worker from "../src/hosted.js";
import { LOCATION_HINT, TENANT } from "../src/tenant_db.js";
import { REGISTRY_NAME, REGISTRY_SCHEMA, tenantRegistry } from "../src/tenant_registry.js";
import { TENANT_STATUSES } from "../src/tenant_status.js";
import { DEFAULT_DAILY_RUN_CAP } from "../src/run_queue.js";
import { SCHEMA_SQL, drainAlarms, namespace, registryNamespace, registryObject } from "./sql_storage.js";

const AT = new Date("2026-11-18T06:00:00Z");
const STAMP = "2026-11-18T06:00:00+00:00";

describe("the operator's registry", () => {
  it("creates its own tables, none of them a tenant's", () => {
    const created = [...REGISTRY_SCHEMA.matchAll(/CREATE TABLE IF NOT EXISTS (\w+)/g)].map((m) => m[1]);
    const tenants = [...SCHEMA_SQL.matchAll(/CREATE TABLE IF NOT EXISTS (\w+)/g)].map((m) => m[1]);

    expect(created.sort()).toEqual(["inbound_addresses", "invites", "runs", "sessions", "spend_rollup", "tenants"]);
    expect(created.filter((name) => tenants.includes(name))).toEqual([]);
  });

  it("holds no tenant until one is registered", () => {
    const registry = registryObject();

    expect(registry.tenants()).toEqual([]);
    expect(registry.tenant("alpha")).toBeNull();
  });

  it("registers a tenant as active unless told otherwise, and answers its row", () => {
    const registry = registryObject();

    expect(registry.register("alpha", { now: AT })).toEqual({
      tenant: {
        id: "alpha",
        status: "active",
        digest_times: null,
        timezone: null,
        daily_run_cap: DEFAULT_DAILY_RUN_CAP,
        created_at: STAMP,
        updated_at: STAMP,
      },
    });
    expect(registry.register("bravo", { status: "paused", now: AT }).tenant.status).toBe("paused");
    expect(registry.tenants().map((row) => [row.id, row.status])).toEqual([
      ["alpha", "active"],
      ["bravo", "paused"],
    ]);
    expect(registry.tenants({ status: "active" }).map((row) => row.id)).toEqual(["alpha"]);
  });

  it("sets the status of a tenant registered again, and keeps when it was first registered", () => {
    const registry = registryObject();
    registry.register("alpha", { now: AT });

    const later = new Date("2026-11-19T06:00:00Z");
    const { tenant } = registry.register("alpha", { status: "paused", now: later });

    expect(tenant).toMatchObject({ status: "paused", created_at: STAMP, updated_at: "2026-11-19T06:00:00+00:00" });
    expect(registry.tenants()).toHaveLength(1);
  });

  it("keeps a paused tenant paused when it is registered again with no status named", () => {
    const registry = registryObject();
    registry.register("alpha", { now: AT });
    registry.register("alpha", { status: "paused", now: AT });

    const later = new Date("2026-11-19T06:00:00Z");
    const { tenant } = registry.register("alpha", { now: later });

    expect(tenant).toMatchObject({ status: "paused", created_at: STAMP, updated_at: STAMP });
    expect(registry.register("alpha", { status: "active", now: later }).tenant).toMatchObject({
      status: "active",
      updated_at: "2026-11-19T06:00:00+00:00",
    });
  });

  it("refuses an id outside the tenant pattern, or a status it does not know, and writes nothing", () => {
    const registry = registryObject();

    for (const id of ["Alpha", "../bravo", "", "a".repeat(41), REGISTRY_NAME, 7]) {
      expect(registry.register(id).refused).toMatch(/a tenant id matches/);
    }
    expect(registry.register("alpha", { status: "gone" }).refused).toBe(
      `a tenant's status is one of ${TENANT_STATUSES.join(", ")}, not gone`,
    );
    expect(registry.tenants()).toEqual([]);
  });

  it("is one object under a fixed name no tenant id can take, hinted where the tenants are", () => {
    const REGISTRY = registryNamespace();

    tenantRegistry({ REGISTRY });
    tenantRegistry({ REGISTRY });

    expect(TENANT.test(REGISTRY_NAME)).toBe(false);
    expect(REGISTRY.gets).toEqual([
      { name: REGISTRY_NAME, options: { locationHint: LOCATION_HINT } },
      { name: REGISTRY_NAME, options: { locationHint: LOCATION_HINT } },
    ]);
    expect(REGISTRY.objects.size).toBe(1);
  });
});

// The hosted Worker's entry itself, hosted.js, over the registry, the tenant databases and a
// TENANT_RUN that records each start instead of starting a container. The registry starts runs
// through the same TENANT_RUN.
function bindings() {
  const starts = [];
  const TENANT_RUN = {
    gets: [],
    getByName(tenant, options) {
      this.gets.push({ name: tenant, options });
      return {
        startRun: async (args) => {
          starts.push([tenant, args]);
          return { started: true, tenant };
        },
      };
    },
  };
  const bound = { TENANT_DB: namespace(), TENANT_RUN, starts };
  bound.REGISTRY = registryNamespace(bound);
  return bound;
}

// The registry object behind `env.REGISTRY`, whose alarm a test runs.
const registryOf = (bound) => bound.REGISTRY.getByName(REGISTRY_NAME);

// A tenant's database with the schema and the schedule the seed writes: 08:00 and 20:00 UTC.
function scheduled(TENANT_DB, tenant) {
  const db = TENANT_DB.getByName(tenant);
  db.query(SCHEMA_SQL);
  for (const [key, value] of [
    ["general.digest_schedule", ["08:00", "20:00"]],
    ["general.timezone", "UTC"],
  ]) {
    db.query("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)", [key, JSON.stringify(value), STAMP]);
  }
}

describe("the hosted Worker's hourly tick", () => {
  let bound;
  beforeEach(() => {
    bound = bindings();
    Object.assign(env, bound);
    vi.useFakeTimers({ toFake: ["Date"] });
  });
  afterEach(() => {
    vi.useRealTimers();
    for (const key of Object.keys(env)) delete env[key];
  });

  it("schedules only the registry's active tenants, and reads no other tenant's database", async () => {
    const registry = tenantRegistry(env);
    await registry.register("alpha");
    await registry.register("bravo", { status: "paused" });
    await registry.register("delta");
    for (const tenant of ["alpha", "bravo", "delta"]) {
      await registry.setSchedule(tenant, { times: ["08:00", "20:00"], timeZone: "UTC" });
    }
    // charlie has a database with a due schedule, but the registry never heard of it.
    for (const tenant of ["alpha", "bravo", "charlie", "delta"]) scheduled(bound.TENANT_DB, tenant);
    bound.TENANT_DB.gets.length = 0;
    vi.setSystemTime(new Date("2026-11-18T08:00:00Z"));
    vi.spyOn(console, "log").mockImplementation(() => {});

    await worker.scheduled();
    await drainAlarms(registryOf(bound));

    expect(bound.starts).toEqual([
      ["alpha", { period: "morning", date: "2026-11-18" }],
      ["delta", { period: "morning", date: "2026-11-18" }],
    ]);
    vi.restoreAllMocks();
    // Each is reached for its RSS poll and then its schedule; no other tenant's is reached at all.
    expect([...new Set(bound.TENANT_DB.gets.map((get) => get.name))].sort()).toEqual(["alpha", "delta"]);
  });

  it("starts nothing when the registry holds no tenant", async () => {
    scheduled(bound.TENANT_DB, "alpha");
    vi.setSystemTime(new Date("2026-11-18T08:00:00Z"));

    await worker.scheduled();

    expect(bound.starts).toEqual([]);
    expect(bound.REGISTRY.gets.map((get) => get.name)).toEqual([REGISTRY_NAME]);
  });

  it("starts no active tenant off its digest hours", async () => {
    await tenantRegistry(env).register("alpha");
    await tenantRegistry(env).setSchedule("alpha", { times: ["08:00", "20:00"], timeZone: "UTC" });
    scheduled(bound.TENANT_DB, "alpha");
    vi.setSystemTime(new Date("2026-11-18T13:00:00Z"));

    await worker.scheduled();
    await drainAlarms(registryOf(bound));

    expect(bound.starts).toEqual([]);
  });
});

describe("the hosted Worker's operator routes, over the registry", () => {
  const TEAM = "registry-team.example";
  const AUD = "AUD-REGISTRY";
  let bound;
  let privateKey;

  beforeEach(async () => {
    const keys = await generateKeyPair("RS256");
    privateKey = keys.privateKey;
    const jwk = { ...(await exportJWK(keys.publicKey)), kid: "k1", alg: "RS256", use: "sig" };
    // The Access team's certs, which accessKeys fetches once per team domain.
    vi.stubGlobal("fetch", async () => Response.json({ keys: [jwk] }));
    bound = bindings();
    Object.assign(env, bound, { CYRIS_ACCESS_TEAM_DOMAIN: `${crypto.randomUUID()}.${TEAM}`, CYRIS_ACCESS_AUD: AUD });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    for (const key of Object.keys(env)) delete env[key];
  });

  const operator = async (method, path) => {
    const jwt = await new SignJWT({})
      .setProtectedHeader({ alg: "RS256", kid: "k1" })
      .setIssuer(`https://${env.CYRIS_ACCESS_TEAM_DOMAIN}`)
      .setAudience(AUD)
      .setIssuedAt()
      .setExpirationTime(Math.floor(Date.now() / 1000) + 3600)
      .sign(privateKey);
    const request = new Request(`https://cyris-hosted.example.workers.dev${path}`, {
      method,
      headers: { "Cf-Access-Jwt-Assertion": jwt },
    });
    return worker.fetch(request);
  };

  it("refuse to seed, start, poll or report a tenant before it is registered, touching none of its objects", async () => {
    for (const [method, path] of [
      ["POST", "/hosted/tenants/alpha/seed"],
      ["POST", "/hosted/tenants/alpha/run?period=morning"],
      ["POST", "/hosted/tenants/alpha/poll"],
      ["GET", "/hosted/tenants/alpha/report"],
    ]) {
      const response = await operator(method, path);
      expect(response.status, path).toBe(404);
      expect(await response.json()).toEqual({ error: "alpha is not a registered tenant" });
    }
    expect(bound.starts).toEqual([]);
    expect(bound.TENANT_RUN.gets).toEqual([]);
    expect(bound.TENANT_DB.gets).toEqual([]);
  });

  it("register, seed, schedule and start a tenant, and start none that is paused", async () => {
    vi.spyOn(console, "log").mockImplementation(() => {});
    const registered = await operator("POST", "/hosted/tenants/alpha/register");
    expect(registered.status).toBe(200);
    expect(await registered.json()).toMatchObject({ id: "alpha", status: "active" });

    const seeded = await operator("POST", "/hosted/tenants/alpha/seed");
    expect(seeded.status).toBe(200);
    expect((await seeded.json()).tenant).toBe("alpha");

    const unscheduled = await operator("POST", "/hosted/tenants/alpha/run?period=morning");
    expect(unscheduled.status).toBe(409);
    expect((await unscheduled.json()).error).toContain("has no schedule in the registry");

    const schedule = await operator("POST", "/hosted/tenants/alpha/schedule?times=07:00,19:00&timezone=asia/taipei");
    expect(schedule.status).toBe(200);
    expect(await schedule.json()).toMatchObject({
      tenant: { id: "alpha", digest_times: '["07:00","19:00"]', timezone: "Asia/Taipei" },
      settings: true,
    });
    // The tenant's own settings hold a copy, so its run dates and windows in the same zone.
    const copied = bound.TENANT_DB.getByName("alpha").query(
      "SELECT key, value FROM settings WHERE key IN ('general.digest_schedule', 'general.timezone') ORDER BY key",
    ).rows;
    expect(copied).toEqual([
      { key: "general.digest_schedule", value: '["07:00","19:00"]' },
      { key: "general.timezone", value: '"Asia/Taipei"' },
    ]);

    const ran = await operator("POST", "/hosted/tenants/alpha/run?period=morning");
    expect(ran.status).toBe(202);
    expect((await ran.json()).queued).toMatchObject({ tenant_id: "alpha", period: "morning", state: "queued" });
    await drainAlarms(registryOf(bound));
    // Named in the tenant's zone when it was queued: 2026-11-18 there at any hour of the test's.
    const [[tenant, { period, date }]] = bound.starts;
    expect([tenant, period]).toEqual(["alpha", "morning"]);
    expect(date).toMatch(/^\d{4}-\d{2}-\d{2}$/);

    const report = await (await operator("GET", "/hosted/tenants/alpha/report")).json();
    expect(report.runs.map((run) => [run.period, run.state, run.origin])).toEqual([["morning", "running", "operator"]]);

    expect((await operator("POST", "/hosted/tenants/alpha/register?status=paused")).status).toBe(200);
    expect((await operator("POST", "/hosted/tenants/alpha/run?period=evening")).status).toBe(409);
    expect(bound.starts).toHaveLength(1);
    vi.restoreAllMocks();
  });

  it("refuse a schedule the registry does not take, and write nothing", async () => {
    await operator("POST", "/hosted/tenants/alpha/register");

    for (const query of [
      "?timezone=UTC",
      "?times=08:00&timezone=UTC",
      "?times=08:30,20:00&timezone=UTC",
      "?times=08:00,20:00",
      "?times=08:00,20:00&timezone=Mars/Olympus",
      "?times=08:00,20:00&timezone=UTC&daily_run_cap=0",
      "?times=08:00,20:00&timezone=UTC&daily_run_cap=2.5",
    ]) {
      const response = await operator("POST", `/hosted/tenants/alpha/schedule${query}`);
      expect(response.status, query).toBe(400);
    }
    expect(await tenantRegistry(env).tenant("alpha")).toMatchObject({ digest_times: null, timezone: null });
    expect((await operator("POST", "/hosted/tenants/charlie/schedule?times=08:00,20:00&timezone=UTC")).status).toBe(404);
  });

  it("poll an active tenant's feeds into its buffer on request, and refuse a paused one", async () => {
    const certs = globalThis.fetch;
    const feeds = [];
    // The seed's feeds answer one entry each; the Access certs are fetched as before.
    vi.stubGlobal("fetch", async (url, init) => {
      if (String(url).includes(TEAM)) return certs(url, init);
      feeds.push(String(url));
      const item = `<item><title>t</title><link>${url}/one</link><pubDate>${new Date().toUTCString()}</pubDate></item>`;
      return new Response(`<?xml version="1.0"?><rss version="2.0"><channel>${item}</channel></rss>`);
    });
    vi.spyOn(console, "log").mockImplementation(() => {});
    await operator("POST", "/hosted/tenants/alpha/register");
    await operator("POST", "/hosted/tenants/alpha/seed");

    const polled = await operator("POST", "/hosted/tenants/alpha/poll");

    expect(polled.status).toBe(200);
    const body = await polled.json();
    expect(body).toMatchObject({ tenant: "alpha", feeds: feeds.length, fetches: feeds.length, written: feeds.length });
    expect(feeds.length).toBeGreaterThan(0);
    const buffered = bound.TENANT_DB.getByName("alpha").query("SELECT count(*) AS n FROM articles").rows[0].n;
    expect(buffered).toBe(feeds.length);
    expect(bound.starts).toEqual([]);

    await operator("POST", "/hosted/tenants/alpha/register?status=paused");
    const paused = await operator("POST", "/hosted/tenants/alpha/poll");
    expect(paused.status).toBe(409);
    expect(feeds).toHaveLength(body.feeds);
    vi.restoreAllMocks();
  });

  it("answer 502 with the reason when a tenant's poll fails", async () => {
    vi.spyOn(console, "log").mockImplementation(() => {});
    await operator("POST", "/hosted/tenants/alpha/register");

    // Registered but never seeded: it has no sources table to read.
    const polled = await operator("POST", "/hosted/tenants/alpha/poll");

    expect(polled.status).toBe(502);
    expect((await polled.json()).error).toContain("could not read sources");
    vi.restoreAllMocks();
  });

  it("leave a paused tenant paused on a bare register, and resume it only when told", async () => {
    vi.spyOn(console, "log").mockImplementation(() => {});
    expect((await operator("POST", "/hosted/tenants/alpha/register?status=paused")).status).toBe(200);
    await operator("POST", "/hosted/tenants/alpha/schedule?times=08:00,20:00&timezone=UTC");

    const again = await operator("POST", "/hosted/tenants/alpha/register");

    expect(again.status).toBe(200);
    expect((await again.json()).status).toBe("paused");
    expect((await operator("POST", "/hosted/tenants/alpha/run?period=morning")).status).toBe(409);
    expect((await operator("POST", "/hosted/tenants/alpha/register?status=active")).status).toBe(200);
    expect((await operator("POST", "/hosted/tenants/alpha/run?period=morning")).status).toBe(202);
    await drainAlarms(registryOf(bound));
    expect(bound.starts.map(([tenant, { period }]) => [tenant, period])).toEqual([["alpha", "morning"]]);
    vi.restoreAllMocks();
  });

  it("refuse to register an id or a status the registry does not take", async () => {
    const id = await operator("POST", "/hosted/tenants/Alpha/register");
    const status = await operator("POST", "/hosted/tenants/alpha/register?status=gone");

    expect([id.status, status.status]).toEqual([400, 400]);
    expect(await tenantRegistry(env).tenants()).toEqual([]);
  });
});
