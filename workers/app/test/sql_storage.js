// Stand-ins for the hosted Worker's SQLite Durable Objects, a tenant's database and the
// operator's registry, over real SQLite (node:sqlite), shared by their tests and the Worker's.
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { TenantDb } from "../src/tenant_db.js";
import { TenantRegistry } from "../src/tenant_registry.js";

export const SCHEMA_SQL = readFileSync(
  new URL("../../../src/cyris/adapters/store/schema.sql", import.meta.url),
  "utf8",
);

export const HOSTED_SCHEMA_SQL = readFileSync(
  new URL("../../../src/cyris/adapters/store/hosted_schema.sql", import.meta.url),
  "utf8",
);

// One SQL statement's limit on a Durable Object (ADR-0028); a bound value may be far larger.
export const MAX_STATEMENT_BYTES = 100_000;

// `ctx.storage.sql` as the runtime answers: exec() takes the bindings as arguments and returns a
// cursor. A script with no bindings runs whole, as the probe measured schema.sql run in one
// exec; node's prepare() would stop at the first statement. The object's own `_cf_KV` table is
// there as in a real one, and a statement over the runtime's limit is refused as it would be.
// `sql` with its quoted names, string literals and line comments emptied in one left-to-right
// pass, so a ";" inside any of them is not taken for the end of a statement, as SQLite's own
// parser does not take it; a quote inside a comment, or "--" inside a name, is passed over too.
const unquoted = (sql) => sql.replace(/"(?:[^"]|"")*"|'(?:[^']|'')*'|--[^\n]*/g, "");

export function sqlStorage() {
  const db = new DatabaseSync(":memory:");
  db.exec("CREATE TABLE _cf_KV (key TEXT PRIMARY KEY, value BLOB)");
  return {
    db,
    exec(sql, ...params) {
      if (new TextEncoder().encode(sql).length > MAX_STATEMENT_BYTES) throw new Error("SQLITE_TOOBIG: statement too long");
      let rows = [];
      if (params.length === 0 && /;\s*\S/.test(unquoted(sql))) db.exec(sql);
      else rows = db.prepare(sql).all(...params).map((row) => ({ ...row }));
      return { toArray: () => rows, one: () => rows[0] };
    },
  };
}

// `ctx.storage` as the runtime gives a SQLite-backed object: its SQL, and transactionSync, which
// commits what the callback wrote or, when it throws, none of it.
export function objectStorage(sql = sqlStorage()) {
  return {
    sql,
    transactionSync(run) {
      sql.db.exec("BEGIN");
      try {
        const result = run();
        sql.db.exec("COMMIT");
        return result;
      } catch (error) {
        sql.db.exec("ROLLBACK");
        throw error;
      }
    },
  };
}

export function tenantDb() {
  const storage = objectStorage();
  return { db: new TenantDb({ storage }, {}), sql: storage.sql };
}

// TENANT_DB as the runtime binds it: getByName returns the tenant's own object, made once.
// `make` builds each object; REGISTRY's is `registryNamespace`.
export function namespace(make = () => tenantDb().db) {
  const objects = new Map();
  const gets = [];
  return {
    gets,
    objects,
    getByName(name, options) {
      gets.push({ name, options });
      if (!objects.has(name)) objects.set(name, make());
      return objects.get(name);
    },
  };
}

// `ctx.storage` with the one alarm a Durable Object may have set: `alarm` is when it is set for,
// or null. The runtime calls the object's alarm() then; a test calls `drainAlarms`.
export function alarmStorage(sql = sqlStorage()) {
  return {
    ...objectStorage(sql),
    alarm: null,
    async setAlarm(at) {
      this.alarm = Number(at);
    },
    async getAlarm() {
      return this.alarm;
    },
    async deleteAlarm() {
      this.alarm = null;
    },
  };
}

// The operator's registry as the runtime builds one: its constructor creates its tables. `env`
// is what it starts runs through (TENANT_RUN).
export const registryObject = (env = {}) => new TenantRegistry({ storage: alarmStorage() }, env);

// REGISTRY as the runtime binds it.
export const registryNamespace = (env = {}) => namespace(() => registryObject(env));

// Runs the registry's alarm while it is set for now or earlier, as the runtime would, clearing
// it first as the runtime does; at most `limit` passes, so a test that loops fails instead.
export async function drainAlarms(registry, limit = 50) {
  const storage = registry.ctx.storage;
  for (let pass = 0; storage.alarm !== null && storage.alarm <= Date.now(); pass++) {
    if (pass >= limit) throw new Error(`the registry's alarm was still due after ${limit} passes`);
    storage.alarm = null;
    await registry.alarm();
  }
}
