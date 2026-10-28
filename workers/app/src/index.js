// The Worker in front of the cyris Container: the hourly tick, and the only
// door to /settings.
//
// Auth is Cloudflare Access alone: router.js checks the Access JWT before
// anything reaches the container. The team domain and AUD are grade-B settings.
import { DurableObject, env } from "cloudflare:workers";
import { accessKeys } from "./access_keys.js";
import { Lifecycle } from "./container.js";
import { handleRequest } from "./router.js";
import { planTick, readTimeZone } from "./schedule.js";
import { createTypeScale } from "./type_scale.js";
import { createTheme } from "./theme.js";

// Every secret the pipeline reads from the environment. The provider is a
// runtime setting in D1, so every LLM key rides along — passing only the
// configured one would make switching provider on /settings a redeploy.
const SECRETS = {
  CLOUDFLARE_ACCOUNT_ID: env.CLOUDFLARE_ACCOUNT_ID,
  CLOUDFLARE_API_TOKEN: env.CLOUDFLARE_API_TOKEN,
  CLOUDFLARE_EMBEDDING_API_TOKEN: env.CLOUDFLARE_EMBEDDING_API_TOKEN,
  CYRIS_WORKER_TOKEN: env.CYRIS_WORKER_TOKEN,
  // Separate from CYRIS_WORKER_TOKEN: older published pages carry it in their
  // vote buttons, so rotating it would break those buttons.
  CYRIS_PROMOTE_TOKEN: env.CYRIS_PROMOTE_TOKEN,
  ANTHROPIC_API_KEY: env.ANTHROPIC_API_KEY,
  GEMINI_API_KEY: env.GEMINI_API_KEY,
  OPENAI_API_KEY: env.OPENAI_API_KEY,
  OPENAI_COMPATIBLE_API_KEY: env.OPENAI_COMPATIBLE_API_KEY,
  OPENAI_COMPATIBLE_EMBEDDING_API_KEY: env.OPENAI_COMPATIBLE_EMBEDDING_API_KEY,
  OPENAI_COMPATIBLE_BASE_URL: env.OPENAI_COMPATIBLE_BASE_URL,
  OPENAI_COMPATIBLE_MODEL: env.OPENAI_COMPATIBLE_MODEL,
  OPENAI_COMPATIBLE_EMBEDDING_BASE_URL: env.OPENAI_COMPATIBLE_EMBEDDING_BASE_URL,
  OPENAI_COMPATIBLE_EMBEDDING_MODEL: env.OPENAI_COMPATIBLE_EMBEDDING_MODEL,
  CLOUDFLARE_AI_TOKEN: env.CLOUDFLARE_AI_TOKEN,
  CLOUDFLARE_AIG_TOKEN: env.CLOUDFLARE_AIG_TOKEN,
};

// Grade-B deployment identity. The deployer sets these as Worker secrets or
// [vars]; this list carries no defaults and no account-specific values.
const DEPLOYMENT = {
  CYRIS_STORE_BACKEND: env.CYRIS_STORE_BACKEND,
  CYRIS_STORE_DATABASE_ID: env.CYRIS_STORE_DATABASE_ID,
  CYRIS_HTML_OUTPUT_ENABLED: env.CYRIS_HTML_OUTPUT_ENABLED,
  CYRIS_PROMOTE_PUBLISH_ENABLED: env.CYRIS_PROMOTE_PUBLISH_ENABLED,
  CYRIS_PROMOTE_PAGES_PROJECT: env.CYRIS_PROMOTE_PAGES_PROJECT,
  CYRIS_PROMOTE_CUSTOM_DOMAIN: env.CYRIS_PROMOTE_CUSTOM_DOMAIN,
  CYRIS_PROMOTE_WORKER_URL: env.CYRIS_PROMOTE_WORKER_URL,
  CYRIS_NEWSLETTER_WORKER_URL: env.CYRIS_NEWSLETTER_WORKER_URL,
  CYRIS_RSS_WORKER_URL: env.CYRIS_RSS_WORKER_URL,
  CYRIS_STORE_SQLITE_PATH: env.CYRIS_STORE_SQLITE_PATH,
  CYRIS_PROMOTE_LOCAL_ORIGIN: env.CYRIS_PROMOTE_LOCAL_ORIGIN,
  CYRIS_RSS_LOCAL_BUFFER: env.CYRIS_RSS_LOCAL_BUFFER,
  // From [vars] in wrangler.toml, not from the deployer.
  CYRIS_APP_WORKER_NAME: env.CYRIS_APP_WORKER_NAME,
};

// Unset or empty Worker bindings become JS undefined/"". Spreading those into
// envVars would hand the container the string "undefined". Drop them instead.
const present = (vars) =>
  Object.fromEntries(Object.entries(vars).filter(([, v]) => v != null && v !== ""));

const containerEnv = (role) => ({ ...present(SECRETS), ...present(DEPLOYMENT), CYRIS_ROLE: role });

const GEMINI_GENERATE_URL = "https://generativelanguage.googleapis.com/v1beta/models";

