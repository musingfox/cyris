// The hosted Worker, `cyris-hosted` (wrangler.hosted.toml): many tenants' digests,
// each run against the tenant's own SQLite Durable Object. A separate deployment from the
// default Worker in index.js, which shares no binding, class or route with it.
// docs/decisions/0028-one-sqlite-durable-object-per-hosted-tenant.md.
//
// It serves the operator's routes and each tenant's pages behind Access (hosted_router.js,
// hosted_site.js) and runs the hourly tick for every tenant: each tenant's RSS poll into its own
// buffer (tenant_buffer.js), then its run when one is due. Its email() handler takes each
// tenant's newsletters at the tenant's random address into the tenant's own object
// (hosted_inbound.js). Until later cards land, a run notifies nobody. The tenant list is the
// operator's registry (tenant_registry.js).
import { env } from "cloudflare:workers";
import HOSTED_SCHEMA_SQL from "../../../src/cyris/adapters/store/hosted_schema.sql";
import SCHEMA_SQL from "../../../src/cyris/adapters/store/schema.sql";
import FEED_CATALOG from "../../../src/cyris/onboarding_feeds.json";
import STARTING_VALUES from "../../../src/cyris/settings_starting_values.json";
import { accessKeys } from "./access_keys.js";
import SEED from "./hosted_seed.json";
import { receiveInbound } from "./hosted_inbound.js";
import { handleHostedRequest } from "./hosted_router.js";
import { createSitePage } from "./hosted_site.js";
import { addNewsletterSource, seedTenant, tenantReport } from "./hosted_seed.js";
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

// One poll of `tenant`'s buffer through `call`, which counts it, and what it did; a failed one is
// logged and answers its error.
async function pollTenant(tenant, call = (what) => what) {
  try {
    const polled = await call(tenantDb(env, tenant).pollRss());
    console.log("rss poll", { tenant, ...pollMeasure(polled) });
    return { tenant, ...polled };
  } catch (error) {
    console.log("rss poll failed", { tenant, error: String(error?.message ?? error) });
    return { tenant, error: String(error?.message ?? error) };
  }
}

// One tenant's poll in counts: its feeds, the feed requests it made, what they held, what it wrote.
const pollMeasure = (polled) => ({
  feeds: polled.feeds,
  fetches: polled.fetches,
  entries: polled.entries,
  fresh: polled.fresh,
  written: polled.written,
  failed_feeds: polled.failures.length,
});

// The tick's own measurement, one log line for the operator to read beside the invocation's CPU
// time in Workers Logs (docs/hosted-tenants.md): how many tenants it polled and how many polls
// failed; `object_calls`, the Durable Object calls the tick made itself (the registry, each
// tenant's poll, its schedule reads and its start), which are the tick's own subrequests if
// Cloudflare counts them as such; `feed_fetches`, the feed requests the tenants' polls made, each
// in that tenant's object's invocation; and its wall time.
function tickMeasure(tick, tenants) {
  const polled = tick.polls.filter((poll) => !poll.error);
  const sum = (key) => polled.reduce((total, poll) => total + poll[key], 0);
  return {
    tenants: tenants.length,
    polled: polled.length,
    failed_polls: tick.polls.length - polled.length,
    feeds: sum("feeds"),
    feed_fetches: sum("fetches"),
    max_tenant_fetches: Math.max(0, ...polled.map((poll) => poll.fetches)),
    written: sum("written"),
    object_calls: tick.calls,
    wall_ms: Date.now() - tick.started,
  };
}

// One memo of each tenant's type size per isolate, as module scope holds it.
const sitePage = createSitePage(env);

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
          hostedSchemaSql: HOSTED_SCHEMA_SQL,
          startingValues: STARTING_VALUES,
          seed: SEED,
          catalog: FEED_CATALOG,
        }),
      report: (tenant) => tenantReport({ db: tenantDb(env, tenant), tenant }),
      timeZone: (tenant) => readTimeZone({ env, read: tenantRows(env, tenant) }),
      startRun,
      poll: (tenant) => pollTenant(tenant),
      inboundAddress: (tenant) => tenantRegistry(env).inboundAddress(tenant),
      rotateAddress: (tenant) => tenantRegistry(env).rotateInboundAddress(tenant),
      revokeAddress: (tenant) => tenantRegistry(env).revokeInboundAddress(tenant),
      addNewsletterSource: (tenant, params) => addNewsletterSource({ db: tenantDb(env, tenant), params }),
      page: sitePage,
    });
  },

  // Every message the catch-all rule routes here: stored in the tenant whose address it came to,
  // or rejected. A paused tenant still takes mail, so pausing loses none; its queue's cap bounds it.
  async email(message) {
    await receiveInbound(message, {
      tenantFor: (address) => tenantRegistry(env).inboundTenant(address),
      store: (tenant, id, record) => tenantDb(env, tenant).receiveNewsletter(id, record),
    });
  },

  // Each active tenant of the registry first polls its RSS feeds into its own buffer, then asks
  // its own database whether this is a digest hour or one to catch up; a tenant of any other
  // status is neither polled nor ticked. The poll comes first so that a run this tick starts reads
  // this hour's entries: they are stamped when the poll starts, before the run's window closes.
  // A poll that fails is logged, and its tenant's run still starts on what its buffer holds. A
  // tenant whose schedule cannot be read gets no start: its container would reach the same
  // object. One tenant's failure is logged and leaves the others' polls and ticks alone.
  async scheduled() {
    const tick = { started: Date.now(), calls: 0, polls: [] };
    const call = (what) => {
      tick.calls += 1;
      return what;
    };
    const tenants = (await call(tenantRegistry(env).tenants({ status: ACTIVE }))).map((row) => row.id);
    const ticks = await Promise.allSettled(
      tenants.map(async (tenant) => {
        tick.polls.push(await pollTenant(tenant, call));
        const read = tenantRows(env, tenant);
        const plan = await planTick({ env, read: (...args) => call(read(...args)), unreadable: null });
        if (!plan) return;
        if (plan.period) console.log("catching up a digest hour with no ok run", { tenant, ...plan });
        await call(startRun(tenant, plan.period, plan.date));
      }),
    );
    ticks.forEach((result, i) => {
      if (result.status === "rejected") {
        console.log("tick failed", { tenant: tenants[i], error: String(result.reason) });
      }
    });
    console.log("hosted tick", tickMeasure(tick, tenants));
  },
};
