// A hosted tenant's tables: one SQLite Durable Object per tenant, and the gateway its run
// container reaches it through. Why a Durable Object and not D1:
// docs/decisions/0028-one-sqlite-durable-object-per-hosted-tenant.md.
//
// The container sends each query to http://db.cyris/t/<tenant>/query. Its Durable Object
// (hosted_container.js) intercepts that host before the container starts and hands it to
// TenantDbGateway with `props.tenant` set to its own name, so a gateway answers one tenant
// and refuses every other request with 403; the container holds no token and no binding.

import { DurableObject, WorkerEntrypoint } from "cloudflare:workers";
import FIELDS from "../../../src/cyris/settings_fields.json";
import { dumpObject, restoreObject } from "./sqlite_dump.js";
import { SETTING_UPSERT, SOURCE_UPSERT, stamp } from "./hosted_seed.js";
import { POLICY, SCHEDULE_KEY, TIMEZONE_KEY, WRITABLE } from "./hosted_settings_rules.js";
import { pollTenantBuffer } from "./tenant_buffer.js";
import { storeNewsletter } from "./tenant_inbound.js";
import { queueVote } from "./tenant_votes.js";

// The host the container queries; `GATEWAY_ORIGIN` in src/cyris/adapters/store/tenant_gateway.py
// names the same one, and tests/test_hosted_config.py holds the two together.
export const DB_HOST = "db.cyris";

// A tenant id: one path segment, and the name of the tenant's Durable Objects. `config.TENANT_ID`
// in the container holds the same pattern.
export const TENANT = /^[a-z0-9-]{1,40}$/;

// Only an object's first get() honours a location hint, and the operator's seed may be that
// first get(), from anywhere: so every get() names the one region the hosted container runs in
// (`[containers.constraints] regions` in wrangler.hosted.toml), where the probe measured
// 42 ms a read and 66 ms a write.
// https://developers.cloudflare.com/durable-objects/reference/data-location/#provide-a-location-hint
export const LOCATION_HINT = "wnam";

export const tenantDb = (env, tenant) => env.TENANT_DB.getByName(tenant, { locationHint: LOCATION_HINT });

// The object's own tables start with "_" (`_cf_KV`, `_cf_METADATA`) and SQLite's with
// "sqlite_"; neither is the tenant's, so no count of the tenant's tables includes them.
export const isTenantTable = (name) => !name.startsWith("_") && !name.startsWith("sqlite_");

// One query on a tenant's database, for the Worker's own reads (schedule.js): its rows, or
// null when it cannot answer, with a warning naming `what` and the tenant.
export const tenantRows = (env, tenant) => async (sql, params, what) => {
  try {
    return (await tenantDb(env, tenant).query(sql, params)).rows;
  } catch (error) {
    console.warn(`${what} read failed for tenant ${tenant}:`, String(error?.message ?? error));
    return null;
  }
};

// `name` as an SQL identifier: in double quotes, each double quote in it doubled.
export const quotedName = (name) => `"${String(name).replaceAll('"', '""')}"`;

const hasTable = (sql, name) =>
  sql.exec("SELECT 1 AS found FROM sqlite_master WHERE type = 'table' AND name = ?", name).toArray().length > 0;

// A stored setting or config: JSON, as the container's stores write it; the text itself if not.
const stored = (value) => {
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
};

// The schedule's copy in the tenant's own settings (hosted_seed.js `writeSchedule`), shown beside
// the writable keys; the registry's is what the tick reads.
const SCHEDULE_COPY = [SCHEDULE_KEY, TIMEZONE_KEY];

export class TenantDb extends DurableObject {
  // `D1Queryable.query`'s shape: SQL and its bound values in, rows and rows changed out.
  // `changes` is the difference of two `total_changes()` readings, the rows-affected figure D1
  // reports; `rowsWritten` would count index writes too (2 for a one-row insert with a key).
  query(sql, params = []) {
    const storage = this.ctx.storage.sql;
    const total = () => storage.exec("SELECT total_changes() AS n").one().n;
    const before = total();
    const rows = storage.exec(sql, ...params).toArray();
    return { rows, changes: total() - before };
  }

  // One poll of the tenant's RSS feeds into its own buffer (tenant_buffer.js), which the hourly
  // tick asks for: the feed requests are this object's invocation's, not the tick's. `ownHosts`,
  // the hosted Worker's names, are hosts no feed of the tenant's may be fetched from.
  pollRss({ ownHosts = [] } = {}) {
    return pollTenantBuffer(this.ctx.storage, { ownHosts });
  }

