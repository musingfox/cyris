// The operator's registry: one SQLite Durable Object that holds what belongs to the hosted
// deployment rather than to any one tenant. Why one object and not KV or D1:
// docs/decisions/0030-the-operator-registry-is-one-sqlite-durable-object.md.
//
// Only the hosted Worker reaches it, through its REGISTRY binding (wrangler.hosted.toml). No
// tenant's run container does: it holds no binding, and the only Worker code it can reach is
// TenantDbGateway (tenant_db.js), which answers one tenant's queries and never names this object.

import { DurableObject } from "cloudflare:workers";
import { randomAddress } from "./inbound_address.js";
import { LOCATION_HINT, TENANT } from "./tenant_db.js";
import { ACTIVE, TENANT_STATUSES } from "./tenant_status.js";

// The registry's name in its namespace. There is one registry, so the name is fixed; it is
// outside TENANT, so no tenant id can ever name it.
export const REGISTRY_NAME = "cyris:registry";

// The registry's own tables: not schema.sql, which is a tenant's. `tenants` and
// `inbound_addresses` have a reader and a writer; the others are created empty for the cards
// that fill them (login, invites, budgets), so those cards add code and no new home for state.
// `inbound_addresses.address` is a newsletter address's random local part (inbound_address.js);
// a revoked address is deleted, so a row is an address that takes mail.
// `digest_times` is the tenant's digest hours as a JSON list, kept for the scheduling card;
// until it lands the tick still reads each tenant's schedule from the tenant's own database.
export const REGISTRY_SCHEMA = `
CREATE TABLE IF NOT EXISTS tenants (
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  digest_times TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invites (
  code TEXT PRIMARY KEY,
  email TEXT NOT NULL,
  tenant_id TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  email TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_tenant ON sessions(tenant_id);
CREATE TABLE IF NOT EXISTS inbound_addresses (
  address TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inbound_addresses_tenant ON inbound_addresses(tenant_id);
CREATE TABLE IF NOT EXISTS spend_rollup (
  tenant_id TEXT NOT NULL,
  month TEXT NOT NULL,
  cost_usd REAL NOT NULL DEFAULT 0,
  runs INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, month)
);
`;

const TENANT_COLUMNS = "id, status, digest_times, created_at, updated_at";
const TENANT_INSERT = "INSERT INTO tenants (id, status, created_at, updated_at) VALUES (?, ?, ?, ?)";
// A status named sets it; none named adds an absent tenant as active and leaves a present one alone.
const TENANT_UPSERT =
  `${TENANT_INSERT} ON CONFLICT(id) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at`;
const TENANT_ADD = `${TENANT_INSERT} ON CONFLICT(id) DO NOTHING`;

// An address's tenant, only while the registry holds that tenant.
const INBOUND_TENANT =
  "SELECT a.tenant_id FROM inbound_addresses a JOIN tenants t ON t.id = a.tenant_id WHERE a.address = ?";

// The time a row is written at, as the container's stores write it (seconds, UTC offset).
const stamp = (now) => `${now.toISOString().slice(0, 19)}+00:00`;

// The registry, from any Worker-side code that holds the binding. Created with the tenant
// objects' hint: only an object's first get() honours one, and that may be the operator's.
export const tenantRegistry = (env) => env.REGISTRY.getByName(REGISTRY_NAME, { locationHint: LOCATION_HINT });

export class TenantRegistry extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    // Every statement is CREATE ... IF NOT EXISTS, so this is a no-op on every start but the first.
    ctx.storage.sql.exec(REGISTRY_SCHEMA);
  }

  // The registered tenants in id order, every one or only those with `status`.
  tenants({ status } = {}) {
    const sql = this.ctx.storage.sql;
    const rows = status
      ? sql.exec(`SELECT ${TENANT_COLUMNS} FROM tenants WHERE status = ? ORDER BY id`, status)
      : sql.exec(`SELECT ${TENANT_COLUMNS} FROM tenants ORDER BY id`);
    return rows.toArray().map((row) => ({ ...row }));
  }

  // One tenant's row, or null when the id is not registered.
  tenant(id) {
    const [row] = this.ctx.storage.sql.exec(`SELECT ${TENANT_COLUMNS} FROM tenants WHERE id = ?`, id).toArray();
    return row ? { ...row } : null;
  }

  // Registers `id` and answers `{tenant}`, its row. With `status`, the tenant gets it whether or
  // not it was registered. Without one, an absent tenant is registered as active and a present
  // one keeps its status, so registering again never resumes a paused tenant. An id outside
  // TENANT, which names the tenant's Durable Objects and is one path segment, or a status
  // outside TENANT_STATUSES writes nothing and answers `{refused}`, the reason: an answer and
  // not a throw, so the caller can tell the operator's mistake from a registry that failed.
  register(id, { status, now = new Date() } = {}) {
    if (typeof id !== "string" || !TENANT.test(id)) return { refused: `a tenant id matches ${TENANT}: ${id}` };
    if (status !== undefined && !TENANT_STATUSES.includes(status)) {
      return { refused: `a tenant's status is one of ${TENANT_STATUSES.join(", ")}, not ${status}` };
    }
    const at = stamp(now);
    if (status === undefined) this.ctx.storage.sql.exec(TENANT_ADD, id, ACTIVE, at, at);
    else this.ctx.storage.sql.exec(TENANT_UPSERT, id, status, at, at);
    return { tenant: this.tenant(id) };
  }

  // The tenant `address` delivers to, or null when no registered tenant holds it: the hosted
  // email() handler's one lookup (hosted_inbound.js).
  inboundTenant(address) {
    const [row] = this.ctx.storage.sql.exec(INBOUND_TENANT, address).toArray();
    return row ? row.tenant_id : null;
  }

  // The tenant's current address and when it was issued, or null when it has none.
  inboundAddress(id) {
    const [row] = this.ctx.storage.sql
      .exec("SELECT address, created_at FROM inbound_addresses WHERE tenant_id = ?", id)
      .toArray();
    return row ? { ...row } : null;
  }

  // Gives a registered tenant a fresh address and revokes every one it held, so an address that
  // leaked stops taking mail at once: the statements run in one synchronous turn of the object,
  // which commits them together. Answers `{address, created_at, revoked}`, or `{refused}` for a
  // tenant the registry does not hold. `address` is for a test; the Worker never passes one.
  rotateInboundAddress(id, { now = new Date(), address = randomAddress() } = {}) {
    if (!this.tenant(id)) return { refused: `${id} is not a registered tenant` };
    const sql = this.ctx.storage.sql;
    const revoked = this.#revoke(id);
    const at = stamp(now);
    sql.exec("INSERT INTO inbound_addresses (address, tenant_id, created_at) VALUES (?, ?, ?)", address, id, at);
    return { address, created_at: at, revoked };
  }

  // Revokes every address the tenant holds, at once; answers `{revoked}`, how many.
  revokeInboundAddress(id) {
    return { revoked: this.#revoke(id) };
  }

  #revoke(id) {
    const sql = this.ctx.storage.sql;
    const held = sql.exec("SELECT count(*) AS n FROM inbound_addresses WHERE tenant_id = ?", id).one().n;
    sql.exec("DELETE FROM inbound_addresses WHERE tenant_id = ?", id);
    return held;
  }
}
