// The hosted Worker, `cyris-hosted` (wrangler.hosted.toml): many tenants' digests,
// each run against the tenant's own SQLite Durable Object. A separate deployment from the
// default Worker in index.js, which shares no binding, class or route with it.
// docs/decisions/0028-one-sqlite-durable-object-per-hosted-tenant.md.
//
// It serves the operator's routes and each tenant's pages behind Access (hosted_router.js,
// hosted_pages.js) and runs the hourly tick for every tenant. Until later cards land, a run
// notifies nobody and the tenant list is the stub in wrangler.hosted.toml.
import { env } from "cloudflare:workers";
import HOSTED_SCHEMA_SQL from "../../../src/cyris/adapters/store/hosted_schema.sql";
import SCHEMA_SQL from "../../../src/cyris/adapters/store/schema.sql";
import FEED_CATALOG from "../../../src/cyris/onboarding_feeds.json";
import STARTING_VALUES from "../../../src/cyris/settings_starting_values.json";
import { accessKeys } from "./access_keys.js";
import SEED from "./hosted_seed.json";
import { serveTenantPage } from "./hosted_pages.js";
import { handleHostedRequest } from "./hosted_router.js";
import { seedTenant, tenantReport } from "./hosted_seed.js";
import { planTick, readTimeZone } from "./schedule.js";
import { LOCATION_HINT, hostedTenants, tenantDb, tenantRows } from "./tenant_db.js";
import { createTypeScale } from "./type_scale.js";

export { TenantContainer } from "./hosted_container.js";
export { TenantDb, TenantDbGateway } from "./tenant_db.js";

// The same hint as the tenant's database: only an object's first get() honours one.
const tenantRun = (tenant) => env.TENANT_RUN.getByName(tenant, { locationHint: LOCATION_HINT });

const startRun = (tenant, period, date) => tenantRun(tenant).startRun({ period, date });

// Each tenant's type size, read from its own object; module scope, so one memo per isolate and
// tenant, as index.js holds one for the deployment (ADR-0013).
const typeScales = new Map();
const typeScaleOf = (tenant) => {
  if (!typeScales.has(tenant)) typeScales.set(tenant, createTypeScale({ env, read: tenantRows(env, tenant) }));
  return typeScales.get(tenant);
};

// As index.js's: the style goes at the end of <head>.
const injectStyle = (response, html) =>
  new HTMLRewriter()
    .on("head", {
      element(head) {
        head.append(html, { html: true });
      },
    })
    .transform(response);

export default {
  async fetch(request) {
    return handleHostedRequest(request, env, {
      accessKeys: accessKeys(env.CYRIS_ACCESS_TEAM_DOMAIN),
      tenants: hostedTenants(env),
      seed: (tenant) =>
        seedTenant({
          db: tenantDb(env, tenant),
          tenant,
          schemaSql: SCHEMA_SQL,
          hostedSchemaSql: HOSTED_SCHEMA_SQL,
          startingValues: STARTING_VALUES,
          seed: SEED,
          catalog: FEED_CATALOG,
        }),
      report: (tenant) => tenantReport({ db: tenantDb(env, tenant), tenant }),
      timeZone: (tenant) => readTimeZone({ env, read: tenantRows(env, tenant) }),
      startRun,
      page: (request, tenant, rest) =>
        serveTenantPage(request, {
          tenant,
          rest,
          db: tenantDb(env, tenant),
          deps: { typeScale: typeScaleOf(tenant), injectStyle },
        }),
    });
  },

  // Each tenant's tick asks its own database whether this is a digest hour or one to catch up.
  // A tenant whose schedule cannot be read gets no start: its container would reach the same
  // object. One tenant's failure is logged and leaves the others' ticks alone.
  async scheduled() {
    const tenants = hostedTenants(env);
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