  // One newsletter the hosted Worker's email() handler took at this tenant's address, into its own
  // queue (tenant_inbound.js); the handler found the tenant in the registry, and the address is
  // not passed here.
  receiveNewsletter(id, record) {
    return storeNewsletter(this.ctx.storage, id, record);
  }

  // One vote the hosted Worker took from a reader whose session names this tenant, into the
  // tenant's own vote queue (tenant_votes.js); the Worker took the tenant from the session, and
  // the object checks that the article is its own.
  recordVote(vote) {
    return queueVote(this.ctx.storage, vote);
  }

  // The tenant's own settings a reader may see, as values: by default every writable key and the
  // schedule's copy; or `keys`, any grade-D key the settings page shows
  // (hosted_shared_settings.js), but never a notify target, which the run pins blank
  // whatever a row holds (ADR-0040). Null for a tenant never seeded, which has no `settings` table.
  settings(keys = [...WRITABLE, ...SCHEDULE_COPY]) {
    const sql = this.ctx.storage.sql;
    if (!hasTable(sql, "settings")) return null;
    const asked = keys.filter((key) => Object.hasOwn(FIELDS, key) && !POLICY.blank_settings.includes(key));
    if (!asked.length) return {};
    const rows = sql.exec(`SELECT key, value FROM settings WHERE key IN (${asked.map(() => "?").join(", ")})`, ...asked);
    return Object.fromEntries(rows.toArray().map((row) => [row.key, stored(row.value)]));
  }

  // Writes `rows`, [key, value] each, in one transaction, as the container's store writes a
  // setting (JSON). The Worker checked each value; this refuses any key the policy does not let a
  // tenant write, so no caller of the object can write the provider or a notify target through it.
  saveSettings(rows, now = new Date()) {
    const storage = this.ctx.storage;
    if (!hasTable(storage.sql, "settings")) return { refused: "not_seeded", status: 409 };
    const denied = rows.find(([key]) => !WRITABLE.includes(key));
    if (denied) return { refused: "operator_key", key: denied[0], status: 400 };
    storage.transactionSync(() => {
      for (const [key, value] of rows) storage.sql.exec(SETTING_UPSERT, key, JSON.stringify(value), stamp(now));
    });
    return { saved: rows.map(([key]) => key) };
  }

  // The tenant's sources with what the shared page lists of each (hosted_shared_settings.js): its
  // columns, its config parsed, and for an RSS feed its poll record, which `D1FeedHealth` reads
  // by the same join (src/cyris/adapters/store/feed_health.py): the `feed_health` row the poll
  // writes (tenant_buffer.js) and the newest article it stored. A feed never polled has a record
  // of nothing. Health is an annotation: a failed read of it leaves every `health` null and the
  // list still answers. Null for a tenant never seeded.
  sourceList() {
    const sql = this.ctx.storage.sql;
    if (!hasTable(sql, "sources")) return null;
    const rows = sql.exec("SELECT name, url, type, config FROM sources ORDER BY name").toArray();
    let polled = new Map();
    try {
      polled = new Map(
        sql
          .exec(
            `SELECT s.name, h.consecutive_failures, h.last_error, h.last_failed_at, h.last_ok_at, a.newest_article_at
               FROM sources s
               LEFT JOIN feed_health h ON h.name = s.name
               LEFT JOIN (SELECT source_name, max(first_seen_at) AS newest_article_at
                            FROM stored_articles GROUP BY source_name) a ON a.source_name = s.name
              WHERE s.type = 'rss' AND s.url IS NOT NULL`,
          )
          .toArray()
          .map(({ name, consecutive_failures, ...rest }) => [name, { consecutive_failures: consecutive_failures ?? 0, ...rest }]),
      );
    } catch (error) {
      console.warn("feed_health read failed:", String(error?.message ?? error));
    }
    return rows.map(({ name, url, type, config }) => ({ name, url, type, config: stored(config) ?? {}, health: polled.get(name) ?? null }));
  }

  // Adds `row` ({name, url, type, config}) or rewrites the RSS source of its name, keeping what
  // else that row's config held (its tags, language). The count and the write are one
  // synchronous turn of this object, so two saves at once cannot both take the last place under
  // `cap`: a new name past it is refused, a rewrite never is. A name a newsletter source holds is
  // refused, as the operator's newsletter route refuses an RSS one's.
  saveSource(row, cap) {
    const storage = this.ctx.storage;
    if (!hasTable(storage.sql, "sources")) return { refused: "not_seeded", status: 409 };
    return storage.transactionSync(() => {
      const held = storage.sql.exec("SELECT type, config FROM sources WHERE name = ?", row.name).toArray()[0];
      if (held && held.type !== row.type) return { refused: "source_taken", status: 409 };
      if (!held && storage.sql.exec("SELECT count(*) AS n FROM sources").one().n >= cap) {
        return { refused: "source_cap", status: 409 };
      }
      const config = { ...(held ? stored(held.config) : {}), ...row.config };
      storage.sql.exec(SOURCE_UPSERT, row.name, row.url, row.type, JSON.stringify(config));
      return { source: { ...row, config } };
    });
  }

