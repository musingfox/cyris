// The operator's registry: one SQLite Durable Object that holds what belongs to the hosted
// deployment rather than to any one tenant. Why one object and not KV or D1:
// docs/decisions/0030-the-operator-registry-is-one-sqlite-durable-object.md.
//
// Only the hosted Worker reaches it, through its REGISTRY binding (wrangler.hosted.toml). No
// tenant's run container does: it holds no binding, and the only Worker code it can reach is
// TenantDbGateway (tenant_db.js) and OperatorAlertGateway (hosted_alerts.js), which each answer
// one tenant and never name this object, and SpendGateway (hosted_spend.js), which answers one
// tenant's allowance and adds to its spend here and does nothing else.

import { DurableObject } from "cloudflare:workers";
import {
  MONTHLY_AI_BUDGET_COLUMN,
  SPEND_COLUMNS,
  SPEND_ROLLUP_SCHEMA,
  addSpend,
  checkBudget,
  checkSpend,
  monthSpend,
  spendMonth,
  spendReport,
} from "./ai_budget.js";
import { alertOperator, workerAlert } from "./hosted_alerts.js";
import RESERVED_IDS from "./hosted_reserved_ids.json";
import {
  SESSION_TTL_MS,
  SIGN_IN_ADDRESS_CAP,
  SIGN_IN_DAILY_CEILING,
  SIGN_IN_DAY_MS,
  SIGN_IN_GAP_MS,
  SIGN_IN_HOUR_MS,
  SIGN_IN_TENANT_CAP,
  SIGN_IN_TTL_MS,
  isEmail,
  normalEmail,
} from "./hosted_session.js";
import { randomAddress } from "./inbound_address.js";
import {
  DEFAULT_DAILY_RUN_CAP,
  LOST,
  RUNS_SCHEMA,
  checkSchedule,
  dispatchRuns,
  endRun,
  expireLeases,
  heldRun,
  nextPassAt,
  queueRun,
  recentRuns,
  tenantRun,
} from "./run_queue.js";
import { LOCATION_HINT, TENANT, tenantRows } from "./tenant_db.js";
import { ACTIVE, TENANT_STATUSES } from "./tenant_status.js";

// The registry's name in its namespace. There is one registry, so the name is fixed; it is
// outside TENANT, so no tenant id can ever name it.
export const REGISTRY_NAME = "cyris:registry";

// The registry's own tables: not schema.sql, which is a tenant's. `tenants` is the tenant list,
// and a tenant's schedule is the registry's, never its own database's, which its run container
// can rewrite (ADR-0032): `digest_times`, its two digest hours as a JSON list, and `timezone`,
// the zone they are hours of; neither is set until the operator's schedule route sets both.
// `daily_run_cap` is the most runs it may start in 24 hours. The run queue's `runs`
// (run_queue.js) holds every hosted run. `inbound_addresses.address` is a newsletter address's
// random local part (inbound_address.js); a revoked address is deleted, so a row is an address
// that takes mail. The sign-in (ADR-0033) has four: `members`, the operator's invite list, one
// tenant per address; `invites`, each single-use sign-in link, the operator's invitation and
// every later one alike, keyed by its token's SHA-256 in `code` and never by the token;
// `sessions`, keyed the same way; and `sign_in_asks`, each link a reader asked for at /login,
// kept a day so the caps on asking can count it. `spend_rollup` is each tenant's AI spend per UTC
// month, which its runs report through SpendGateway (hosted_spend.js), and
// `tenants.monthly_ai_budget_usd` the allowance a run is checked against as it starts
// (ai_budget.js, ADR-0038).
export const REGISTRY_SCHEMA = `
CREATE TABLE IF NOT EXISTS tenants (
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  digest_times TEXT,
  timezone TEXT,
  daily_run_cap INTEGER NOT NULL DEFAULT ${DEFAULT_DAILY_RUN_CAP},
  monthly_ai_budget_usd ${MONTHLY_AI_BUDGET_COLUMN},
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
CREATE INDEX IF NOT EXISTS idx_sessions_email ON sessions(email);
CREATE INDEX IF NOT EXISTS idx_invites_email ON invites(email);
CREATE TABLE IF NOT EXISTS members (
  email TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_members_tenant ON members(tenant_id);
CREATE TABLE IF NOT EXISTS sign_in_asks (
  email TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  asked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sign_in_asks_asked ON sign_in_asks(asked_at);
CREATE TABLE IF NOT EXISTS inbound_addresses (
  address TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inbound_addresses_tenant ON inbound_addresses(tenant_id);
${SPEND_ROLLUP_SCHEMA}
${RUNS_SCHEMA}`;

