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

// The operator's registry as the runtime builds one: its constructor creates its tables.
export const registryObject = () => new TenantRegistry({ storage: { sql: sqlStorage() } }, {});

// REGISTRY as the runtime binds it.
export const registryNamespace = () => namespace(registryObject);
