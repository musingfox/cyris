// The operator's registry: one SQLite Durable Object that holds what belongs to the hosted
// deployment rather than to any one tenant. Why one object and not KV or D1:
// docs/decisions/0030-the-operator-registry-is-one-sqlite-durable-object.md.
//
// Only the hosted Worker reaches it, through its REGISTRY binding (wrangler.hosted.toml). No
// tenant's run container does: it holds no binding, and the only Worker code it can reach is
// TenantDbGateway (tenant_db.js), which answers one tenant's queries and never names this object.

import { DurableObject } from "cloudflare:workers";
import {
  DEFAULT_DAILY_RUN_CAP,
  DISPATCH_EVERY_MS,
  RUNS_SCHEMA,
  checkSchedule,
  dispatchRuns,
  endRun,
  queueRun,
  recentRuns,
  tenantRun,
} from "./run_queue.js";
import { LOCATION_HINT, TENANT } from "./tenant_db.js";
import { ACTIVE, TENANT_STATUSES } from "./tenant_status.js";

// The registry's name in its namespace. There is one registry, so the name is fixed; it is
// outside TENANT, so no tenant id can ever name it.
export const REGISTRY_NAME = "cyris:registry";

// The registry's own tables: not schema.sql, which is a tenant's. `tenants` and the run queue's
// `runs` (run_queue.js) have readers and writers; the others are created empty for the cards that
// fill them (login, invites, newsletters, budgets), so those cards add code and no new home for
// state. A tenant's schedule is the registry's, never its own database's, which its run container
// can rewrite (ADR-0032): `digest_times`, its two digest hours as a JSON list, and `timezone`, the
// zone they are hours of; neither is set until the operator's schedule route sets both.
// `daily_run_cap` is the most runs it may start in 24 hours.
export const REGISTRY_SCHEMA = `
CREATE TABLE IF NOT EXISTS tenants (
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  digest_times TEXT,
  timezone TEXT,
  daily_run_cap INTEGER NOT NULL DEFAULT ${DEFAULT_DAILY_RUN_CAP},
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
${RUNS_SCHEMA}`;

// The columns a registry created before them lacks, added in place: each is additive and has a
// default, so a check of the table's own columns is the whole migration (ADR-0032).
const ADDED_COLUMNS = [
  ["tenants", "timezone", "TEXT"],
  ["tenants", "daily_run_cap", `INTEGER NOT NULL DEFAULT ${DEFAULT_DAILY_RUN_CAP}`],
];

const TENANT_COLUMNS = "id, status, digest_times, timezone, daily_run_cap, created_at, updated_at";
const TENANT_INSERT = "INSERT INTO tenants (id, status, created_at, updated_at) VALUES (?, ?, ?, ?)";
// A status named sets it; none named adds an absent tenant as active and leaves a present one alone.
const TENANT_UPSERT =
  `${TENANT_INSERT} ON CONFLICT(id) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at`;
const TENANT_ADD = `${TENANT_INSERT} ON CONFLICT(id) DO NOTHING`;
const TENANT_SCHEDULE = "UPDATE tenants SET digest_times = ?, timezone = ?, daily_run_cap = ?, updated_at = ? WHERE id = ?";

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
    for (const [table, column, type] of ADDED_COLUMNS) {
      const columns = ctx.storage.sql.exec(`PRAGMA table_info(${table})`).toArray().map((row) => row.name);
      if (!columns.includes(column)) ctx.storage.sql.exec(`ALTER TABLE ${table} ADD COLUMN ${column} ${type}`);
    }
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

  // Sets a registered tenant's schedule, `{ times, timeZone, dailyRunCap }`, checked by
  // `checkSchedule`, and answers `{tenant}`, its row, or `{refused}`, writing nothing.
  setSchedule(id, schedule, { now = new Date() } = {}) {
    if (!this.tenant(id)) return { refused: `${id} is not a registered tenant` };
    const checked = checkSchedule(schedule);
    if (checked.refused) return checked;
    const { digestTimes, timeZone, dailyRunCap } = checked.schedule;
    this.ctx.storage.sql.exec(TENANT_SCHEDULE, JSON.stringify(digestTimes), timeZone, dailyRunCap, stamp(now), id);
    return { tenant: this.tenant(id) };
  }

  // Queues one run of `id`'s issue `{ period, date, origin }` (run_queue.js `queueRun`) and has
  // the alarm start it as soon as a slot is free.
  async queueRun(id, issue, { now = new Date() } = {}) {
    const answer = queueRun(this.ctx.storage.sql, this.tenant(id), issue, now.getTime());
    if (answer.queued) await this.ctx.storage.setAlarm(now.getTime());
    return answer;
  }

  // `id`'s container stopped, `{ exitCode, reason }` as its monitor saw it: its held run ends or
  // goes back in the queue, and the alarm hands the freed slot on.
  async runEnded(id, outcome, { now = new Date() } = {}) {
    const run = endRun(this.ctx.storage.sql, id, outcome, now.getTime());
    await this.ctx.storage.setAlarm(now.getTime());
    return run;
  }

  // `id`'s most recent runs, newest first.
  runs(id) {
    return recentRuns(this.ctx.storage.sql, id);
  }

  // One pass of the run queue; while a run still waits, another pass is set for later.
  async alarm() {
    const start = (run) => tenantRun(this.env, run.tenant_id).startRun({ period: run.period, date: run.date });
    const { waiting } = await dispatchRuns(this.ctx.storage.sql, start);
    if (waiting > 0) await this.ctx.storage.setAlarm(Date.now() + DISPATCH_EVERY_MS);
  }
}
