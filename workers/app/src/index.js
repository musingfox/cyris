// The Worker in front of the cyris Container: the hourly tick, and the only
// door to /settings.
//
// Auth is one layer always — the CYRIS_UI_TOKEN cookie, checked in router.js
// before anything reaches the container. Cloudflare Access is an optional
// second layer on the hostname named by CYRIS_UI_ACCESS_HOST (grade B).
import { Container, getContainer } from "@cloudflare/containers";
import { env } from "cloudflare:workers";
import { handleRequest } from "./router.js";
import { createTypeScale } from "./type_scale.js";

// Every secret the pipeline reads from the environment. The provider is a
// runtime setting in D1, so all three LLM keys ride along — passing only the
// configured one would make switching provider on /settings a redeploy.
const SECRETS = {
  CLOUDFLARE_ACCOUNT_ID: env.CLOUDFLARE_ACCOUNT_ID,
  CLOUDFLARE_API_TOKEN: env.CLOUDFLARE_API_TOKEN,
  CLOUDFLARE_EMBEDDING_API_TOKEN: env.CLOUDFLARE_EMBEDDING_API_TOKEN,
  CYRIS_WORKER_TOKEN: env.CYRIS_WORKER_TOKEN,
  // Separate because it is not a secret: it is rendered into every published
  // digest page so the vote buttons work in the reader's browser.
  CYRIS_PROMOTE_TOKEN: env.CYRIS_PROMOTE_TOKEN,
  ANTHROPIC_API_KEY: env.ANTHROPIC_API_KEY,
  GEMINI_API_KEY: env.GEMINI_API_KEY,
  OPENAI_API_KEY: env.OPENAI_API_KEY,
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

export class CyrisContainer extends Container {
  defaultPort = 8766;
  // Idle time is billed. §7 called the default 10 minutes ~10 container-hours
  // per 60 runs, which is why the hook below exists at all.
  sleepAfter = "5m";

  // Without a role the image runs the Mac mini's supercronic loop, which in the
  // cloud would be a second scheduler racing the Workers Cron above.
  envVars = containerEnv("ui");

  // Overriding this hook without stopping renews the timer instead of sleeping,
  // and the instance bills on. This is the whole point of the override.
  //
  // stop() is only a SIGTERM. The image's PID 1 has to act on it — Linux drops
  // signals PID 1 has no handler for, which is how this hook ran every five
  // minutes for a day without ever putting the instance to sleep.
  async onActivityExpired() {
    await this.stop();
  }

  onStop({ exitCode, reason }) {
    console.log("container stopped", { exitCode, reason });
  }

  onError(error) {
    console.log("container error:", error);
  }
}

// Module scope, so one reader per isolate: its memo is what bounds the REST calls.
const typeScale = createTypeScale({ env, fetchImpl: (input, init) => fetch(input, init) });

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
// schedule check, so a manual run works outside the two digest hours.
async function startRun(period) {
  const envVars = containerEnv("run");
  if (period) envVars.CYRIS_RUN_PERIOD = period;
  await getContainer(env.CYRIS, "run").start({ envVars });
  return { started: "run", at: new Date().toISOString() };
}

export default {
  async fetch(request) {
    return handleRequest(request, env, {
      container: (r) => getContainer(env.CYRIS, "ui").fetch(r),
      startRun,
      probeGemini,
      fetchImpl: (input, init) => fetch(input, init),
      typeScale,
      injectStyle,
    });
  },

  async scheduled() {
    await startRun();
  },
};
