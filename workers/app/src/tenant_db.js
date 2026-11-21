// A hosted tenant's tables: one SQLite Durable Object per tenant, and the gateway its run
// container reaches it through. Why a Durable Object and not D1:
// docs/decisions/0028-one-sqlite-durable-object-per-hosted-tenant.md.
//
// The container sends each query to http://db.cyris/t/<tenant>/query. Its Durable Object
// (hosted_container.js) intercepts that host before the container starts and hands it to
// TenantDbGateway with `props.tenant` set to its own name, so a gateway answers one tenant
// and refuses every other request with 403; the container holds no token and no binding.

import { DurableObject, WorkerEntrypoint } from "cloudflare:workers";
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
  // tick asks for: the feed requests are this object's invocation's, not the tick's.
  pollRss() {
    return pollTenantBuffer(this.ctx.storage);
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

  // Each of the tenant's tables with its row count, in name order.
  tables() {
    const storage = this.ctx.storage.sql;
    const names = storage
      .exec("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
      .toArray()
      .map((row) => row.name)
      .filter(isTenantTable);
    return Object.fromEntries(
      names.map((name) => [name, storage.exec(`SELECT count(*) AS n FROM "${name}"`).one().n]),
    );
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