  // Removes the source `name`, of either type; `{deleted}` is how many rows went, 0 or 1.
  deleteSource(name) {
    const storage = this.ctx.storage;
    if (!hasTable(storage.sql, "sources")) return { refused: "not_seeded", status: 409 };
    return { deleted: this.query("DELETE FROM sources WHERE name = ?", [name]).changes };
  }

  // Each of the tenant's tables with its row count, in name order. A table's name cannot be bound,
  // and the tenant's container can create a table of any name, a double quote or a second
  // statement in it included; so each name is quoted as an SQL identifier, its double quotes
  // doubled, and is only ever the one name it says. Every table is counted, not only the ones
  // schema.sql makes, so the operator's report shows what else the container created.
  tables() {
    const storage = this.ctx.storage.sql;
    const names = storage
      .exec("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
      .toArray()
      .map((row) => row.name)
      .filter(isTenantTable);
    return Object.fromEntries(
      names.map((name) => [name, storage.exec(`SELECT count(*) AS n FROM ${quotedName(name)}`).one().n]),
    );
  }

  // The whole database as one gzip dump (sqlite_dump.js), for the daily backup (hosted_backup.js).
  exportDump() {
    return dumpObject(this.ctx);
  }

  // A dump loaded into this object, which must hold no table yet: the operator's restore of an
  // export into a new tenant id (hosted_backup.js).
  restoreDump(bytes) {
    return restoreObject(this.ctx, bytes);
  }
}

// D1's error shape, so the container's client reads the reason the way it reads D1's.
const refuse = (status, message) => Response.json({ errors: [{ message }] }, { status });

// The container's only way to a database. It runs in the Workers runtime, so it holds the
// binding; `props.tenant` is set by the Durable Object that registered it, never by the container.
export class TenantDbGateway extends WorkerEntrypoint {
  async fetch(request) {
    // Every other request is refused 403 before any object is reached: another tenant's
    // query, one aimed at the operator's registry (tenant_registry.js, which this gateway never
    // names and whose name no tenant id can take), or any other path or method.
    const parts = new URL(request.url).pathname.split("/").filter(Boolean);
    if (request.method !== "POST" || parts.length !== 3 || parts[0] !== "t" || parts[2] !== "query") {
      return refuse(403, "this gateway answers only POST /t/<tenant>/query");
    }
    // A gateway registered without a tenant is a wiring fault, never a gateway to every tenant.
    const tenant = this.ctx.props?.tenant;
    if (!tenant || parts[1] !== tenant) return refuse(403, `tenant ${parts[1]} is not this run's`);
    return answerQuery(tenantDb(this.env, tenant), request);
  }
}

// One `{sql, params}` request answered from `db`, a tenant's object: `{rows, changes}`, or D1's
// error shape with `failureStatus`'s status. The run's gateway and the operator's query route
// (hosted.js) both answer through here, so the operator's client reads a tenant exactly as the
// tenant's own run does. Whose tenant `db` is, and who may ask, is the caller's to have checked.
export async function answerQuery(db, request) {
  let body;
  try {
    body = await request.json();
  } catch (error) {
    return refuse(400, `the body is not JSON: ${error}`);
  }
  const params = body?.params ?? [];
  if (typeof body?.sql !== "string" || !Array.isArray(params)) {
    return refuse(400, "the body must be {sql, params}");
  }
  try {
    return Response.json(await db.query(body.sql, params));
  } catch (error) {
    return refuse(failureStatus(error), String(error?.message ?? error));
  }
}

// The status a failed query is answered with; the container's client retries a 5xx and no 4xx.
// An overloaded object is answered 429 whatever `retryable` says: Cloudflare's guidance is not
// to retry it, since a retry adds to the load that refused it. A reset object that can be
// retried is 503. A statement SQLite refused is the request's own fault, 400: repeating it
// would only delay saying so.
// https://developers.cloudflare.com/durable-objects/best-practices/error-handling/
export function failureStatus(error) {
  if (error?.overloaded) return 429;
  return error?.retryable ? 503 : 400;
}