async function probeGemini(model) {
  const started = Date.now();
  if (!env.GEMINI_API_KEY) {
    return {
      status: 503,
      body: {
        ok: false,
        provider: "gemini",
        model,
        latency_ms: Date.now() - started,
        error: { code: 503, status: "UNAVAILABLE", message: "GEMINI_API_KEY is not configured" },
      },
    };
  }

  try {
    const response = await fetch(`${GEMINI_GENERATE_URL}/${encodeURIComponent(model)}:generateContent`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-goog-api-key": env.GEMINI_API_KEY,
      },
      body: JSON.stringify({
        contents: [{ parts: [{ text: "ping" }] }],
        generationConfig: { maxOutputTokens: 128 },
      }),
    });
    const latency_ms = Date.now() - started;
    if (response.ok) {
      return { status: 200, body: { ok: true, provider: "gemini", model, latency_ms } };
    }
    const payload = await response.json().catch(() => ({}));
    const error = payload.error ?? {};
    return {
      status: response.status,
      body: {
        ok: false,
        provider: "gemini",
        model,
        latency_ms,
        error: {
          code: error.code ?? response.status,
          status: error.status ?? "UNKNOWN",
          message: String(error.message ?? `Gemini returned HTTP ${response.status}`).slice(0, 300),
        },
      },
    };
  } catch (error) {
    return {
      status: 502,
      body: {
        ok: false,
        provider: "gemini",
        model,
        latency_ms: Date.now() - started,
        error: { code: 502, status: "UNAVAILABLE", message: String(error).slice(0, 300) },
      },
    };
  }
}

// The run instance exits on its own when its pass ends, so its timeout is only
// a cap on a hung run. 5m is too short for that: the slowest run in digest_runs
// took 196s before promote-sync, and the run role's PID 1 acts on SIGTERM
// (docker/entrypoint.sh), so a stop ends the pass it lands on.
const RUN_INACTIVITY_TIMEOUT_MS = 900_000;
// Idle time is billed. The Container class's 10-minute default idle added ~10
// container-hours per 60 runs.
const UI_INACTIVITY_TIMEOUT_MS = 300_000;
const RUN_INSTANCE = "run";

export class CyrisContainer extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    // Read from the instance's own name, not from the start call, so a Durable
    // Object restart mid-run keeps the run's timeout.
    this.lifecycle = new Lifecycle(
      ctx,
      ctx.id.name === RUN_INSTANCE ? RUN_INACTIVITY_TIMEOUT_MS : UI_INACTIVITY_TIMEOUT_MS,
    );
    if (ctx.container.running) void ctx.blockConcurrencyWhile(() => this.lifecycle.adopt());
  }

  // Without a role the image runs the local machine's supercronic loop, which in the
  // cloud would be a second scheduler racing the Workers Cron above.
  async fetch(request) {
    try {
      await this.lifecycle.ready(containerEnv("ui"));
    } catch (error) {
      console.log("container error:", error);
      return new Response(`Failed to start container: ${error}`, { status: 503 });
    }
    return this.lifecycle.forward(request);
  }

  async startRun(envVars) {
    await this.lifecycle.start(envVars);
  }
}

// Module scope, so one reader per isolate: its memo is what bounds the REST calls.
const typeScale = createTypeScale({ env, fetchImpl: (input, init) => fetch(input, init) });
// The theme rides the same memo shape: one reader per isolate, one rest call per TTL.
const theme = createTheme({ env, fetchImpl: (input, init) => fetch(input, init) });

const injectStyle = (response, html) =>
  new HTMLRewriter()
    .on("head", {
      element(head) {
        head.append(html, { html: true });
      },
    })
    .transform(response);

// A separate instance from the UI: this one runs the pipeline once and exits,
// so it stops billing without waiting for a sleep timer. A period skips the
// schedule check, so a manual run works outside the two digest hours; a date,
// which the router allows only beside a period, names the issue's date.
async function startRun(period, date) {
  const envVars = containerEnv("run");
  if (period) envVars.CYRIS_RUN_PERIOD = period;
  if (date) envVars.CYRIS_RUN_DATE = date;
  await env.CYRIS.getByName(RUN_INSTANCE).startRun(envVars);
  return { started: "run", at: new Date().toISOString() };
}

export default {
  async fetch(request) {
    return handleRequest(request, env, {
      container: (r) => env.CYRIS.getByName("ui").fetch(r),
      startRun,
      runTimeZone: () => readTimeZone({ env, fetchImpl: (input, init) => fetch(input, init) }),
      probeGemini,
      fetchImpl: (input, init) => fetch(input, init),
      accessKeys: accessKeys(env.CYRIS_ACCESS_TEAM_DOMAIN),
      typeScale,
      theme,
      injectStyle,
    });
  },

  // Hourly, and 22 of every 24 ticks are not a digest hour: asking first keeps
  // the container asleep through those instead of waking it to ask itself. The
  // two ticks after a digest hour whose run never finished catch that issue up.
  async scheduled() {
    const plan = await planTick({ env, fetchImpl: (input, init) => fetch(input, init) });
    if (!plan) {
      console.log("not a digest hour, nor one to catch up; the run container stays asleep");
      return;
    }
    if (plan.period) {
      console.log("catching up a digest hour with no ok run", { period: plan.period, date: plan.date });
    }
    await startRun(plan.period, plan.date);
  },
};
