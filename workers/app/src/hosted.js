// The hosted Worker, `cyris-hosted` (wrangler.hosted.toml): many tenants' digests,
// each run against the tenant's own SQLite Durable Object. A separate deployment from the
// default Worker in index.js, which shares no binding, class or route with it.
// docs/decisions/0028-one-sqlite-durable-object-per-hosted-tenant.md.
//
// It serves the operator's routes behind Access, each tenant's pages to that tenant's signed-in
// readers, and the sign-in that mails them a link (hosted_router.js, hosted_site.js,
// hosted_login.js, ADR-0033), and runs the hourly tick for every tenant: each tenant's RSS poll
// into its own buffer (tenant_buffer.js), then its run, queued in the registry, when one is due
// by the registry's schedule (run_queue.js). Its email() handler takes each tenant's newsletters
// at the tenant's random address into the tenant's own object (hosted_inbound.js). A run's alerts
// reach the operator through OperatorAlertGateway (hosted_alerts.js), and its AI spend reaches the
// registry through SpendGateway (hosted_spend.js). The tenant list is the operator's registry
// (tenant_registry.js).
import { env } from "cloudflare:workers";
import HOSTED_SCHEMA_SQL from "../../../src/cyris/adapters/store/hosted_schema.sql";
import SCHEMA_SQL from "../../../src/cyris/adapters/store/schema.sql";
import FEED_CATALOG from "../../../src/cyris/onboarding_feeds.json";
import STARTING_VALUES from "../../../src/cyris/settings_starting_values.json";
import { accessKeys } from "./access_keys.js";
import { HOSTED_DOMAIN } from "./hosted_container.js";
import { createSignIn } from "./hosted_login.js";
import SEED from "./hosted_seed.json";
import { receiveInbound } from "./hosted_inbound.js";
import { handleHostedRequest } from "./hosted_router.js";
import { handleTenantSettings } from "./hosted_settings.js";
import { createSitePage } from "./hosted_site.js";
import { addNewsletterSource, seedTenant, tenantReport, writeSchedule } from "./hosted_seed.js";
import { registrySchedule } from "./run_queue.js";
import { planTick } from "./schedule.js";
import { answerQuery, tenantDb, tenantRows } from "./tenant_db.js";
import { tenantRegistry } from "./tenant_registry.js";
import { ACTIVE } from "./tenant_status.js";

export { OperatorAlertGateway } from "./hosted_alerts.js";
export { TenantContainer } from "./hosted_container.js";
export { SpendGateway } from "./hosted_spend.js";
export { TenantDb, TenantDbGateway } from "./tenant_db.js";
export { TenantRegistry } from "./tenant_registry.js";

