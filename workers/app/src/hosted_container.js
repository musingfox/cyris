// A hosted tenant's run container: one Durable Object per tenant, named by the tenant's id,
// whose container reaches that tenant's database and nothing else.
// docs/decisions/0028-one-sqlite-durable-object-per-hosted-tenant.md.

import { DurableObject } from "cloudflare:workers";
import { Lifecycle } from "./container.js";
import { DB_HOST, TENANT } from "./tenant_db.js";

// As index.js's run instance: the run exits when its pass ends, so this only caps a hung one.
const RUN_INACTIVITY_TIMEOUT_MS = 900_000;

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

// The environment a tenant's run container starts with. The tenant id comes from the object's
// own name, and publishing is off whatever the image defaults to: until per-tenant serving
// lands, a hosted issue stays in the tenant's `digests` (bootstrap refuses to publish too).
export function hostedContainerEnv(env, tenant) {
  const secrets = Object.fromEntries(
    HOSTED_SECRETS.filter((name) => env[name] != null && env[name] !== "").map((name) => [name, env[name]]),
  );
  return {
    ...secrets,
    CYRIS_ROLE: "run",
    CYRIS_STORE_BACKEND: "d1",
    CYRIS_STORE_TENANT_ID: tenant,
    CYRIS_HTML_OUTPUT_ENABLED: "false",
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
      // The gateway answers only this tenant; registered before every start().
      intercept: () => {
        const gateway = ctx.exports.TenantDbGateway({ props: { tenant: this.tenant } });
        return ctx.container.interceptOutboundHttp(DB_HOST, gateway);
      },
      startRetry: START_RETRY,
    });
    if (ctx.container.running) void ctx.blockConcurrencyWhile(() => this.lifecycle.adopt());
  }

  // One run of this tenant's digest: `period` names it, as POST /run?period= does, and `date`,
  // checked by the caller, names the issue's date. Neither names a period lets --if-due decide.
  async startRun({ period, date } = {}) {
    if (!TENANT.test(this.tenant ?? "")) throw new Error(`not a tenant's run container: ${this.tenant}`);
    const envVars = hostedContainerEnv(this.env, this.tenant);
    if (period) envVars.CYRIS_RUN_PERIOD = period;
    if (date) envVars.CYRIS_RUN_DATE = date;
    return { started: await this.lifecycle.start(envVars), tenant: this.tenant };
  }
}
