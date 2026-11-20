import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";
import { env } from "cloudflare:workers";
import { generateKeyPair, exportJWK, SignJWT } from "jose";
import worker from "../src/hosted.js";
import { LOCATION_HINT, TENANT } from "../src/tenant_db.js";
import RESERVED_IDS from "../src/hosted_reserved_ids.json" with { type: "json" };
import {
  SESSION_TTL_MS,
  SIGN_IN_DAILY_CAP,
  SIGN_IN_DAY_MS,
  SIGN_IN_GAP_MS,
  SIGN_IN_HOURLY_CAP,
  SIGN_IN_HOUR_MS,
  SIGN_IN_TTL_MS,
} from "../src/hosted_session.js";
import { REGISTRY_NAME, REGISTRY_SCHEMA, tenantRegistry } from "../src/tenant_registry.js";
import { TENANT_STATUSES } from "../src/tenant_status.js";
import { SCHEMA_SQL, namespace, registryNamespace, registryObject } from "./sql_storage.js";

const AT = new Date("2026-11-18T06:00:00Z");
const STAMP = "2026-11-18T06:00:00+00:00";
const later = (ms) => new Date(AT.getTime() + ms);

// The registry's sign-in rows, as the operator's invite and a reader's sign-in leave them.
describe("the registry's sign-in", () => {
  // alpha and bravo are registered; ana is on alpha's list, bea on bravo's.
  function signInRegistry() {
    const registry = registryObject();
    registry.register("alpha", { now: AT });
    registry.register("bravo", { now: AT });
    registry.invite("alpha", "ana@example.com", { now: AT });
    registry.invite("bravo", "bea@example.com", { now: AT });
    return registry;
  }
  const rows = (registry, table) => registry.ctx.storage.sql.exec(`SELECT * FROM ${table}`).toArray();

  it("puts an address on one tenant's list only, as the operator names it, trimmed and lower-cased", () => {
    const registry = signInRegistry();

    expect(registry.invite("alpha", "  Ana@Example.COM ", { now: later(1000) })).toEqual({
      member: { email: "ana@example.com", tenant_id: "alpha", created_at: STAMP },
    });
    expect(registry.invite("bravo", "ana@example.com")).toEqual({
      refused: "ana@example.com is already invited to alpha",
      status: 409,
    });
    expect(registry.invite("charlie", "cy@example.com")).toMatchObject({ status: 404 });
    expect(registry.invite("alpha", "not an address")).toMatchObject({ status: 400 });
    expect(registry.members("alpha", { now: AT })).toEqual([{ email: "ana@example.com", created_at: STAMP, sessions: 0 }]);
  });

  it("issues a link only to an address on a registered tenant's list, for that tenant, keyed by its hash", () => {
    const registry = signInRegistry();

    expect(registry.issueSignIn("stranger@example.com", "h-stranger", { now: AT })).toEqual({ refused: "not invited" });
    expect(registry.issueSignIn("ANA@example.com", "h-ana", { now: AT })).toEqual({
      tenant: "alpha",
      expires_at: "2026-11-18T06:15:00+00:00",
    });
    expect(rows(registry, "invites")).toEqual([
      {
        code: "h-ana",
        email: "ana@example.com",
        tenant_id: "alpha",
        created_at: STAMP,
        expires_at: "2026-11-18T06:15:00+00:00",
        used_at: null,
      },
    ]);
  });

  it("mails an address at most one link a minute", () => {
    const registry = signInRegistry();
    registry.issueSignIn("ana@example.com", "h-1", { now: AT });

    expect(registry.issueSignIn("ana@example.com", "h-2", { now: later(SIGN_IN_GAP_MS - 1000) }).refused).toMatch(/less than a minute/);
    expect(registry.issueSignIn("ana@example.com", "h-3", { now: later(SIGN_IN_GAP_MS + 1000) }).tenant).toBe("alpha");
    expect(rows(registry, "invites").map((row) => row.code)).toEqual(["h-1", "h-3"]);
  });

  it("issues one address at most SIGN_IN_DAILY_CAP links in any 24 hours, the invitation among them", () => {
    const registry = signInRegistry();
    const step = SIGN_IN_GAP_MS + 1000;
    for (let i = 0; i < SIGN_IN_DAILY_CAP; i++) {
      expect(registry.issueSignIn("ana@example.com", `h-${i}`, { now: later(i * step) }).tenant).toBe("alpha");
    }

    const past = registry.issueSignIn("ana@example.com", "h-past", { now: later(SIGN_IN_DAILY_CAP * step) });
    expect(past).toMatchObject({ capped: "daily" });
    expect(past.tenant).toBeUndefined();
    expect(rows(registry, "invites")).toHaveLength(SIGN_IN_DAILY_CAP);
    // Another address is not held by ana's cap, and a day after ana's first link she gets one again.
    expect(registry.issueSignIn("bea@example.com", "h-bea", { now: later(SIGN_IN_DAILY_CAP * step) }).tenant).toBe("bravo");
    expect(registry.issueSignIn("ana@example.com", "h-next", { now: later(SIGN_IN_DAY_MS + 1000) }).tenant).toBe("alpha");
  });

  it("issues every address together at most SIGN_IN_HOURLY_CAP links in any hour", () => {
    const registry = signInRegistry();
    const cohort = Array.from({ length: SIGN_IN_HOURLY_CAP }, (_, i) => `reader${i}@example.com`);
    for (const email of cohort) registry.invite("alpha", email, { now: AT });
    for (const email of cohort) expect(registry.issueSignIn(email, `h-${email}`, { now: AT }).tenant).toBe("alpha");

    const past = registry.issueSignIn("ana@example.com", "h-ana", { now: later(SIGN_IN_HOUR_MS - 1000) });
    expect(past).toMatchObject({ capped: "hourly" });
    expect(rows(registry, "invites").map((row) => row.email)).not.toContain("ana@example.com");
    expect(registry.issueSignIn("ana@example.com", "h-ana", { now: later(SIGN_IN_HOUR_MS + 1000) }).tenant).toBe("alpha");
  });

  it("writes nothing for an address off the list, however often it asks, and counts none against the caps", () => {
    const registry = signInRegistry();
    const before = JSON.stringify(["members", "invites", "sessions", "tenants"].map((table) => rows(registry, table)));

    for (let i = 0; i < SIGN_IN_HOURLY_CAP * 10; i++) {
      expect(registry.issueSignIn(`stranger${i}@example.com`, `h-${i}`, { now: AT })).toEqual({ refused: "not invited" });
    }

    expect(JSON.stringify(["members", "invites", "sessions", "tenants"].map((table) => rows(registry, table)))).toBe(before);
    expect(registry.issueSignIn("ana@example.com", "h-ana", { now: AT }).tenant).toBe("alpha");
  });

  it("keeps a link's row a day past its issue, to count it, and then deletes it", () => {
    const registry = signInRegistry();
    registry.issueSignIn("ana@example.com", "h-ana", { now: AT });

    registry.issueSignIn("bea@example.com", "h-bea", { now: later(SIGN_IN_TTL_MS + 1000) });
    expect(rows(registry, "invites").map((row) => row.code)).toEqual(["h-ana", "h-bea"]);
    registry.issueSignIn("stranger@example.com", "h-x", { now: later(SIGN_IN_DAY_MS) });
    expect(rows(registry, "invites").map((row) => row.code)).toEqual(["h-bea"]);
  });

  it("opens a session for the link's own address and tenant, once", () => {
    const registry = signInRegistry();
    registry.issueSignIn("bea@example.com", "h-bea", { now: AT });

    expect(registry.redeemSignIn("h-bea", "s-1", { now: later(60_000) })).toEqual({
      tenant: "bravo",
      expires_at: new Date(AT.getTime() + 60_000 + SESSION_TTL_MS).toISOString().slice(0, 19) + "+00:00",
    });
    expect(registry.session("s-1", { now: later(120_000) })).toMatchObject({ tenant_id: "bravo", email: "bea@example.com" });
    // The same link again opens nothing, and leaves the first session as it was.
    expect(registry.redeemSignIn("h-bea", "s-2", { now: later(120_000) })).toBeNull();
    expect(registry.session("s-2", { now: later(120_000) })).toBeNull();
    expect(rows(registry, "sessions").map((row) => row.token_hash)).toEqual(["s-1"]);
  });

  it("opens no session for a link past its time, or one it never issued", () => {
    const registry = signInRegistry();
    registry.issueSignIn("ana@example.com", "h-ana", { now: AT });

    expect(registry.redeemSignIn("h-ana", "s-late", { now: later(SIGN_IN_TTL_MS + 1000) })).toBeNull();
    expect(registry.redeemSignIn("h-never", "s-never", { now: AT })).toBeNull();
    expect(rows(registry, "sessions")).toEqual([]);
  });

  it("refuses a session past its time", () => {
    const registry = signInRegistry();
    registry.issueSignIn("ana@example.com", "h-ana", { now: AT });
    registry.redeemSignIn("h-ana", "s-ana", { now: AT });

    expect(registry.session("s-ana", { now: later(SESSION_TTL_MS - 1000) })).not.toBeNull();
    expect(registry.session("s-ana", { now: later(SESSION_TTL_MS + 1000) })).toBeNull();
  });

  it("ends a session at once when it signs out, or when its address is taken off the list", () => {
    const registry = signInRegistry();
    for (const [email, code, session] of [
      ["ana@example.com", "h-ana", "s-ana"],
      ["bea@example.com", "h-bea", "s-bea"],
    ]) {
      registry.issueSignIn(email, code, { now: AT });
      registry.redeemSignIn(code, session, { now: AT });
    }
    registry.issueSignIn("ana@example.com", "h-ana-2", { now: later(SIGN_IN_GAP_MS + 1000) });

    registry.endSession("s-bea");
    expect(registry.session("s-bea", { now: AT })).toBeNull();

    expect(registry.revoke("bravo", "ana@example.com")).toMatchObject({ status: 404 });
    expect(registry.revoke("alpha", "ana@example.com")).toEqual({
      revoked: { email: "ana@example.com", tenant_id: "alpha", sessions: 1 },
    });
    expect(registry.session("s-ana", { now: AT })).toBeNull();
    // Its unspent link went with it, and it is no longer on any list.
    expect(registry.redeemSignIn("h-ana-2", "s-ana-2", { now: later(SIGN_IN_GAP_MS + 2000) })).toBeNull();
    expect(registry.issueSignIn("ana@example.com", "h-ana-3", { now: later(SIGN_IN_TTL_MS) })).toEqual({ refused: "not invited" });
    expect(rows(registry, "members").map((row) => row.email)).toEqual(["bea@example.com"]);
  });

  it("opens no session for a link whose address left the list after it was mailed", () => {
    const registry = signInRegistry();
    registry.issueSignIn("ana@example.com", "h-ana", { now: AT });
    registry.ctx.storage.sql.exec("DELETE FROM members WHERE email = ?", "ana@example.com");

    expect(registry.redeemSignIn("h-ana", "s-ana", { now: AT })).toBeNull();
    expect(rows(registry, "sessions")).toEqual([]);
  });
});

