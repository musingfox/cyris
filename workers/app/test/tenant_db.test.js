import { describe, it, expect } from "vitest";
import {
  DB_HOST,
  LOCATION_HINT,
  TenantDbGateway,
  failureStatus,
  isTenantTable,
  quotedName,
  tenantRows,
} from "../src/tenant_db.js";
import { REGISTRY_NAME, tenantRegistry } from "../src/tenant_registry.js";
import { SCHEMA_SQL, namespace, registryNamespace, tenantDb } from "./sql_storage.js";

const query = (tenant, body) =>
  new Request(`http://${DB_HOST}/t/${tenant}/query`, { method: "POST", body: JSON.stringify(body) });

function gateway(props, TENANT_DB = namespace()) {
  return { gateway: new TenantDbGateway({ props }, { TENANT_DB }), TENANT_DB };
}

describe("TenantDb", () => {
  it("answers rows as D1 does, with the values bound rather than written into the SQL", () => {
    const { db } = tenantDb();
    db.query("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)");
    const value = "'; DROP TABLE settings; --";
    db.query("INSERT INTO settings (key, value) VALUES (?, ?)", ["general.timezone", value]);

    expect(db.query("SELECT key, value FROM settings WHERE key = ?", ["general.timezone"])).toEqual({
      rows: [{ key: "general.timezone", value }],
      changes: 0,
    });
  });

  it("counts the rows a statement changed, not the index entries it wrote", () => {
    const { db } = tenantDb();
    db.query("CREATE TABLE articles (url TEXT PRIMARY KEY, title TEXT, state TEXT)");
    db.query("CREATE INDEX idx_state ON articles(state)");

    const insert = "INSERT OR IGNORE INTO articles VALUES (?, ?, ?)";

    expect(db.query(insert, ["https://a.test/1", "One", "pending"]).changes).toBe(1);
    db.query(insert, ["https://a.test/2", "Two", "pending"]);
    expect(db.query(insert, ["https://a.test/1", "Again", "x"]).changes).toBe(0);
    expect(db.query("UPDATE articles SET state = ? WHERE state = ?", ["accepted", "pending"]).changes).toBe(2);
  });

  it("runs the whole schema as one query and lists only the tenant's tables", () => {
    const { db } = tenantDb();
    db.query(SCHEMA_SQL);
    db.query("INSERT INTO sources (name, url) VALUES (?, ?)", ["A", "https://a.test/feed"]);

    const tables = db.tables();
    const created = [...SCHEMA_SQL.matchAll(/CREATE TABLE IF NOT EXISTS (\w+)/g)].map((m) => m[1]).sort();
    expect(Object.keys(tables)).toEqual(created);
    expect(tables.sources).toBe(1);
    expect(Object.keys(tables).filter((name) => name.startsWith("_"))).toEqual([]);
  });

  it("counts a table whatever its name, and runs nothing a name holds", () => {
    const { db } = tenantDb();
    db.query(SCHEMA_SQL);
    db.query("INSERT INTO sources (name, url) VALUES (?, ?)", ["A", "https://a.test/feed"]);
    // Tables the tenant's container could create in its own database.
    const names = ['x"; DROP TABLE sources; --', 'a""b', "with space", "'single'"];
    for (const name of names) {
      db.query(`CREATE TABLE ${quotedName(name)} (v)`);
      db.query(`INSERT INTO ${quotedName(name)} VALUES (1)`);
    }

    const tables = db.tables();

    for (const name of names) expect(tables[name], name).toBe(1);
    expect(tables.sources).toBe(1);
    expect(db.query("SELECT count(*) AS n FROM sources").rows[0].n).toBe(1);
  });

  it("leaves out the object's own tables and SQLite's", () => {
    expect(["_cf_KV", "_cf_METADATA", "sqlite_sequence", "digests"].filter(isTenantTable)).toEqual(["digests"]);
  });
});

