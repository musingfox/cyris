// A hosted tenant's run container: one Durable Object per tenant, named by the tenant's id,
// whose container reaches that tenant's database and nothing else.
// docs/decisions/0028-one-sqlite-durable-object-per-hosted-tenant.md.

import { DurableObject } from "cloudflare:workers";
import { Lifecycle, NO_INSTANCE } from "./container.js";
import { OPS_HOST, alertOperator, workerAlert } from "./hosted_alerts.js";
import { SPEND_HOST } from "./hosted_spend.js";
import { DB_HOST, TENANT } from "./tenant_db.js";
import { tenantRegistry } from "./tenant_registry.js";
import { ACTIVE } from "./tenant_status.js";

// As index.js's run instance: the run exits when its pass ends, so this only caps a hung one.
// The run queue's lease (`RUN_LEASE_MS` in run_queue.js) outlasts it.
export const RUN_INACTIVITY_TIMEOUT_MS = 900_000;

// Tenants share the container application's max_instances, and a start made while every
// instance is held is refused for about 10 s after a destroy() (ADR-0028). The backoff is the
// probe's; 90 s outlasts that release several times over and keeps one tenant's failed start
// from holding a manual run's request for minutes.
export const START_RETRY = { maxWaitMs: 90_000, backoffMs: [1_000, 2_000, 4_000, 8_000, 10_000] };

// What a hosted run may use: the model providers' keys and the account they run on. Not the
// D1 and Pages token, the Worker bearers or any deployment identity of the default Worker's
// (`SECRETS` and `DEPLOYMENT` in index.js): a hosted tenant has no D1, no Pages project, and
// none of the rss, promote or newsletter Worker queues, which belong to another deployment
// and which it would drain. tests/test_hosted_config.py holds this list inside `SECRETS`.
const HOSTED_SECRETS = [
  "CLOUDFLARE_ACCOUNT_ID",
  "CLOUDFLARE_EMBEDDING_API_TOKEN",
  "ANTHROPIC_API_KEY",
  "GEMINI_API_KEY",
  "OPENAI_API_KEY",
  "CLOUDFLARE_AI_TOKEN",
  "CLOUDFLARE_AIG_TOKEN",
];

// The names of HOSTED_SECRETS the operator set, which every run container is handed; the shared
// settings page names a provider's key ready only when its are among them.
export const handedSecrets = (env) => HOSTED_SECRETS.filter((name) => env[name] != null && env[name] !== "");

// The hosted Worker's hostname, which a tenant's links point at (ADR-0029). The operator sets it
// once (`scripts/hosted.sh secrets`), and every container gets it under the same name.
export const HOSTED_DOMAIN = "CYRIS_STORE_HOSTED_DOMAIN";

// The run object's storage key for the alert kind a run reported its end with (ADR-0035): set by
// OperatorAlertGateway, cleared before every start, read when the container stops abnormally.
export const REPORTED = "reported";

// Its key for the alert the object sent itself about a start's abnormal stop: the run that start
// ran, set once the alert is done, cleared before every start. With REPORTED, what `endTold`
// answers the run queue, whose lease end would otherwise tell the operator again of a stop whose
// report never reached it. It names its run because a stop's alert can end after the next start.
export const TOLD = "told";

// Its key for the run of the registry's queue this start runs, `{ id, attempt }` (`startRun`): set
// before every start, read by a restarted object that adopts the container. A stop is reported
// with the run its start ran, so a report heard late ends that run and never a later one.
export const RUN = "run";

// Whether `a` and `b` name the same run of the queue, and the same start of it.
const sameRun = (a, b) => Boolean(a && b && a.id === b.id && a.attempt === b.attempt);

// The environment a tenant's run container starts with. The tenant id comes from the object's
// own name, and `members` are the addresses on its invite list, the reader's own: mail the
// tenant's address gets from one is a forward, read for the sender it quotes (ADR-0050). Pages
// publishing is off whatever the image defaults to: a hosted run stores its pages in the tenant's
// own object (bootstrap wires that whatever this says). Without the hosted domain a run would have
// no link to give, so it is not started.
export function hostedContainerEnv(env, tenant, members = []) {
  if (!env[HOSTED_DOMAIN]) throw new Error(`${HOSTED_DOMAIN} is not set; run: scripts/hosted.sh secrets`);
  const secrets = Object.fromEntries(handedSecrets(env).map((name) => [name, env[name]]));
  return {
    ...secrets,
    CYRIS_ROLE: "run",
    CYRIS_STORE_BACKEND: "d1",
    CYRIS_STORE_TENANT_ID: tenant,
    CYRIS_STORE_MEMBER_ADDRESSES: members.join(","),
    [HOSTED_DOMAIN]: env[HOSTED_DOMAIN],
    CYRIS_PROMOTE_PUBLISH_ENABLED: "false",
  };
}