describe("the operator's registry", () => {
  it("creates its own tables, none of them a tenant's", () => {
    const created = [...REGISTRY_SCHEMA.matchAll(/CREATE TABLE IF NOT EXISTS (\w+)/g)].map((m) => m[1]);
    const tenants = [...SCHEMA_SQL.matchAll(/CREATE TABLE IF NOT EXISTS (\w+)/g)].map((m) => m[1]);

    expect(created.sort()).toEqual(["inbound_addresses", "invites", "members", "sessions", "spend_rollup", "tenants"]);
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

  it("refuses a reserved id, and one that starts or ends with a hyphen, so no later path or host label is taken", () => {
    const registry = registryObject();

    for (const id of RESERVED_IDS.reserved) expect(registry.register(id).refused).toBe(`${id} is reserved, so no tenant can take it`);
    for (const id of ["-", "-alpha", "alpha-"]) expect(registry.register(id).refused).toMatch(/starts and ends with a letter or a digit/);
    expect(registry.register("alpha-2").tenant.id).toBe("alpha-2");
    expect(registry.tenants().map((row) => row.id)).toEqual(["alpha-2"]);
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

  it("refuse to register an id or a status the registry does not take", async () => {
    const id = await operator("POST", "/hosted/tenants/Alpha/register");
    const status = await operator("POST", "/hosted/tenants/alpha/register?status=gone");

    expect([id.status, status.status]).toEqual([400, 400]);
    expect(await tenantRegistry(env).tenants()).toEqual([]);
  });
});
