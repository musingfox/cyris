// The operator's registry: one SQLite Durable Object that holds what belongs to the hosted
// deployment rather than to any one tenant. Why one object and not KV or D1:
// docs/decisions/0030-the-operator-registry-is-one-sqlite-durable-object.md.
//
// Only the hosted Worker reaches it, through its REGISTRY binding (wrangler.hosted.toml). No
// tenant's run container does: it holds no binding, and the only Worker code it can reach is
// TenantDbGateway (tenant_db.js), which answers one tenant's queries and never names this object.

import { DurableObject } from "cloudflare:workers";
import RESERVED_IDS from "./hosted_reserved_ids.json";
import {
  SESSION_TTL_MS,
  SIGN_IN_DAILY_CAP,
  SIGN_IN_DAY_MS,
  SIGN_IN_GAP_MS,
  SIGN_IN_HOURLY_CAP,
  SIGN_IN_HOUR_MS,
  SIGN_IN_TTL_MS,
  isEmail,
  normalEmail,
} from "./hosted_session.js";
import { LOCATION_HINT, TENANT } from "./tenant_db.js";
import { ACTIVE, TENANT_STATUSES } from "./tenant_status.js";

// The registry's name in its namespace. There is one registry, so the name is fixed; it is
// outside TENANT, so no tenant id can ever name it.
export const REGISTRY_NAME = "cyris:registry";

// The registry's own tables: not schema.sql, which is a tenant's. `tenants` is the tenant list.
// The sign-in (ADR-0033) has three: `members`, the operator's invite list, one tenant per
// address; `invites`, each single-use sign-in link, the operator's invitation and every later
// one alike, keyed by its token's SHA-256 in `code` and never by the token; and `sessions`, keyed
// the same way. `inbound_addresses` and `spend_rollup` are created empty for the newsletter and
// budget cards, so those cards add code and no new home for state. `digest_times` is the
// tenant's digest hours as a JSON list, kept for the scheduling card; until it lands the tick
// still reads each tenant's schedule from the tenant's own database.
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
CREATE INDEX IF NOT EXISTS idx_sessions_email ON sessions(email);
CREATE INDEX IF NOT EXISTS idx_invites_email ON invites(email);
CREATE TABLE IF NOT EXISTS members (
  email TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_members_tenant ON members(tenant_id);
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
  // stranger asking adds nothing here. A listed address also gets `{refused}`, and no row, when it
  // was sent a link less than SIGN_IN_GAP_MS ago, which that link still serves, or when a cap is
  // reached: SIGN_IN_DAILY_CAP links to it in a day, or SIGN_IN_HOURLY_CAP to everyone in an
  // hour, `capped` naming which. A link's row is kept a day past its issue, spent or expired, to
  // count against the caps, so the table holds at most a day of the caps' links.
  issueSignIn(rawEmail, codeHash, { now = new Date() } = {}) {
    const email = normalEmail(rawEmail);
    const sql = this.ctx.storage.sql;
    const since = (ms) => stamp(new Date(now.getTime() - ms));
    sql.exec("DELETE FROM invites WHERE created_at <= ?", since(SIGN_IN_DAY_MS));
    const [member] = sql.exec(MEMBER_SQL, email).toArray();
    if (!member) return { refused: "not invited" };
    const recent = sql.exec(
      "SELECT count(*) AS n FROM invites WHERE email = ? AND used_at IS NULL AND created_at > ?",
      email,
      since(SIGN_IN_GAP_MS),
    );
    if (recent.one().n > 0) return { refused: "a link was sent less than a minute ago" };
    const today = sql.exec("SELECT count(*) AS n FROM invites WHERE email = ?", email).one().n;
    if (today >= SIGN_IN_DAILY_CAP) {
      return { refused: `${SIGN_IN_DAILY_CAP} links were sent to this address in a day`, capped: "daily" };
    }
    const hour = sql.exec("SELECT count(*) AS n FROM invites WHERE created_at > ?", since(SIGN_IN_HOUR_MS)).one().n;
    if (hour >= SIGN_IN_HOURLY_CAP) return { refused: `${SIGN_IN_HOURLY_CAP} links were sent in an hour`, capped: "hourly" };
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
