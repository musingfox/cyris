import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";
import { env } from "cloudflare:workers";
import { generateKeyPair, exportJWK, SignJWT } from "jose";
import worker from "../src/hosted.js";
import { LOCATION_HINT, TENANT } from "../src/tenant_db.js";
import RESERVED_IDS from "../src/hosted_reserved_ids.json" with { type: "json" };
import {
  SESSION_TTL_MS,
  SIGN_IN_ADDRESS_CAP,
  SIGN_IN_DAILY_CEILING,
  SIGN_IN_DAY_MS,
  SIGN_IN_GAP_MS,
  SIGN_IN_HOUR_MS,
  SIGN_IN_TENANT_CAP,
  SIGN_IN_TTL_MS,
} from "../src/hosted_session.js";
import { REGISTRY_NAME, REGISTRY_SCHEMA, TenantRegistry, tenantRegistry } from "../src/tenant_registry.js";
import { TENANT_STATUSES } from "../src/tenant_status.js";
import { DEFAULT_DAILY_RUN_CAP, DEFAULT_MAX_DIGEST_TIMES } from "../src/run_queue.js";
import { SCHEMA_SQL, alarmStorage, drainAlarms, namespace, registryNamespace, registryObject, sqlStorage } from "./sql_storage.js";

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

  // A reader's ask at /login, and the operator's invitation, as the two callers make them.
  const ask = (registry, email, code, ms = 0) => registry.issueSignIn(email, code, { now: later(ms) });
  const invitation = (registry, email, code, ms = 0) => registry.issueSignIn(email, code, { now: later(ms), invitation: true });
  const step = SIGN_IN_GAP_MS + 1000;

  it("lets one address ask for at most SIGN_IN_ADDRESS_CAP links in any hour, and again within the hour", () => {
    const registry = signInRegistry();
    for (let i = 0; i < SIGN_IN_ADDRESS_CAP; i++) expect(ask(registry, "ana@example.com", `h-${i}`, i * step).tenant).toBe("alpha");

    const past = ask(registry, "ana@example.com", "h-past", SIGN_IN_ADDRESS_CAP * step);
    expect(past).toMatchObject({ capped: "address" });
    expect(past.tenant).toBeUndefined();
    expect(rows(registry, "invites").map((row) => row.code)).not.toContain("h-past");
    // Another address is not held by ana's cap, and an hour after her first ask she gets a link again.
    expect(ask(registry, "bea@example.com", "h-bea", SIGN_IN_ADDRESS_CAP * step).tenant).toBe("bravo");
    expect(ask(registry, "ana@example.com", "h-next", SIGN_IN_HOUR_MS + 1000).tenant).toBe("alpha");
  });

  it("lets one tenant's addresses ask for at most SIGN_IN_TENANT_CAP links in any hour, holding no other tenant", () => {
    const registry = signInRegistry();
    const cohort = Array.from({ length: SIGN_IN_TENANT_CAP }, (_, i) => `reader${i}@example.com`);
    for (const email of cohort) registry.invite("alpha", email, { now: AT });
    for (const email of cohort) expect(ask(registry, email, `h-${email}`).tenant).toBe("alpha");

    expect(ask(registry, "ana@example.com", "h-ana", SIGN_IN_HOUR_MS - 1000)).toMatchObject({ capped: "tenant" });
    expect(ask(registry, "bea@example.com", "h-bea", SIGN_IN_HOUR_MS - 1000).tenant).toBe("bravo");
    expect(ask(registry, "ana@example.com", "h-ana", SIGN_IN_HOUR_MS + 1000).tenant).toBe("alpha");
  });

  it("holds every ask past SIGN_IN_DAILY_CEILING in a day, which one tenant's flood cannot reach", () => {
    const registry = signInRegistry();
    // A day of alpha's readers asking as fast as anyone may: four addresses, each every gap.
    const flood = ["ana", "al", "amy", "abe"].map((name) => `${name}@example.com`);
    for (const email of flood.slice(1)) registry.invite("alpha", email, { now: AT });
    let issued = 0;
    for (let ms = 0; ms < SIGN_IN_DAY_MS; ms += step) {
      for (const email of flood) if (ask(registry, email, `h-${email}-${ms}`, ms).tenant) issued++;
    }
    expect(issued).toBeLessThanOrEqual((SIGN_IN_DAY_MS / SIGN_IN_HOUR_MS + 1) * SIGN_IN_TENANT_CAP);
    expect(issued).toBeLessThan(SIGN_IN_DAILY_CEILING);
    expect(ask(registry, "bea@example.com", "h-bea", SIGN_IN_DAY_MS).tenant).toBe("bravo");

    // Asks enough to reach the ceiling, from many tenants, hold every reader, bravo's too.
    const fresh = signInRegistry();
    for (let i = 0; i < SIGN_IN_DAILY_CEILING; i++) {
      fresh.ctx.storage.sql.exec("INSERT INTO sign_in_asks (email, tenant_id, asked_at) VALUES (?, ?, ?)", `r${i}@example.com`, `t${i}`, STAMP);
    }
    expect(ask(fresh, "bea@example.com", "h-bea", 1000)).toMatchObject({ capped: "daily" });
    expect(ask(fresh, "bea@example.com", "h-bea", SIGN_IN_DAY_MS + 1000).tenant).toBe("bravo");
  });

  it("issues the operator's invitation whatever the gap and the caps, counting it against none", () => {
    const registry = signInRegistry();
    for (let i = 0; i <= SIGN_IN_ADDRESS_CAP; i++) ask(registry, "ana@example.com", `h-${i}`, i * step);
    expect(ask(registry, "ana@example.com", "h-held", SIGN_IN_ADDRESS_CAP * step + 1000)).toMatchObject({ capped: "address" });

    // Held by its cap and inside the gap, ana is still invited again at once.
    expect(invitation(registry, "ana@example.com", "h-invite", SIGN_IN_ADDRESS_CAP * step + 2000).tenant).toBe("alpha");
    // A cohort invited at once takes nothing from the tenant's readers' hour.
    const cohort = Array.from({ length: SIGN_IN_TENANT_CAP * 3 }, (_, i) => `reader${i}@example.com`);
    for (const email of cohort) registry.invite("bravo", email, { now: AT });
    for (const email of cohort) expect(invitation(registry, email, `h-${email}`).tenant).toBe("bravo");
    expect(rows(registry, "sign_in_asks").filter((row) => row.tenant_id === "bravo")).toEqual([]);
    expect(ask(registry, "bea@example.com", "h-bea").tenant).toBe("bravo");
    // An invitation still goes only to an address on the list.
    expect(invitation(registry, "stranger@example.com", "h-x")).toEqual({ refused: "not invited" });
  });

  it("writes nothing for an address off the list, however often it asks, and counts none against the caps", () => {
    const registry = signInRegistry();
    const tables = ["members", "invites", "sessions", "tenants", "sign_in_asks"];
    const before = JSON.stringify(tables.map((table) => rows(registry, table)));

    for (let i = 0; i < SIGN_IN_TENANT_CAP * 30; i++) {
      expect(ask(registry, `stranger${i}@example.com`, `h-${i}`)).toEqual({ refused: "not invited" });
    }

    expect(JSON.stringify(tables.map((table) => rows(registry, table)))).toBe(before);
    expect(ask(registry, "ana@example.com", "h-ana").tenant).toBe("alpha");
  });

  it("deletes a link's row once it expires, and an ask's a day after it, and revokes both with the address", () => {
    const registry = signInRegistry();
    ask(registry, "ana@example.com", "h-ana");
    ask(registry, "bea@example.com", "h-bea");

    ask(registry, "stranger@example.com", "h-x", SIGN_IN_TTL_MS + 1000);
    expect(rows(registry, "invites")).toEqual([]);
    expect(rows(registry, "sign_in_asks").map((row) => row.email)).toEqual(["ana@example.com", "bea@example.com"]);
    registry.revoke("bravo", "bea@example.com");
    expect(rows(registry, "sign_in_asks").map((row) => row.email)).toEqual(["ana@example.com"]);
    ask(registry, "stranger@example.com", "h-x", SIGN_IN_DAY_MS);
    expect(rows(registry, "sign_in_asks")).toEqual([]);
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

// The registry's schema as main (62eefb3d) created it, before the hosted beta's cards: a deployed
// registry may hold exactly these tables and rows when the combined Worker first starts it.
const MAIN_REGISTRY_SCHEMA = `
CREATE TABLE IF NOT EXISTS tenants (id TEXT PRIMARY KEY, status TEXT NOT NULL, digest_times TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS invites (code TEXT PRIMARY KEY, email TEXT NOT NULL, tenant_id TEXT, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, used_at TEXT);
CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, email TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_sessions_tenant ON sessions(tenant_id);
CREATE TABLE IF NOT EXISTS inbound_addresses (address TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_inbound_addresses_tenant ON inbound_addresses(tenant_id);
CREATE TABLE IF NOT EXISTS spend_rollup (tenant_id TEXT NOT NULL, month TEXT NOT NULL, cost_usd REAL NOT NULL DEFAULT 0, runs INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, PRIMARY KEY (tenant_id, month));
`;

describe("the registry's schema on a registry an earlier version created", () => {
  // The object's own `_cf_*` tables and SQLite's are not the registry's.
  const tables = (sql) =>
    sql
      .exec("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
      .toArray()
      .map((row) => row.name)
      .filter((name) => !name.startsWith("_cf_") && !name.startsWith("sqlite_"));
  const columns = (sql, table) => sql.exec(`PRAGMA table_info(${table})`).toArray().map((row) => row.name);
  const indexes = (sql) => sql.exec("SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL ORDER BY name").toArray().map((row) => row.name);

  it("gains every card's tables and columns, keeps its rows, and adds nothing on a second start", () => {
    const sql = sqlStorage();
    sql.exec(MAIN_REGISTRY_SCHEMA);
    sql.exec("INSERT INTO tenants VALUES ('alpha', 'active', NULL, ?, ?)", STAMP, STAMP);
    sql.exec("INSERT INTO tenants VALUES ('bravo', 'paused', NULL, ?, ?)", STAMP, STAMP);

    const registry = new TenantRegistry({ storage: alarmStorage(sql) }, {});
    const made = { tables: tables(sql), tenants: columns(sql, "tenants"), indexes: indexes(sql) };

    const created = [...REGISTRY_SCHEMA.matchAll(/CREATE TABLE IF NOT EXISTS (\w+)/g)].map((m) => m[1]).sort();
    expect(made.tables).toEqual(created);
    expect(made.tenants).toEqual([
      "id",
      "status",
      "digest_times",
      "created_at",
      "updated_at",
      "timezone",
      "daily_run_cap",
      "max_digest_times",
      "monthly_ai_budget_usd",
    ]);
    expect(registry.tenants().map(({ id, status, daily_run_cap, max_digest_times }) => [id, status, daily_run_cap, max_digest_times])).toEqual([
      ["alpha", "active", DEFAULT_DAILY_RUN_CAP, DEFAULT_MAX_DIGEST_TIMES],
      ["bravo", "paused", DEFAULT_DAILY_RUN_CAP, DEFAULT_MAX_DIGEST_TIMES],
    ]);

    // Each card's writes work on it: the schedule, an invite and its link, a newsletter address.
    expect(registry.setSchedule("alpha", { times: ["07:00", "19:00"], timeZone: "Asia/Taipei", maxDigestTimes: 2 }).refused).toBeUndefined();
    expect(registry.invite("alpha", "ana@example.com", { now: AT }).member).toMatchObject({ tenant_id: "alpha" });
    expect(registry.issueSignIn("ana@example.com", "h-ana", { now: AT })).toMatchObject({ tenant: "alpha" });
    expect(registry.rotateInboundAddress("alpha", { now: AT, address: "a".repeat(26) })).toMatchObject({ revoked: 0 });
    const held = () => ["tenants", "members", "invites", "sign_in_asks", "inbound_addresses"].map(
      (table) => sql.exec(`SELECT count(*) AS n FROM ${table}`).one().n,
    );
    const before = held();

    expect(() => new TenantRegistry({ storage: alarmStorage(sql) }, {})).not.toThrow();
    expect({ tables: tables(sql), tenants: columns(sql, "tenants"), indexes: indexes(sql) }).toEqual(made);
    expect(held()).toEqual(before);
    expect(new TenantRegistry({ storage: alarmStorage(sql) }, {}).tenant("alpha")).toMatchObject({
      digest_times: '["07:00","19:00"]',
      timezone: "Asia/Taipei",
    });
  });
});

describe("the operator's registry", () => {
  it("creates its own tables, none of them a tenant's", () => {
    const created = [...REGISTRY_SCHEMA.matchAll(/CREATE TABLE IF NOT EXISTS (\w+)/g)].map((m) => m[1]);
    const tenants = [...SCHEMA_SQL.matchAll(/CREATE TABLE IF NOT EXISTS (\w+)/g)].map((m) => m[1]);

    expect(created.sort()).toEqual(["inbound_addresses", "invites", "members", "runs", "sessions", "sign_in_asks", "spend_reports", "spend_rollup", "tenants", "vote_key", "waitlist", "waitlist_attempts"]);
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
        max_digest_times: DEFAULT_MAX_DIGEST_TIMES,
        // Unset: the default is DEFAULT_MONTHLY_AI_BUDGET_USD's alone (ai_budget.js).
        monthly_ai_budget_usd: null,
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
      await registry.setSchedule(tenant, { times: ["08:00", "20:00"], timeZone: "UTC", maxDigestTimes: 2 });
    }
    // charlie has a database with a due schedule, but the registry never heard of it.
    for (const tenant of ["alpha", "bravo", "charlie", "delta"]) scheduled(bound.TENANT_DB, tenant);
    bound.TENANT_DB.gets.length = 0;
    vi.setSystemTime(new Date("2026-11-18T08:00:00Z"));
    vi.spyOn(console, "log").mockImplementation(() => {});

    await worker.scheduled();
    await drainAlarms(registryOf(bound));

    expect(bound.starts).toEqual([
      ["alpha", { period: "morning", date: "2026-11-18", run: { id: 1, attempt: 1 } }],
      ["delta", { period: "morning", date: "2026-11-18", run: { id: 2, attempt: 1 } }],
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
    await tenantRegistry(env).setSchedule("alpha", { times: ["08:00", "20:00"], timeZone: "UTC", maxDigestTimes: 2 });
    scheduled(bound.TENANT_DB, "alpha");
    vi.setSystemTime(new Date("2026-11-18T13:00:00Z"));

    await worker.scheduled();
    await drainAlarms(registryOf(bound));

    expect(bound.starts).toEqual([]);
  });
});

describe("the hosted Worker's operator routes, over the registry", () => {
  const TEAM = "registry-team.example";
  // The service token the operator signs in with: its Client ID, which Access signs in as `common_name`.
  const OPERATOR = "operator-token.access";
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
    Object.assign(env, bound, { CYRIS_ACCESS_TEAM_DOMAIN: `${crypto.randomUUID()}.${TEAM}`, CYRIS_ACCESS_AUD: AUD, CYRIS_HOSTED_OPERATORS: OPERATOR });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    for (const key of Object.keys(env)) delete env[key];
  });

  const signIn = () =>
    new SignJWT({ common_name: OPERATOR })
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

  it("register, seed, schedule and start a tenant, and start none that is paused", async () => {
    vi.spyOn(console, "log").mockImplementation(() => {});
    const registered = await operator("POST", "/hosted/tenants/alpha/register");
    expect(registered.status).toBe(200);
    expect(await registered.json()).toMatchObject({ id: "alpha", status: "active" });

    const unscheduled = await operator("POST", "/hosted/tenants/alpha/run?period=morning");
    expect(unscheduled.status).toBe(409);
    expect((await unscheduled.json()).error).toContain("has no schedule in the registry");

    // The seed gives a tenant with no schedule its own one hour, in UTC (hosted_seed.js `spreadHour`).
    const seeded = await operator("POST", "/hosted/tenants/alpha/seed");
    expect(seeded.status).toBe(200);
    expect(await seeded.json()).toMatchObject({ tenant: "alpha", schedule: { times: ["08:00"], timeZone: "UTC" } });
    expect(await tenantRegistry(env).tenant("alpha")).toMatchObject({ digest_times: '["08:00"]', timezone: "UTC", max_digest_times: 1 });

    const schedule = await operator("POST", "/hosted/tenants/alpha/schedule?max_digest_times=2&times=07:00,19:00&timezone=asia/taipei");
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
    // Seeding again keeps the schedule the operator set, in the registry and in its copy.
    expect(await (await operator("POST", "/hosted/tenants/alpha/seed")).json()).toMatchObject({
      schedule: { times: ["07:00", "19:00"], timeZone: "Asia/Taipei" },
    });
    expect(await tenantRegistry(env).tenant("alpha")).toMatchObject({ digest_times: '["07:00","19:00"]', timezone: "Asia/Taipei" });

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
      // Two times for a tenant allowed one, three for any, and a maximum past two.
      "?times=08:00,20:00&timezone=UTC",
      "?max_digest_times=2&times=08:00,12:00,20:00&timezone=UTC",
      "?max_digest_times=3&times=08:00&timezone=UTC",
      "?max_digest_times=0&times=08:00&timezone=UTC",
      "?times=08:30&timezone=UTC",
      "?times=08:30,20:00&timezone=UTC",
      "?times=08:00,20:00",
      "?max_digest_times=2&times=08:00,20:00&timezone=Mars/Olympus",
      // An offset the runtime takes and the container's zoneinfo does not.
      "?max_digest_times=2&times=08:00,20:00&timezone=%2B08:00",
      "?max_digest_times=2&times=08:00,20:00&timezone=-05:00",
      "?max_digest_times=2&times=08:00,20:00&timezone=UTC&daily_run_cap=0",
      "?max_digest_times=2&times=08:00,20:00&timezone=UTC&daily_run_cap=2.5",
    ]) {
      const response = await operator("POST", `/hosted/tenants/alpha/schedule${query}`);
      expect(response.status, query).toBe(400);
    }
    expect(await tenantRegistry(env).tenant("alpha")).toMatchObject({ digest_times: null, timezone: null });
    expect((await operator("POST", "/hosted/tenants/charlie/schedule?max_digest_times=2&times=08:00,20:00&timezone=UTC")).status).toBe(404);
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
    await operator("POST", "/hosted/tenants/alpha/schedule?max_digest_times=2&times=08:00,20:00&timezone=UTC");

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
    const forged = await new SignJWT({ common_name: OPERATOR })
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