// The columns a registry created before them lacks, added in place: each is additive and has a
// default, so a check of the table's own columns is the whole migration (ADR-0032).
const ADDED_COLUMNS = [
  ["tenants", "timezone", "TEXT"],
  ["tenants", "daily_run_cap", `INTEGER NOT NULL DEFAULT ${DEFAULT_DAILY_RUN_CAP}`],
  ...SPEND_COLUMNS,
];

const TENANT_COLUMNS =
  "id, status, digest_times, timezone, daily_run_cap, monthly_ai_budget_usd, created_at, updated_at";
const TENANT_INSERT = "INSERT INTO tenants (id, status, created_at, updated_at) VALUES (?, ?, ?, ?)";
// A status named sets it; none named adds an absent tenant as active and leaves a present one alone.
const TENANT_UPSERT =
  `${TENANT_INSERT} ON CONFLICT(id) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at`;
const TENANT_ADD = `${TENANT_INSERT} ON CONFLICT(id) DO NOTHING`;
// A cap of null keeps the tenant's own: re-scheduling its hours never resets its spend limit.
const TENANT_SCHEDULE =
  "UPDATE tenants SET digest_times = ?, timezone = ?, daily_run_cap = COALESCE(?, daily_run_cap), updated_at = ? WHERE id = ?";
// Whether a run's issue was made since it started, for a run whose lease ran out (expireLeases).
const MADE_SINCE =
  "SELECT 1 AS made FROM digest_runs WHERE period = ? AND status = 'ok' AND dry_run = 0 AND finished_at >= ? LIMIT 1";

// An address's tenant, only while the registry holds that tenant.
const INBOUND_TENANT =
  "SELECT a.tenant_id FROM inbound_addresses a JOIN tenants t ON t.id = a.tenant_id WHERE a.address = ?";

// The time a row is written at, as the container's stores write it (seconds, UTC offset). Every
// stamp has this one shape, so comparing two as text compares the times.
const stamp = (now) => `${now.toISOString().slice(0, 19)}+00:00`;
const after = (now, ms) => stamp(new Date(now.getTime() + ms));

// Why `id` may not be registered, or null. TENANT is the shape of a name the tenant's objects
// can take; a new tenant must also start and end with a letter or a digit, as a host label
// does, and take no reserved word (hosted_reserved_ids.json).
function refuseTenantId(id) {
  if (typeof id !== "string" || !TENANT.test(id)) return `a tenant id matches ${TENANT}: ${id}`;
  if (id.startsWith("-") || id.endsWith("-")) return `a tenant id starts and ends with a letter or a digit: ${id}`;
  if (RESERVED_IDS.reserved.includes(id)) return `${id} is reserved, so no tenant can take it`;
  return null;
}

// A member of a registered tenant: the address and the tenant the operator invited it to.
const MEMBER_SQL =
  "SELECT m.email, m.tenant_id, m.created_at FROM members m JOIN tenants t ON t.id = m.tenant_id WHERE m.email = ?";