describe("TenantDbGateway", () => {
  it("answers its own tenant from that tenant's object, placed in the container's region", async () => {
    const { gateway: alpha, TENANT_DB } = gateway({ tenant: "alpha" });

    const response = await alpha.fetch(query("alpha", { sql: "SELECT 1 AS one", params: [] }));

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ rows: [{ one: 1 }], changes: 0 });
    expect(TENANT_DB.gets).toEqual([{ name: "alpha", options: { locationHint: LOCATION_HINT } }]);
  });

  it("refuses another tenant with 403, before any object is reached", async () => {
    const { gateway: alpha, TENANT_DB } = gateway({ tenant: "alpha" });

    const response = await alpha.fetch(query("bravo", { sql: "SELECT * FROM settings", params: [] }));

    expect(response.status).toBe(403);
    expect(await response.json()).toEqual({ errors: [{ message: "tenant bravo is not this run's" }] });
    expect(TENANT_DB.gets).toEqual([]);
  });

  it("keeps two tenants' rows apart", async () => {
    const TENANT_DB = namespace();
    const each = {
      alpha: gateway({ tenant: "alpha" }, TENANT_DB).gateway,
      bravo: gateway({ tenant: "bravo" }, TENANT_DB).gateway,
    };
    for (const [tenant, door] of Object.entries(each)) {
      await door.fetch(query(tenant, { sql: "CREATE TABLE digests (date TEXT, period TEXT)" }));
      await door.fetch(query(tenant, { sql: "INSERT INTO digests VALUES (?, ?)", params: [tenant, "morning"] }));
    }

    for (const [tenant, door] of Object.entries(each)) {
      const response = await door.fetch(query(tenant, { sql: "SELECT date FROM digests", params: [] }));
      expect((await response.json()).rows).toEqual([{ date: tenant }]);
    }
  });

  it("refuses every tenant when it was registered without one", async () => {
    for (const props of [undefined, {}, { tenant: "" }]) {
      const { gateway: door, TENANT_DB } = gateway(props);
      expect((await door.fetch(query("alpha", { sql: "SELECT 1" }))).status).toBe(403);
      expect(TENANT_DB.gets).toEqual([]);
    }
  });

  it("answers only POST /t/<tenant>/query, and refuses every other request with 403", async () => {
    const { gateway: alpha, TENANT_DB } = gateway({ tenant: "alpha" });
    for (const request of [
      new Request(`http://${DB_HOST}/t/alpha/query`),
      new Request(`http://${DB_HOST}/t/alpha`, { method: "POST", body: "{}" }),
      new Request(`http://${DB_HOST}/t/alpha/query/more`, { method: "POST", body: "{}" }),
      new Request(`http://${DB_HOST}/`, { method: "POST", body: "{}" }),
    ]) {
      expect((await alpha.fetch(request)).status).toBe(403);
    }
    expect(TENANT_DB.gets).toEqual([]);
  });

  it("answers 403 to anything aimed at the operator's registry, and never reaches it", async () => {
    // The gateway runs with the Worker's whole env, the registry's binding included, so this
    // holds a registry with a tenant in it beside the tenant databases and watches both.
    const REGISTRY = registryNamespace();
    await tenantRegistry({ REGISTRY }).register("alpha");
    REGISTRY.gets.length = 0;
    const TENANT_DB = namespace();
    const alpha = new TenantDbGateway({ props: { tenant: "alpha" } }, { TENANT_DB, REGISTRY });
    const tenantsTable = { sql: "SELECT id, status FROM tenants", params: [] };
    const post = (path, body = tenantsTable) =>
      new Request(`http://${DB_HOST}${path}`, { method: "POST", body: JSON.stringify(body) });

    for (const request of [
      post(`/t/${REGISTRY_NAME}/query`),
      post(`/t/${encodeURIComponent(REGISTRY_NAME)}/query`),
      post("/t/registry/query"),
      post("/registry/query"),
      post("/registry/tenants"),
      post("/t/alpha/register"),
      post("/t/alpha/tenants"),
      post("/t/alpha/query/../../registry/query"),
      new Request(`http://${DB_HOST}/registry/tenants`),
      new Request(`http://${DB_HOST}/t/${REGISTRY_NAME}/query`),
    ]) {
      const response = await alpha.fetch(request);
      expect(response.status, request.url).toBe(403);
    }
    expect(REGISTRY.gets).toEqual([]);
    expect(TENANT_DB.gets).toEqual([]);

    // Its own tenant's query reaches its own database only: the registry's tables are not there.
    const own = await alpha.fetch(post("/t/alpha/query"));
    expect(own.status).toBe(400);
    expect((await own.json()).errors[0].message).toMatch(/no such table: tenants/);
    expect(REGISTRY.gets).toEqual([]);
    expect(TENANT_DB.gets.map((get) => get.name)).toEqual(["alpha"]);
  });

  it("answers a body it cannot read, and a statement SQLite refuses, with 400 and the reason", async () => {
    const { gateway: alpha } = gateway({ tenant: "alpha" });

    const unreadable = new Request(`http://${DB_HOST}/t/alpha/query`, { method: "POST", body: "{" });
    const malformed = await alpha.fetch(unreadable);
    const shapeless = await alpha.fetch(query("alpha", { sql: 7 }));
    const refused = await alpha.fetch(query("alpha", { sql: "SELECT * FROM nowhere", params: [] }));

    expect([malformed.status, shapeless.status, refused.status]).toEqual([400, 400, 400]);
    expect((await refused.json()).errors[0].message).toMatch(/no such table: nowhere/);
  });

  it("answers an object the runtime says can be retried with 503, which the client retries", async () => {
    const reset = Object.assign(new Error("Durable Object reset because its code was updated."), {
      retryable: true,
    });
    const TENANT_DB = { getByName: () => ({ query: () => Promise.reject(reset) }) };
    const { gateway: alpha } = gateway({ tenant: "alpha" }, TENANT_DB);

    expect((await alpha.fetch(query("alpha", { sql: "SELECT 1" }))).status).toBe(503);
  });

  it("answers an overloaded object with 429, which the client does not retry", async () => {
    // Whatever `retryable` says: a retry adds to the load that refused the query.
    for (const retryable of [true, false, undefined]) {
      const message = "Durable Object is overloaded. Too many requests queued.";
      const overloaded = Object.assign(new Error(message), { overloaded: true, retryable });
      const TENANT_DB = { getByName: () => ({ query: () => Promise.reject(overloaded) }) };
      const { gateway: alpha } = gateway({ tenant: "alpha" }, TENANT_DB);

      const response = await alpha.fetch(query("alpha", { sql: "SELECT 1" }));

      expect(response.status).toBe(429);
      expect((await response.json()).errors[0].message).toMatch(/overloaded/);
    }
  });
});

describe("failureStatus", () => {
  it("is 429 for an overloaded object, 503 for one that can be retried, else 400", () => {
    expect(failureStatus({ overloaded: true, retryable: true })).toBe(429);
    expect(failureStatus({ retryable: true })).toBe(503);
    expect(failureStatus(new Error("no such table: nowhere"))).toBe(400);
    expect(failureStatus("a thrown string")).toBe(400);
  });
});

describe("the Worker's own reads of a tenant", () => {
  it("are rows, or null with a warning naming the tenant", async () => {
    const TENANT_DB = namespace();
    const read = tenantRows({ TENANT_DB }, "alpha");

    expect(await read("SELECT 2 AS two", [], "schedule")).toEqual([{ two: 2 }]);
    expect(await read("SELECT key FROM settings", [], "schedule")).toBeNull();
  });
});
