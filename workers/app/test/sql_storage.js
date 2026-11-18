// Stand-ins for a hosted tenant's Durable Objects over real SQLite (node:sqlite), shared by the
// tenant database's tests and the hosted Worker's.
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { TenantDb } from "../src/tenant_db.js";

export const SCHEMA_SQL = readFileSync(
  new URL("../../../src/cyris/adapters/store/schema.sql", import.meta.url),
  "utf8",
);

// `ctx.storage.sql` as the runtime answers: exec() takes the bindings as arguments and returns a
// cursor. A script with no bindings runs whole, as the probe measured schema.sql run in one
// exec; node's prepare() would stop at the first statement. The object's own `_cf_KV` table is
// there as in a real one.
export function sqlStorage() {
  const db = new DatabaseSync(":memory:");
  db.exec("CREATE TABLE _cf_KV (key TEXT PRIMARY KEY, value BLOB)");
  return {
    db,
    exec(sql, ...params) {
      let rows = [];
      if (params.length === 0 && /;\s*\S/.test(sql.replace(/--.*$/gm, ""))) db.exec(sql);
      else rows = db.prepare(sql).all(...params).map((row) => ({ ...row }));
      return { toArray: () => rows, one: () => rows[0] };
    },
  };
}

export function tenantDb() {
  const sql = sqlStorage();
  return { db: new TenantDb({ storage: { sql } }, {}), sql };
}

// TENANT_DB as the runtime binds it: getByName returns the tenant's own object, made once.
export function namespace() {
  const objects = new Map();
  const gets = [];
  return {
    gets,
    objects,
    getByName(name, options) {
      gets.push({ name, options });
      if (!objects.has(name)) objects.set(name, tenantDb().db);
      return objects.get(name);
    },
  };
}
