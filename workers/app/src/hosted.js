// The hosted Worker, `cyris-hosted` (wrangler.hosted.toml): many tenants' digests,
// each run against the tenant's own SQLite Durable Object. A separate deployment from the
// default Worker in index.js, which shares no binding, class or route with it.
// docs/decisions/0028-one-sqlite-durable-object-per-hosted-tenant.md.
//
// It serves the operator's routes behind Access (hosted_router.js) and runs the hourly tick
// for every tenant. Until later cards land, a tenant is served no page, its run publishes
// nothing and notifies nobody. The tenant list is the operator's registry (tenant_registry.js).
import { env } from "cloudflare:workers";
import SCHEMA_SQL from "../../../src/cyris/adapters/store/schema.sql";
import FEED_CATALOG from "../../../src/cyris/onboarding_feeds.json";
import STARTING_VALUES from "../../../src/cyris/settings_starting_values.json";
import { accessKeys } from "./access_keys.js";
import SEED from "./hosted_seed.json";
import { handleHostedRequest } from "./hosted_router.js";
import { seedTenant, tenantReport } from "./hosted_seed.js";
import { planTick, readTimeZone } from "./schedule.js";
import { LOCATION_HINT, tenantDb, tenantRows } from "./tenant_db.js";
import { tenantRegistry } from "./tenant_registry.js";
import { ACTIVE } from "./tenant_status.js";

export { TenantContainer } from "./hosted_container.js";
export { TenantDb, TenantDbGateway } from "./tenant_db.js";
export { TenantRegistry } from "./tenant_registry.js";

// The same hint as the tenant's database: only an object's first get() honours one.
const tenantRun = (tenant) => env.TENANT_RUN.getByName(tenant, { locationHint: LOCATION_HINT });

const startRun = (tenant, period, date) => tenantRun(tenant).startRun({ period, date });

export default {
  async fetch(request) {
    return handleHostedRequest(request, env, {
      accessKeys: accessKeys(env.CYRIS_ACCESS_TEAM_DOMAIN),
      register: (tenant, status) => tenantRegistry(env).register(tenant, { status }),
      tenant: (tenant) => tenantRegistry(env).tenant(tenant),
      seed: (tenant) =>
        seedTenant({
          db: tenantDb(env, tenant),
          tenant,
          schemaSql: SCHEMA_SQL,
          startingValues: STARTING_VALUES,
          seed: SEED,
          catalog: FEED_CATALOG,
        }),
      report: (tenant) => tenantReport({ db: tenantDb(env, tenant), tenant }),
      timeZone: (tenant) => readTimeZone({ env, read: tenantRows(env, tenant) }),
      startRun,
    });
  },

  // Each active tenant of the registry asks its own database whether this is a digest hour or
  // one to catch up; a tenant of any other status gets no tick. A tenant whose schedule cannot
  // be read gets no start: its container would reach the same object. One tenant's failure is
  // logged and leaves the others' ticks alone.
  async scheduled() {
    const tenants = (await tenantRegistry(env).tenants({ status: ACTIVE })).map((row) => row.id);
    const ticks = await Promise.allSettled(
      tenants.map(async (tenant) => {
        const plan = await planTick({ env, read: tenantRows(env, tenant), unreadable: null });
        if (!plan) return;
        if (plan.period) console.log("catching up a digest hour with no ok run", { tenant, ...plan });
        await startRun(tenant, plan.period, plan.date);
      }),
    );
    ticks.forEach((tick, i) => {
      if (tick.status === "rejected") {
        console.log("tick failed", { tenant: tenants[i], error: String(tick.reason) });
      }
    });
  },
};
