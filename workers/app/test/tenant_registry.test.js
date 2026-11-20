import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";
import { env } from "cloudflare:workers";
import { generateKeyPair, exportJWK, SignJWT } from "jose";
import worker from "../src/hosted.js";
import { LOCATION_HINT, TENANT } from "../src/tenant_db.js";
import { REGISTRY_NAME, REGISTRY_SCHEMA, tenantRegistry } from "../src/tenant_registry.js";
import { TENANT_STATUSES } from "../src/tenant_status.js";
import { SCHEMA_SQL, namespace, registryNamespace, registryObject } from "./sql_storage.js";

const AT = new Date("2026-11-18T06:00:00Z");
const STAMP = "2026-11-18T06:00:00+00:00";

describe("the operator's registry", () => {
  it("creates its own tables, none of them a tenant's", () => {
    const created = [...REGISTRY_SCHEMA.matchAll(/CREATE TABLE IF NOT EXISTS (\w+)/g)].map((m) => m[1]);
    const tenants = [...SCHEMA_SQL.matchAll(/CREATE TABLE IF NOT EXISTS (\w+)/g)].map((m) => m[1]);

    expect(created.sort()).toEqual(["inbound_addresses", "invites", "sessions", "spend_rollup", "tenants"]);
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
      tenant: { id: "alpha", status: "active", digest_times: null, created_at: STAMP, updated_at: STAMP },
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
// TENANT_RUN that records each start instead of starting a container.
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
  return { REGISTRY: registryNamespace(), TENANT_DB: namespace(), TENANT_RUN, starts };
}

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
    // charlie has a database with a due schedule, but the registry never heard of it.
    for (const tenant of ["alpha", "bravo", "charlie", "delta"]) scheduled(bound.TENANT_DB, tenant);
    bound.TENANT_DB.gets.length = 0;
    vi.setSystemTime(new Date("2026-11-18T08:00:00Z"));

    await worker.scheduled();

    expect(bound.starts).toEqual([
      ["alpha", { period: undefined, date: undefined }],
      ["delta", { period: undefined, date: undefined }],
    ]);
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
    scheduled(bound.TENANT_DB, "alpha");
    vi.setSystemTime(new Date("2026-11-18T13:00:00Z"));

    await worker.scheduled();

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

  const signIn = () =>
    new SignJWT({})
      .setProtectedHeader({ alg: "RS256", kid: "k1" })
      .setIssuer(`https://${env.CYRIS_ACCESS_TEAM_DOMAIN}`)
      .setAudience(AUD)
      .setIssuedAt()
      .setExpirationTime(Math.floor(Date.now() / 1000) + 3600)
      .sign(privateKey);

  const operator = async (method, path, body) => {
    const request = new Request(`https://cyris-hosted.example.workers.dev${path}`, {
      method,
      headers: { "Cf-Access-Jwt-Assertion": await signIn() },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    return worker.fetch(request);
  };

  it("refuse to seed, start, poll, report or query a tenant before it is registered, touching none of its objects", async () => {
    for (const [method, path] of [
      ["POST", "/hosted/tenants/alpha/seed"],
      ["POST", "/hosted/tenants/alpha/run?period=morning"],
      ["POST", "/hosted/tenants/alpha/poll"],
      ["GET", "/hosted/tenants/alpha/report"],
      ["POST", "/hosted/tenants/alpha/query"],
    ]) {
      const response = await operator(method, path);
      expect(response.status, path).toBe(404);
      expect(await response.json()).toEqual({ error: "alpha is not a registered tenant" });
    }
    expect(bound.starts).toEqual([]);
    expect(bound.TENANT_RUN.gets).toEqual([]);
    expect(bound.TENANT_DB.gets).toEqual([]);
  });

  it("register, seed and start a tenant, and start none that is paused", async () => {
    const registered = await operator("POST", "/hosted/tenants/alpha/register");
    expect(registered.status).toBe(200);
    expect(await registered.json()).toMatchObject({ id: "alpha", status: "active" });

    const seeded = await operator("POST", "/hosted/tenants/alpha/seed");
    expect(seeded.status).toBe(200);
    expect((await seeded.json()).tenant).toBe("alpha");

    const ran = await operator("POST", "/hosted/tenants/alpha/run?period=morning");
    expect(ran.status).toBe(200);
    expect(bound.starts).toEqual([["alpha", { period: "morning", date: undefined }]]);

    expect((await operator("POST", "/hosted/tenants/alpha/register?status=paused")).status).toBe(200);
    expect((await operator("POST", "/hosted/tenants/alpha/run?period=morning")).status).toBe(409);
    expect(bound.starts).toHaveLength(1);
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
    expect((await operator("POST", "/hosted/tenants/alpha/register?status=paused")).status).toBe(200);

    const again = await operator("POST", "/hosted/tenants/alpha/register");

    expect(again.status).toBe(200);
    expect((await again.json()).status).toBe("paused");
    expect((await operator("POST", "/hosted/tenants/alpha/run?period=morning")).status).toBe(409);
    expect((await operator("POST", "/hosted/tenants/alpha/register?status=active")).status).toBe(200);
    expect((await operator("POST", "/hosted/tenants/alpha/run?period=morning")).status).toBe(200);
    expect(bound.starts).toEqual([["alpha", { period: "morning", date: undefined }]]);
  });

  it("query read and write one registered tenant's own database, and no other tenant's", async () => {
    await operator("POST", "/hosted/tenants/alpha/register");
    await operator("POST", "/hosted/tenants/bravo/register");
    await operator("POST", "/hosted/tenants/alpha/seed");
    await operator("POST", "/hosted/tenants/bravo/seed");
    const sql = "UPDATE settings SET value = ? WHERE key = ?";

    const written = await operator("POST", "/hosted/tenants/alpha/query", { sql, params: ['"Asia/Taipei"', "general.timezone"] });
    const read = await operator("POST", "/hosted/tenants/alpha/query", {
      sql: "SELECT value FROM settings WHERE key = ?",
      params: ["general.timezone"],
    });

    expect(written.status).toBe(200);
    expect(await written.json()).toEqual({ rows: [], changes: 1 });
    expect(await read.json()).toEqual({ rows: [{ value: '"Asia/Taipei"' }], changes: 0 });
    const bravo = bound.TENANT_DB.getByName("bravo").query("SELECT value FROM settings WHERE key = ?", ["general.timezone"]);
    expect(bravo.rows).toEqual([{ value: '"UTC"' }]);
    expect(bound.starts).toEqual([]);
  });

  it("query a paused tenant too, since only its runs stop", async () => {
    await operator("POST", "/hosted/tenants/alpha/register?status=paused");

    const response = await operator("POST", "/hosted/tenants/alpha/query", { sql: "SELECT 1 AS one" });

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ rows: [{ one: 1 }], changes: 0 });
  });

  it("answer a refused query in D1's error shape, which the operator's client reads", async () => {
    await operator("POST", "/hosted/tenants/alpha/register");

    const missing = await operator("POST", "/hosted/tenants/alpha/query", { sql: "SELECT * FROM nowhere" });
    const shapeless = await operator("POST", "/hosted/tenants/alpha/query", { sql: 7 });
    const wrongMethod = await operator("GET", "/hosted/tenants/alpha/query");

    expect(missing.status).toBe(400);
    expect((await missing.json()).errors[0].message).toContain("no such table: nowhere");
    expect(shapeless.status).toBe(400);
    expect(await shapeless.json()).toEqual({ errors: [{ message: "the body must be {sql, params}" }] });
    expect(wrongMethod.status).toBe(405);
  });

  it("refuse a query without the operator's Access sign-in before any object is reached", async () => {
    await tenantRegistry(env).register("alpha");
    bound.REGISTRY.gets.length = 0;
    const body = JSON.stringify({ sql: "SELECT * FROM settings" });
    const url = "https://cyris-hosted.example.workers.dev/hosted/tenants/alpha/query";
    const forged = await new SignJWT({})
      .setProtectedHeader({ alg: "RS256", kid: "k1" })
      .setIssuer("https://someone-else.cloudflareaccess.com")
      .setAudience(AUD)
      .setExpirationTime(Math.floor(Date.now() / 1000) + 3600)
      .sign(privateKey);

    for (const headers of [{}, { "Cf-Access-Jwt-Assertion": "not-a-jwt" }, { "Cf-Access-Jwt-Assertion": forged }]) {
      const response = await worker.fetch(new Request(url, { method: "POST", headers, body }));
      expect(response.status).toBe(403);
      expect((await response.json()).error).toBe("forbidden");
    }
    // A Worker with no Access settings fails closed, whatever the request carries.
    delete env.CYRIS_ACCESS_AUD;
    const unset = await worker.fetch(new Request(url, { method: "POST", headers: { "Cf-Access-Jwt-Assertion": await signIn() }, body }));
    expect(unset.status).toBe(403);
    expect(bound.REGISTRY.gets).toEqual([]);
    expect(bound.TENANT_DB.gets).toEqual([]);
  });

  it("refuse to register an id or a status the registry does not take", async () => {
    const id = await operator("POST", "/hosted/tenants/Alpha/register");
    const status = await operator("POST", "/hosted/tenants/alpha/register?status=gone");

    expect([id.status, status.status]).toEqual([400, 400]);
    expect(await tenantRegistry(env).tenants()).toEqual([]);
  });
});