// The registry's schedule copied into the tenant's own settings (hosted_seed.js), when it has one.
async function copySchedule(tenant) {
  const schedule = registrySchedule(await tenantRegistry(env).tenant(tenant));
  return schedule ? writeSchedule({ db: tenantDb(env, tenant), schedule }) : false;
}

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
// failed; `queued`, the runs it queued; `object_calls`, the Durable Object calls the tick made itself (the registry, each
// tenant's poll, its digest_runs read and its queued run), which are the tick's own subrequests if
// Cloudflare counts them as such; `feed_fetches`, the feed requests the tenants' polls made, each
// in that tenant's object's invocation; and its wall time.
function tickMeasure(tick, tenants) {
  const polled = tick.polls.filter((poll) => !poll.error);
  const sum = (key) => polled.reduce((total, poll) => total + poll[key], 0);
  return {
    tenants: tenants.length,
    polled: polled.length,
    failed_polls: tick.polls.length - polled.length,
    queued: tick.queued,
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
  async fetch(request, _env, ctx) {
    const registry = () => tenantRegistry(env);
    // The sign-in mail goes through the Email Service binding, from the operator's sender, with
    // links on the hosted domain: never the request's own Host.
    const signIn = createSignIn({
      issue: (email, codeHash, options) => registry().issueSignIn(email, codeHash, options),
      mail: env.MAIL,
      from: env.CYRIS_HOSTED_MAIL_FROM,
      domain: env[HOSTED_DOMAIN],
    });
    return handleHostedRequest(request, env, {
      accessKeys: accessKeys(env.CYRIS_ACCESS_TEAM_DOMAIN),
      register: (tenant, status) => tenantRegistry(env).register(tenant, { status }),
      tenant: (tenant) => tenantRegistry(env).tenant(tenant),
      // The seed writes the stub's schedule, and then the registry's over it when it has one.
      seed: async (tenant) => {
        const seeded = await seedTenant({
          db: tenantDb(env, tenant),
          tenant,
          schemaSql: SCHEMA_SQL,
          hostedSchemaSql: HOSTED_SCHEMA_SQL,
          startingValues: STARTING_VALUES,
          seed: SEED,
          catalog: FEED_CATALOG,
        });
        await copySchedule(tenant);
        return seeded;
      },
      schedule: async (tenant, schedule) => {
        const answer = await tenantRegistry(env).setSchedule(tenant, schedule);
        if (answer.refused) return answer;
        return { tenant: answer.tenant, settings: await copySchedule(tenant) };
      },
      report: async (tenant) => ({
        ...(await tenantReport({ db: tenantDb(env, tenant), tenant })),
        runs: await tenantRegistry(env).runs(tenant),
        spend: await tenantRegistry(env).budget(tenant),
      }),
      setBudget: (tenant, usd) => registry().setBudget(tenant, usd),
      spend: (month) => registry().spend({ month }),
      query: (tenant, request) => answerQuery(tenantDb(env, tenant), request),
      queueRun: (tenant, issue) => tenantRegistry(env).queueRun(tenant, issue),
      poll: (tenant) => pollTenant(tenant),
      inboundAddress: (tenant) => tenantRegistry(env).inboundAddress(tenant),
      rotateAddress: (tenant) => tenantRegistry(env).rotateInboundAddress(tenant),
      revokeAddress: (tenant) => tenantRegistry(env).revokeInboundAddress(tenant),
      addNewsletterSource: (tenant, params) => addNewsletterSource({ db: tenantDb(env, tenant), params }),
      page: sitePage,
      // The schedule a reader saves goes through the registry, as the operator's does, never as a
      // settings row of the tenant's; it names no run cap, so the operator's is kept.
      settings: (request, tenant, rest) =>
        handleTenantSettings(request, {
          rest,
          db: tenantDb(env, tenant),
          registryRow: () => registry().tenant(tenant),
          setSchedule: async (schedule) => {
            const answer = await registry().setSchedule(tenant, schedule);
            if (!answer.refused) await copySchedule(tenant);
            return answer;
          },
          ownHosts: [env[HOSTED_DOMAIN]].filter(Boolean),
        }),
      invite: (tenant, email) => registry().invite(tenant, email),
      revoke: (tenant, email) => registry().revoke(tenant, email),
      members: (tenant) => registry().members(tenant),
      signIn,
      session: (sessionHash) => registry().session(sessionHash),
      voteToken: (sessionHash) => registry().voteToken(sessionHash),
      voteSession: (token) => registry().voteSession(token),
      recordVote: (tenant, vote) => tenantDb(env, tenant).recordVote(vote),
      login: {
        signIn,
        waitUntil: (promise) => ctx.waitUntil(promise),
        redeem: (codeHash, sessionHash) => registry().redeemSignIn(codeHash, sessionHash),
        endSession: (sessionHash) => registry().endSession(sessionHash),
      },
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
  // the registry's schedule whether this is a digest hour, or the tenant's own digest_runs
  // whether a digest hour has none to catch up, and queues that issue's run in the registry,
  // which starts it as a container is free (run_queue.js); a tenant of any other status is
  // neither polled nor ticked, and one with no schedule in the registry is polled only. The poll
  // comes first so that a run this tick queues reads this hour's entries: they are stamped when
  // the poll starts, before the run's window closes. A poll that fails is logged, and its
  // tenant's run is still queued on what its buffer holds. Off a digest hour, a tenant whose
  // digest_runs cannot be read gets no catch-up. One tenant's failure is logged and leaves the
  // others' polls and ticks alone.
  async scheduled() {
    const tick = { started: Date.now(), calls: 0, polls: [], queued: 0 };
    const call = (what) => {
      tick.calls += 1;
      return what;
    };
    const registry = tenantRegistry(env);
    const rows = await call(registry.tenants({ status: ACTIVE }));
    const tenants = rows.map((row) => row.id);
    const ticks = await Promise.allSettled(
      rows.map(async (row) => {
        const tenant = row.id;
        tick.polls.push(await pollTenant(tenant, call));
        const schedule = registrySchedule(row);
        if (!schedule) {
          console.log("no schedule in the registry; nothing queued", { tenant });
          return;
        }
        const read = tenantRows(env, tenant);
        const plan = await planTick({ env, schedule, nameDue: true, read: (...args) => call(read(...args)), unreadable: null });
        if (!plan) return;
        const { period, date, origin = "catch-up" } = plan;
        if (origin === "catch-up") console.log("catching up a digest hour with no ok run", { tenant, period, date });
        const answer = await call(registry.queueRun(tenant, { period, date, origin }));
        if (answer.refused) console.log("run not queued", { tenant, period, date, origin, reason: answer.refused });
        else tick.queued += 1;
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