export class TenantContainer extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    // The object's name is the tenant (hosted.js addresses it as TENANT_RUN.getByName(tenant)),
    // so nothing a caller passes can point this container at another tenant's database.
    this.tenant = ctx.id.name;
    this.lifecycle = new Lifecycle(ctx, RUN_INACTIVITY_TIMEOUT_MS, {
      // Every gateway answers only this tenant; registered before every start(). The second
      // carries the run's alerts to the operator (hosted_alerts.js), the third its AI allowance
      // and spend to the registry (hosted_spend.js). A new start has reported nothing yet, so the
      // last one's record goes first.
      intercept: async (run) => {
        await ctx.storage.delete(REPORTED);
        await ctx.storage.delete(TOLD);
        if (run) await ctx.storage.put(RUN, run);
        else await ctx.storage.delete(RUN);
        const props = { tenant: this.tenant };
        await ctx.container.interceptOutboundHttp(DB_HOST, ctx.exports.TenantDbGateway({ props }));
        await ctx.container.interceptOutboundHttp(OPS_HOST, ctx.exports.OperatorAlertGateway({ props }));
        await ctx.container.interceptOutboundHttp(SPEND_HOST, ctx.exports.SpendGateway({ props }));
      },
      startRetry: START_RETRY,
      // The run queue's slot is held until the registry hears the container stop (run_queue.js):
      // a start that looked granted and then stopped with NO_INSTANCE goes back in the queue. The
      // report names the run its start ran, which alone it can end.
      onStop: (stopped, run) => tenantRegistry(this.env).runEnded(this.tenant, { ...stopped, run }),
      // Every tenant's container lines share one stream, as its run's own lines do.
      logFields: { tenant: this.tenant },
      // Before onStop (container.js): the stop report can free this tenant for its next queued
      // start, whose intercept clears the record this reads. Its alert is bounded by
      // ALERT_TIMEOUT_MS, so the report waits on the webhook no longer than that.
      onAbnormalStop: (error, run) => this.#stoppedUnreported(error, run),
    });
    if (ctx.container.running) {
      void ctx.blockConcurrencyWhile(async () => this.lifecycle.adopt(await ctx.storage.get(RUN)));
    }
  }

  // One run of this tenant's digest: `period` names it, as POST /run?period= does, and `date`,
  // checked by the caller, names the issue's date. The registry's run queue, the one caller,
  // names both, so a run that waited past its hour still makes its issue (ADR-0032), and `run`,
  // `{ id, attempt }`, the row and start this is, which its stop report names.
  // Only an active tenant of the registry is started, whoever asks: the queue asks the registry
  // first, and this asks again so no other path can start an unregistered one.
  // A start that throws is rethrown, and is the run queue's to tell: the queue holds every start it
  // makes, puts a no-instance start back in the queue and tells the operator once it ends the run
  // (run_queue.js), so the 90 s give-up of a start it will try again alerts nobody. This object
  // tells the operator itself only when the registry cannot say it holds the start, as when it
  // cannot be read, since then nothing else will. A tenant the registry does not hold as active is
  // a refusal, not a failed run, and alerts nobody.
  async startRun({ period, date, run } = {}) {
    if (!TENANT.test(this.tenant ?? "")) throw new Error(`not a tenant's run container: ${this.tenant}`);
    let registered;
    let members;
    try {
      registered = await tenantRegistry(this.env).tenant(this.tenant);
      members = (await tenantRegistry(this.env).members(this.tenant)).map((member) => member.email);
    } catch (error) {
      await this.#couldNotStart(error, period, date);
      throw error;
    }
    if (registered?.status !== ACTIVE) {
      const status = registered?.status ?? "unregistered";
      throw new Error(`tenant ${this.tenant} is not an active registered tenant: ${status}`);
    }
    try {
      const envVars = hostedContainerEnv(this.env, this.tenant, members);
      if (period) envVars.CYRIS_RUN_PERIOD = period;
      if (date) envVars.CYRIS_RUN_DATE = date;
      const named = run ? { id: run.id, attempt: run.attempt } : undefined;
      return { started: await this.lifecycle.start(envVars, named), tenant: this.tenant };
    } catch (error) {
      await this.#couldNotStart(error, period, date);
      throw error;
    }
  }

  // RPC from OperatorAlertGateway: this start's run sent an alert that ends it (ENDING_KINDS).
  async alertReported(kind) {
    await this.ctx.storage.put(REPORTED, kind);
  }

  // RPC from the registry, when a held run's lease runs out with no stop report: whether the
  // operator has heard how `run`'s start, `{ id, attempt }`, ended, from the run's own alert while
  // that start is still this object's last, or from the object's alert about that start's stop.
  async endTold(run) {
    const last = sameRun(await this.ctx.storage.get(RUN), run);
    if (last && (await this.ctx.storage.get(REPORTED))) return true;
    return sameRun(await this.ctx.storage.get(TOLD), run);
  }

  async #couldNotStart(error, period, date) {
    if (await this.#queueHolds()) return;
    const run = [period && `period=${period}`, date && `date=${date}`].filter(Boolean).join(" ");
    const detail = `${run ? `${run}\n` : ""}${String(error?.message ?? error)}`;
    await alertOperator(this.env, this.tenant, workerAlert("start_failed", detail));
  }

  // Whether the registry's run queue holds this tenant's start, and so tells the operator how it
  // ends; false when the registry cannot say, so the operator is told here rather than not at all.
  async #queueHolds() {
    try {
      return Boolean(await tenantRegistry(this.env).heldRun(this.tenant));
    } catch {
      return false;
    }
  }

  // A container that exited non-zero or was killed (OOM, SIGKILL, the inactivity cap's SIGTERM)
  // tells the operator, unless its run already sent an alert that ends it: that alert said why,
  // and this one would only repeat it. A stop with no container instance, of a run the queue
  // holds, is the queue's: its stop report puts the run back in the queue. Never throws: it runs
  // in the monitor's waitUntil. `run` is the run the stopped start ran, which TOLD names.
  async #stoppedUnreported(error, run) {
    try {
      const reported = await this.ctx.storage.get(REPORTED);
      if (reported) return;
      if (NO_INSTANCE.test(String(error)) && (await this.#queueHolds())) return;
      const detail = `exitCode=${error?.exitCode ?? "unknown"} ${String(error?.message ?? error)}`;
      await alertOperator(this.env, this.tenant, workerAlert("stopped", detail));
      if (run) await this.ctx.storage.put(TOLD, run);
    } catch (failure) {
      console.log("operator alert not sent", { tenant: this.tenant, reason: String(failure?.message ?? failure) });
    }
  }
}