// A live session, only while its address is still a member of its tenant and the tenant is
// registered: removing either ends the session at its next request, before any row is deleted.
const SESSION_SQL =
  "SELECT s.tenant_id, s.email, s.expires_at FROM sessions s " +
  "JOIN members m ON m.email = s.email AND m.tenant_id = s.tenant_id " +
  "JOIN tenants t ON t.id = s.tenant_id WHERE s.token_hash = ? AND s.expires_at > ?";

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
  // one keeps its status, so registering again never resumes a paused tenant. An id
  // `refuseTenantId` refuses, or a status outside TENANT_STATUSES, writes nothing and answers
  // `{refused}`, the reason: an answer and not a throw, so the caller can tell the operator's
  // mistake from a registry that failed.
  register(id, { status, now = new Date() } = {}) {
    const refused = refuseTenantId(id);
    if (refused) return { refused };
    if (status !== undefined && !TENANT_STATUSES.includes(status)) {
      return { refused: `a tenant's status is one of ${TENANT_STATUSES.join(", ")}, not ${status}` };
    }
    const at = stamp(now);
    if (status === undefined) this.ctx.storage.sql.exec(TENANT_ADD, id, ACTIVE, at, at);
    else this.ctx.storage.sql.exec(TENANT_UPSERT, id, status, at, at);
    return { tenant: this.tenant(id) };
  }

  // Sets a registered tenant's schedule, `{ times, timeZone, dailyRunCap }`, checked by
  // `checkSchedule`, and answers `{tenant}`, its row, or `{refused}`, writing nothing. With no
  // `dailyRunCap` the tenant keeps the cap it has.
  setSchedule(id, schedule, { now = new Date() } = {}) {
    if (!this.tenant(id)) return { refused: `${id} is not a registered tenant` };
    const checked = checkSchedule(schedule);
    if (checked.refused) return checked;
    const { digestTimes, timeZone, dailyRunCap } = checked.schedule;
    this.ctx.storage.sql.exec(TENANT_SCHEDULE, JSON.stringify(digestTimes), timeZone, dailyRunCap, stamp(now), id);
    return { tenant: this.tenant(id) };
  }

  // Queues one run of `id`'s issue `{ period, date, origin }` (run_queue.js `queueRun`) and has
  // the alarm start it as soon as a slot is free. Leases already over are expired first, so an
  // issue whose stop report never came is queued again rather than refused as live.
  async queueRun(id, issue, { now = new Date() } = {}) {
    const told = [];
    const requeued = await expireLeases(this.ctx.storage.sql, now.getTime(), this.#finished, this.#tell(told));
    const answer = queueRun(this.ctx.storage.sql, this.tenant(id), issue, now.getTime());
    await this.#arm(now.getTime(), Boolean(answer.queued || requeued));
    await Promise.all(told);
    return answer;
  }

  // `id`'s container stopped, `{ exitCode, reason, run }` as its monitor saw it: the held run its
  // start ran, `run`, ends or goes back in the queue, and the alarm hands the freed slot on.
  async runEnded(id, outcome, { now = new Date() } = {}) {
    const told = [];
    const run = endRun(this.ctx.storage.sql, id, outcome, now.getTime(), this.#tell(told));
    await this.#arm(now.getTime(), true);
    await Promise.all(told);
    return run;
  }

  // `id`'s run the queue holds, starting or running, or null: the tenant's run object asks before
  // it alerts, since a held run's start and its no-instance stop are this queue's to tell.
  heldRun(id) {
    return heldRun(this.ctx.storage.sql, id);
  }

  // `id`'s most recent runs, newest first.
  runs(id) {
    return recentRuns(this.ctx.storage.sql, id);
  }

  // One pass of the run queue, then the next: a minute on while a run waits, and at the earliest
  // held run's lease end (run_queue.js `nextPassAt`).
  async alarm() {
    const told = [];
    const start = (run) =>
      tenantRun(this.env, run.tenant_id).startRun({
        period: run.period,
        date: run.date,
        run: { id: run.id, attempt: run.attempts },
      });
    await dispatchRuns(this.ctx.storage.sql, start, { finished: this.#finished, tell: this.#tell(told) });
    await this.#arm(Date.now(), false);
    await Promise.all(told);
  }

  // Sets the alarm for now when `immediate`, else for `nextPassAt`, unless one is already set
  // sooner; nothing when no run waits or is held.
  async #arm(nowMs, immediate) {
    const at = immediate ? nowMs : nextPassAt(this.ctx.storage.sql, nowMs);
    if (at === null) return;
    const set = await this.ctx.storage.getAlarm();
    if (set === null || set > at) await this.ctx.storage.setAlarm(at);
  }

  // The run queue's `tell`: each run it ends as failed or lost is told to the operator once,
  // through alertOperator, which names the tenant; the alerts go out once the queue's rows are
  // written, and `told` collects them for the caller to await.
  #tell(told) {
    return (run, cause) => told.push(this.#alertEnded(run, cause));
  }

  // One alert for `run`, ended by its start (`cause` "start") or by its lease ("lease"). None for
  // a start refused because the tenant is no longer active: that run was never due (ADR-0035).
  // None for a lease end whose run object already told the operator how that start ended, by the
  // run's own alert or by its abnormal stop, when the stop's report never reached this registry.
  async #alertEnded(run, cause) {
    if (cause === "start" && this.tenant(run.tenant_id)?.status !== ACTIVE) return;
    if (cause === "lease" && (await this.#endTold(run))) return;
    const kind = cause === "start" ? "start_failed" : run.state === LOST ? "lost" : "lost_failed";
    const detail = `period=${run.period} date=${run.date} run=${run.state} starts=${run.attempts}\n${run.outcome}`;
    await alertOperator(this.env, run.tenant_id, workerAlert(kind, detail));
  }

  // Whether `run`'s tenant's run object has told the operator how its last start ended; false when
  // it cannot say, so the alert is sent rather than lost.
  async #endTold(run) {
    try {
      return Boolean(await tenantRun(this.env, run.tenant_id).endTold({ id: run.id, attempt: run.attempts }));
    } catch {
      return false;
    }
  }

  // Whether the tenant's own digest_runs holds an ok run of `run`'s period since it started; false
  // when it cannot be read, so the run is queued again rather than taken as made.
  #finished = async (run) => {
    if (!this.env.TENANT_DB) return false;
    const rows = await tenantRows(this.env, run.tenant_id)(MADE_SINCE, [run.period, run.started_at], "digest_runs");
    return Boolean(rows?.length);
  };

  // AI spend (ai_budget.js, ADR-0038). Every month is a UTC `YYYY-MM`, taken from `now`.

  // `id`'s month so far and whether its allowance is spent: what its run asks as it starts, and
  // what its report shows. Null for a tenant the registry does not hold.
  budget(id, { now = new Date() } = {}) {
    const row = this.tenant(id);
    return row ? monthSpend(this.ctx.storage.sql, row, spendMonth(now)) : null;
  }

  // Adds one run's AI spend, as its run reported it, to `id`'s month, and answers `{spend}`, the
  // month after it, or `{refused}` for a report `checkSpend` refuses, writing nothing. A tenant
  // the registry no longer holds is still charged: what a run spent was spent.
  addSpend(id, report, { now = new Date() } = {}) {
    if (!TENANT.test(id ?? "")) return { refused: `not a tenant: ${id}` };
    const checked = checkSpend(report);
    if (checked.refused) return checked;
    const month = spendMonth(now);
    addSpend(this.ctx.storage.sql, id, checked.spend, month, stamp(now));
    const row = this.tenant(id) ?? { id, monthly_ai_budget_usd: null };
    return { spend: monthSpend(this.ctx.storage.sql, row, month) };
  }

  // Every registered tenant's `month`, this one unless named: the operator's list.
  spend({ month, now = new Date() } = {}) {
    return { month: month ?? spendMonth(now), tenants: spendReport(this.ctx.storage.sql, month ?? spendMonth(now)) };
  }

  // Sets a registered tenant's monthly allowance, in USD, from the next run that starts; answers
  // `{tenant}`, its row, or `{refused}`, writing nothing. What the month has spent stays.
  setBudget(id, raw, { now = new Date() } = {}) {
    if (!this.tenant(id)) return { refused: `${id} is not a registered tenant` };
    const checked = checkBudget(raw);
    if (checked.refused) return checked;
    this.ctx.storage.sql.exec(
      "UPDATE tenants SET monthly_ai_budget_usd = ?, updated_at = ? WHERE id = ?",
      checked.usd,
      stamp(now),
      id,
    );
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

  // The sign-in (ADR-0033). Only the operator's routes call `invite`, `revoke` and `members`; the
  // registry alone says which tenant an address may enter, and no request names one for it. Each
  // method is synchronous, so no other request runs between its read and its write. A refusal is
  // `{refused, status}`, the HTTP status the operator's route answers with.

  // Puts `email` on `tenant`'s invite list and answers `{member}`, its row. An address belongs to
  // one tenant: inviting it to a second is refused, and inviting it again to its own keeps the row.
  invite(tenant, rawEmail, { now = new Date() } = {}) {
    const email = normalEmail(rawEmail);
    if (!this.tenant(tenant)) return { refused: `${tenant} is not a registered tenant`, status: 404 };
    if (!isEmail(email)) return { refused: "the invite needs an email address", status: 400 };
    const sql = this.ctx.storage.sql;
    const [held] = sql.exec("SELECT tenant_id FROM members WHERE email = ?", email).toArray();
    if (held && held.tenant_id !== tenant) {
      return { refused: `${email} is already invited to ${held.tenant_id}`, status: 409 };
    }
    if (!held) sql.exec("INSERT INTO members (email, tenant_id, created_at) VALUES (?, ?, ?)", email, tenant, stamp(now));
    return { member: { ...sql.exec(MEMBER_SQL, email).one() } };
  }

  // Takes `email` off `tenant`'s list, and with it every session and sign-in link it holds, so the
  // next request of any of its sessions is refused. Answers `{revoked: {email, tenant_id,
  // sessions}}`, the sessions ended.
  revoke(tenant, rawEmail) {
    const email = normalEmail(rawEmail);
    const sql = this.ctx.storage.sql;
    const [held] = sql.exec("SELECT tenant_id FROM members WHERE email = ?", email).toArray();
    if (!held || held.tenant_id !== tenant) return { refused: `${email} is not invited to ${tenant}`, status: 404 };
    const sessions = sql.exec("SELECT count(*) AS n FROM sessions WHERE email = ?", email).one().n;
    sql.exec("DELETE FROM sessions WHERE email = ?", email);
    sql.exec("DELETE FROM invites WHERE email = ?", email);
    sql.exec("DELETE FROM sign_in_asks WHERE email = ?", email);
    sql.exec("DELETE FROM members WHERE email = ?", email);
    return { revoked: { email, tenant_id: tenant, sessions } };
  }

  // `tenant`'s invite list in address order, each with its live session count.
  members(tenant, { now = new Date() } = {}) {
    const rows = this.ctx.storage.sql.exec(
      "SELECT m.email, m.created_at, " +
        "(SELECT count(*) FROM sessions s WHERE s.email = m.email AND s.expires_at > ?) AS sessions " +
        "FROM members m WHERE m.tenant_id = ? ORDER BY m.email",
      stamp(now),
      tenant,
    );
    return rows.toArray().map((row) => ({ ...row }));
  }

  // Records a sign-in link for `email`, keyed by `codeHash`, the SHA-256 of a token only the mail
  // will carry, and answers `{tenant, expires_at}`: the tenant the link opens, from the invite
  // list. An address on no registered tenant's list gets `{refused}` and writes no row, so a
  // stranger asking adds nothing here. With `invitation`, the operator's invite route, a listed
  // address always gets its link. A reader's own ask also gets `{refused}`, and no row, when the
  // address was sent a link less than SIGN_IN_GAP_MS ago, which that link still serves, or when a
  // cap on asking is reached, `capped` naming which: SIGN_IN_ADDRESS_CAP asks for the address in
  // an hour, SIGN_IN_TENANT_CAP for its tenant's addresses in an hour, or SIGN_IN_DAILY_CEILING
  // for everyone in a day. Only an ask that is issued a link writes a `sign_in_asks` row, kept a
  // day, so that table holds at most SIGN_IN_DAILY_CEILING rows.
  issueSignIn(rawEmail, codeHash, { now = new Date(), invitation = false } = {}) {
    const email = normalEmail(rawEmail);
    const sql = this.ctx.storage.sql;
    const since = (ms) => stamp(new Date(now.getTime() - ms));
    sql.exec("DELETE FROM invites WHERE expires_at <= ?", stamp(now));
    sql.exec("DELETE FROM sign_in_asks WHERE asked_at <= ?", since(SIGN_IN_DAY_MS));
    const [member] = sql.exec(MEMBER_SQL, email).toArray();
    if (!member) return { refused: "not invited" };
    if (!invitation) {
      const refused = this.#refuseAsk(email, member.tenant_id, since);
      if (refused) return refused;
      sql.exec("INSERT INTO sign_in_asks (email, tenant_id, asked_at) VALUES (?, ?, ?)", email, member.tenant_id, stamp(now));
    }
    const expires = after(now, SIGN_IN_TTL_MS);
    sql.exec(
      "INSERT INTO invites (code, email, tenant_id, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
      codeHash,
      email,
      member.tenant_id,
      stamp(now),
      expires,
    );
    return { tenant: member.tenant_id, expires_at: expires };
  }

  // Why a reader's ask for `email`, a member of `tenant`, gets no link now, or null.
  #refuseAsk(email, tenant, since) {
    const sql = this.ctx.storage.sql;
    const count = (where, ...values) => sql.exec(`SELECT count(*) AS n FROM ${where}`, ...values).one().n;
    if (count("invites WHERE email = ? AND used_at IS NULL AND created_at > ?", email, since(SIGN_IN_GAP_MS)) > 0) {
      return { refused: "a link was sent less than a minute ago" };
    }
    const hour = since(SIGN_IN_HOUR_MS);
    if (count("sign_in_asks WHERE email = ? AND asked_at > ?", email, hour) >= SIGN_IN_ADDRESS_CAP) {
      return { refused: `${SIGN_IN_ADDRESS_CAP} links were sent to this address in an hour`, capped: "address" };
    }
    if (count("sign_in_asks WHERE tenant_id = ? AND asked_at > ?", tenant, hour) >= SIGN_IN_TENANT_CAP) {
      return { refused: `${SIGN_IN_TENANT_CAP} links were sent to ${tenant}'s readers in an hour`, capped: "tenant" };
    }
    if (count("sign_in_asks WHERE asked_at > ?", since(SIGN_IN_DAY_MS)) >= SIGN_IN_DAILY_CEILING) {
      return { refused: `${SIGN_IN_DAILY_CEILING} links were sent in a day`, capped: "daily" };
    }
    return null;
  }

  // Spends the sign-in link `codeHash` and opens a session keyed by `sessionHash` for the address
  // and tenant the link was issued to, never for any the caller names. Answers `{tenant,
  // expires_at}`, or null for a link that is unknown, used, expired, or whose address has since
  // left the tenant's list: the caller cannot tell which, and neither can the reader.
  redeemSignIn(codeHash, sessionHash, { now = new Date() } = {}) {
    const sql = this.ctx.storage.sql;
    const [link] = sql
      .exec("SELECT email, tenant_id FROM invites WHERE code = ? AND used_at IS NULL AND expires_at > ?", codeHash, stamp(now))
      .toArray();
    if (!link) return null;
    sql.exec("UPDATE invites SET used_at = ? WHERE code = ?", stamp(now), codeHash);
    const [member] = sql.exec(MEMBER_SQL, link.email).toArray();
    if (!member || member.tenant_id !== link.tenant_id) return null;
    sql.exec("DELETE FROM sessions WHERE expires_at <= ?", stamp(now));
    const expires = after(now, SESSION_TTL_MS);
    sql.exec(
      "INSERT INTO sessions (token_hash, tenant_id, email, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
      sessionHash,
      link.tenant_id,
      link.email,
      stamp(now),
      expires,
    );
    return { tenant: link.tenant_id, expires_at: expires };
  }

  // The live session keyed by `sessionHash`, `{tenant_id, email, expires_at}`, or null. Read on
  // every request, never cached, so an ended or revoked session is refused at its next one.
  session(sessionHash, { now = new Date() } = {}) {
    const [row] = this.ctx.storage.sql.exec(SESSION_SQL, sessionHash, stamp(now)).toArray();
    return row ? { ...row } : null;
  }

  // Ends one session: the reader's sign-out.
  endSession(sessionHash) {
    this.ctx.storage.sql.exec("DELETE FROM sessions WHERE token_hash = ?", sessionHash);
    return { ended: true };
  }
}
