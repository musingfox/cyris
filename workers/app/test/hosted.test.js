import { afterEach, describe, it, expect, vi } from "vitest";
import { generateKeyPair, exportJWK, SignJWT, createLocalJWKSet } from "jose";
import FEED_CATALOG from "../../../src/cyris/onboarding_feeds.json" with { type: "json" };
import STARTING_VALUES from "../../../src/cyris/settings_starting_values.json" with { type: "json" };
import SEED from "../src/hosted_seed.json" with { type: "json" };
import { ALERT_TIMEOUT_MS, OPERATOR_WEBHOOK, OPS_HOST } from "../src/hosted_alerts.js";
import { SPEND_HOST } from "../src/hosted_spend.js";
import { REPORTED, RUN, START_RETRY, TOLD, TenantContainer, hostedContainerEnv } from "../src/hosted_container.js";
import { handleHostedRequest } from "../src/hosted_router.js";
import { backupHours } from "../src/hosted_backup.js";
import { seedSchedule, seedSettings, seedTenant, spreadHour, tenantReport } from "../src/hosted_seed.js";
import { DISPATCH_EVERY_MS, MAX_START_ATTEMPTS, RUN_LEASE_MS } from "../src/run_queue.js";
import { DB_HOST } from "../src/tenant_db.js";
import { REGISTRY_NAME, tenantRegistry } from "../src/tenant_registry.js";
import { HOSTED_SCHEMA_SQL, SCHEMA_SQL, drainAlarms, namespace, registryNamespace } from "./sql_storage.js";

// The schedule the seed writes a tenant the registry has none for: one hour, in UTC.
const SEEDED_SCHEDULE = { times: ["08:00"], timeZone: "UTC" };

// Everything the default Worker forwards (index.js `SECRETS` and `DEPLOYMENT`), set.
const EVERY_BINDING = {
  CLOUDFLARE_ACCOUNT_ID: "acct",
  CLOUDFLARE_API_TOKEN: "d1-and-pages-token",
  CLOUDFLARE_EMBEDDING_API_TOKEN: "embed",
  CYRIS_WORKER_TOKEN: "worker-bearer",
  CYRIS_PROMOTE_TOKEN: "promote-bearer",
  ANTHROPIC_API_KEY: "anthropic",
  GEMINI_API_KEY: "gemini",
  OPENAI_API_KEY: "",
  CLOUDFLARE_AI_TOKEN: "ai",
  CLOUDFLARE_AIG_TOKEN: undefined,
  CYRIS_STORE_BACKEND: "d1",
  CYRIS_STORE_DATABASE_ID: "production-d1",
  CYRIS_HTML_OUTPUT_ENABLED: "true",
  CYRIS_PROMOTE_PUBLISH_ENABLED: "true",
  CYRIS_PROMOTE_PAGES_PROJECT: "production-pages",
  CYRIS_PROMOTE_CUSTOM_DOMAIN: "digest.example",
  CYRIS_PROMOTE_WORKER_URL: "https://cyris-promote.example.workers.dev",
  CYRIS_NEWSLETTER_WORKER_URL: "https://cyris-newsletter.example.workers.dev",
  CYRIS_RSS_WORKER_URL: "https://cyris-rss.example.workers.dev",
  CYRIS_APP_WORKER_NAME: "cyris-app",
  // The hosted Worker's own hostname, which every tenant's links point at.
  CYRIS_STORE_HOSTED_DOMAIN: "cyris-hosted.example.workers.dev",
};

const WORKER_URLS = ["CYRIS_PROMOTE_WORKER_URL", "CYRIS_NEWSLETTER_WORKER_URL", "CYRIS_RSS_WORKER_URL"];

// A registry that holds `alpha` and `bravo` as active, and `paused` as paused; it starts runs
// through `env`'s TENANT_RUN.
async function registry(env = {}) {
  const REGISTRY = registryNamespace(env);
  for (const tenant of ["alpha", "bravo"]) await tenantRegistry({ REGISTRY }).register(tenant);
  await tenantRegistry({ REGISTRY }).register("paused", { status: "paused" });
  return REGISTRY;
}

// A tenant's run object as the runtime builds it: named by the tenant, its container a fake
// that logs, in order, each interception registered and each start. `settled()` waits for what
// the object handed waitUntil, the monitor's stop handler among it.
async function runObject(name, bindings = EVERY_BINDING) {
  const env = { ...bindings };
  env.REGISTRY = await registry(env);
  const order = [];
  const container = {
    running: false,
    starts: [],
    interceptOutboundHttp: async (host, gateway) => order.push({ intercept: host, props: gateway.props }),
    start(options) {
      order.push("start");
      this.running = true;
      this.starts.push(options);
    },
    setInactivityTimeout: async () => {},
    monitor: () => new Promise(() => {}),
    // A run listens on no port: a granted instance refuses the connection.
    getTcpPort: () => ({
      fetch: async () => {
        throw new Error("connection refused");
      },
    }),
  };
  const ctx = {
    id: { name },
    container,
    exports: {
      TenantDbGateway: ({ props }) => ({ props }),
      OperatorAlertGateway: ({ props }) => ({ props }),
      SpendGateway: ({ props }) => ({ props }),
    },
    waited: [],
    waitUntil(promise) {
      this.waited.push(promise);
    },
    blockConcurrencyWhile: (run) => run(),
    storage: storage(),
  };
  const settled = () => Promise.allSettled(ctx.waited);
  return { object: new TenantContainer(ctx, env), container, order, ctx, env, settled };
}

// A Durable Object's key-value storage, in memory.
function storage() {
  const map = new Map();
  return {
    map,
    get: async (key) => map.get(key),
    put: async (key, value) => void map.set(key, value),
    delete: async (key) => map.delete(key),
  };
}

describe("a hosted tenant's run container", () => {
  it("gets none of the three Worker URLs, nor anything else that reaches past its tenant", () => {
    const started = hostedContainerEnv(EVERY_BINDING, "alpha");

    for (const name of WORKER_URLS) expect(started).not.toHaveProperty(name);
    for (const name of [
      "CYRIS_WORKER_TOKEN",
      "CYRIS_PROMOTE_TOKEN",
      "CLOUDFLARE_API_TOKEN",
      "CYRIS_STORE_DATABASE_ID",
      "CYRIS_PROMOTE_PAGES_PROJECT",
      "CYRIS_PROMOTE_CUSTOM_DOMAIN",
      "CYRIS_APP_WORKER_NAME",
    ]) {
      expect(started).not.toHaveProperty(name);
    }
    expect(started).toEqual({
      CLOUDFLARE_ACCOUNT_ID: "acct",
      CLOUDFLARE_EMBEDDING_API_TOKEN: "embed",
      ANTHROPIC_API_KEY: "anthropic",
      GEMINI_API_KEY: "gemini",
      CLOUDFLARE_AI_TOKEN: "ai",
      CYRIS_ROLE: "run",
      CYRIS_STORE_BACKEND: "d1",
      CYRIS_STORE_TENANT_ID: "alpha",
      CYRIS_STORE_HOSTED_DOMAIN: "cyris-hosted.example.workers.dev",
      CYRIS_PROMOTE_PUBLISH_ENABLED: "false",
    });
  });

  it("never hands a container the operator's webhook, which only the alert gateway reads", () => {
    const env = hostedContainerEnv({ ...EVERY_BINDING, [OPERATOR_WEBHOOK]: "https://discord.com/api/webhooks/1/op" }, "alpha");

    expect(env).not.toHaveProperty(OPERATOR_WEBHOOK);
    expect(JSON.stringify(env)).not.toContain("webhooks");
  });

  it("is not started without the hosted domain its links would point at", async () => {
    const { object, container } = await runObject("alpha", { ...EVERY_BINDING, CYRIS_STORE_HOSTED_DOMAIN: "" });

    await expect(object.startRun({ period: "morning" })).rejects.toThrow("CYRIS_STORE_HOSTED_DOMAIN is not set");
    expect(container.starts).toEqual([]);
  });

  it("registers every gateway for its own tenant before the container starts", async () => {
    const { object, container, order } = await runObject("bravo");

    expect(await object.startRun({ period: "morning", date: "2026-11-18" })).toEqual({
      started: true,
      tenant: "bravo",
    });
    expect(order).toEqual([
      { intercept: DB_HOST, props: { tenant: "bravo" } },
      { intercept: OPS_HOST, props: { tenant: "bravo" } },
      { intercept: SPEND_HOST, props: { tenant: "bravo" } },
      "start",
    ]);
    const [{ env, enableInternet }] = container.starts;
    expect(enableInternet).toBe(true);
    expect(env).toMatchObject({
      CYRIS_STORE_TENANT_ID: "bravo",
      CYRIS_RUN_PERIOD: "morning",
      CYRIS_RUN_DATE: "2026-11-18",
    });
    for (const name of WORKER_URLS) expect(env).not.toHaveProperty(name);
  });

  it("takes its tenant from its own name, so no caller can hand it another", async () => {
    const { object, container } = await runObject("alpha");

    await object.startRun({ period: "evening", CYRIS_STORE_TENANT_ID: "bravo" });

    expect(container.starts[0].env.CYRIS_STORE_TENANT_ID).toBe("alpha");
  });

  it("refuses to start for an object that is not named by a tenant id", async () => {
    for (const name of [undefined, "../alpha", "Alpha"]) {
      const { object, container } = await runObject(name);
      await expect(object.startRun({ period: "morning" })).rejects.toThrow("not a tenant's run container");
      expect(container.starts).toEqual([]);
    }
  });

  it("refuses to start a tenant the registry does not hold as active, before any interception", async () => {
    for (const [name, status] of [
      ["charlie", "unregistered"],
      ["paused", "paused"],
    ]) {
      const { object, container, order } = await runObject(name);
      await expect(object.startRun({ period: "morning" })).rejects.toThrow(
        `tenant ${name} is not an active registered tenant: ${status}`,
      );
      expect(container.starts).toEqual([]);
      expect(order).toEqual([]);
    }
  });

  it("tells the registry when its container stops, which ends the run it held or queues it again", async () => {
    vi.spyOn(console, "log").mockImplementation(() => {});
    const { object, container, ctx, env } = await runObject("alpha");
    env.TENANT_RUN = { getByName: () => object };
    const registry = env.REGISTRY.getByName(REGISTRY_NAME);
    await registry.setSchedule("alpha", { times: ["08:00", "20:00"], timeZone: "UTC", maxDigestTimes: 2 });
    const stops = [];
    container.monitor = () => new Promise((resolve, reject) => stops.push({ resolve, reject }));

    await registry.queueRun("alpha", { period: "morning", date: "2026-11-18", origin: "operator" });
    await drainAlarms(registry);
    expect(container.starts[0].env).toMatchObject({ CYRIS_RUN_PERIOD: "morning", CYRIS_RUN_DATE: "2026-11-18" });
    expect(registry.runs("alpha")[0]).toMatchObject({ state: "running", attempts: 1 });

    // The start looked granted, then the container stopped with no instance: queued again.
    container.running = false;
    stops[0].reject(new Error("There is no container instance that can be provided to this Durable Object"));
    await Promise.all(ctx.waited);
    expect(registry.runs("alpha")[0]).toMatchObject({ state: "queued", outcome: "no container instance" });

    await drainAlarms(registry);
    container.running = false;
    stops[1].resolve();
    await Promise.all(ctx.waited);
    expect(registry.runs("alpha")[0]).toMatchObject({ state: "done", attempts: 2, outcome: "exit 0" });
    vi.restoreAllMocks();
  });

  it("names its tenant on every line it logs about its container", async () => {
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const { object, container, settled } = await runObject("alpha");
    const exited = Object.assign(new Error("exit code 1"), { exitCode: 1 });
    let stopped;
    container.monitor = () => (stopped = Promise.reject(exited));

    await object.startRun({ period: "morning" });
    await stopped.catch(() => {});
    await settled();

    expect(log).toHaveBeenCalledWith("container stopped", expect.objectContaining({ tenant: "alpha", exitCode: 1 }));
    for (const [, fields] of log.mock.calls) expect(fields).toMatchObject({ tenant: "alpha" });
    log.mockRestore();
  });

  it("retries a start no instance can be given for up to 90 seconds", () => {
    expect(START_RETRY.maxWaitMs).toBe(90_000);
    expect(START_RETRY.backoffMs).toEqual([1_000, 2_000, 4_000, 8_000, 10_000]);
  });
});

const HOOK = "https://discord.com/api/webhooks/1/operator";

// The operator's webhook, and every alert it was posted, as the operator reads it.
function webhook() {
  const posted = [];
  vi.stubGlobal("fetch", async (url, init) => {
    if (url === HOOK) posted.push(JSON.parse(init.body).content);
    return new Response(null, { status: 204 });
  });
  return posted;
}

describe("a hosted tenant's run object tells the operator what its container cannot", () => {
  afterEach(() => vi.unstubAllGlobals());
  const exit = (exitCode, message = `exit code ${exitCode}`) => Object.assign(new Error(message), { exitCode });

  it("alerts when the start gives up after 90 s of no instance, and rethrows", async () => {
    vi.useFakeTimers();
    try {
      const posted = webhook();
      const { object, container } = await runObject("alpha", { ...EVERY_BINDING, [OPERATOR_WEBHOOK]: HOOK });
      container.start = () => {
        throw new Error("There is no container instance that can be provided to this Durable Object");
      };
      container.destroy = async () => {};

      const run = object.startRun({ period: "morning", date: "2026-11-20" });
      const failed = expect(run).rejects.toThrow("no container instance");
      await vi.runAllTimersAsync();
      await failed;

      expect(posted).toHaveLength(1);
      expect(posted[0]).toMatch(/^\[alpha\] Digest run could not start\n```\nperiod=morning date=2026-11-20\n/);
      expect(posted[0]).toContain("no container instance");
    } finally {
      vi.useRealTimers();
    }
  });

  it("alerts when the start throws at once, the hosted domain unset among the causes", async () => {
    const posted = webhook();
    const { object, container } = await runObject("alpha", {
      ...EVERY_BINDING,
      CYRIS_STORE_HOSTED_DOMAIN: "",
      [OPERATOR_WEBHOOK]: HOOK,
    });

    await expect(object.startRun({ period: "morning" })).rejects.toThrow("CYRIS_STORE_HOSTED_DOMAIN is not set");

    expect(container.starts).toEqual([]);
    expect(posted).toHaveLength(1);
    expect(posted[0]).toContain("[alpha] Digest run could not start");
    expect(posted[0]).toContain("CYRIS_STORE_HOSTED_DOMAIN is not set");
  });

  it("alerts when the registry cannot be read", async () => {
    const posted = webhook();
    const { object, ctx } = await runObject("alpha", { ...EVERY_BINDING, [OPERATOR_WEBHOOK]: HOOK });
    object.env.REGISTRY = { getByName: () => ({ tenant: async () => Promise.reject(new Error("registry reset")) }) };

    await expect(object.startRun({ period: "morning" })).rejects.toThrow("registry reset");

    expect(ctx.container.starts).toEqual([]);
    expect(posted).toEqual([expect.stringContaining("[alpha] Digest run could not start")]);
  });

  it("does not alert for a tenant it refuses as not active: that run was never due", async () => {
    const posted = webhook();
    const { object } = await runObject("paused", { ...EVERY_BINDING, [OPERATOR_WEBHOOK]: HOOK });

    await expect(object.startRun({ period: "morning" })).rejects.toThrow("not an active registered tenant");

    expect(posted).toEqual([]);
  });

  for (const [what, stop] of [
    ["exits non-zero", exit(1)],
    ["is OOM-killed", exit(137, "Container was killed: out of memory")],
    ["is stopped by the inactivity cap", exit(143)],
    ["stops with no exit code", new Error("container crashed")],
  ]) {
    it(`alerts when a container that reported nothing ${what}`, async () => {
      const posted = webhook();
      const { object, container, settled } = await runObject("alpha", { ...EVERY_BINDING, [OPERATOR_WEBHOOK]: HOOK });
      container.monitor = () => Promise.reject(stop);

      await object.startRun({ period: "morning" });
      await settled();

      expect(posted).toHaveLength(1);
      expect(posted[0]).toContain("[alpha] Digest run stopped before it reported");
      expect(posted[0]).toContain(`exitCode=${stop.exitCode ?? "unknown"}`);
    });
  }

  it("does not alert again when the run already reported why it ended", async () => {
    const posted = webhook();
    const { object, container, settled } = await runObject("alpha", { ...EVERY_BINDING, [OPERATOR_WEBHOOK]: HOOK });
    let exited;
    container.monitor = () => new Promise((_, reject) => (exited = reject));

    await object.startRun({ period: "morning" });
    // What OperatorAlertGateway does when the run's own run_failed alert arrives.
    await object.alertReported("run_failed");
    exited(exit(1));
    await settled();

    expect(posted).toEqual([]);
  });

  it("does not alert when a container exits 0", async () => {
    const posted = webhook();
    const { object, container, settled } = await runObject("alpha", { ...EVERY_BINDING, [OPERATOR_WEBHOOK]: HOOK });
    container.monitor = () => Promise.resolve();

    await object.startRun({ period: "morning" });
    await settled();

    expect(posted).toEqual([]);
  });

  it("forgets the last start's report before the next start", async () => {
    const posted = webhook();
    const { object, container, settled, ctx } = await runObject("alpha", { ...EVERY_BINDING, [OPERATOR_WEBHOOK]: HOOK });
    await object.alertReported("run_failed");
    container.monitor = () => Promise.reject(exit(1));

    await object.startRun({ period: "morning" });
    await settled();

    expect(ctx.storage.map.has(REPORTED)).toBe(false);
    expect(posted).toEqual([expect.stringContaining("Digest run stopped before it reported")]);
  });
});

const NO_INSTANCE_ERROR = "There is no container instance that can be provided to this Durable Object";

// A tenant's run object as hosted.js wires it to the registry's run queue: the queue starts it
// through TENANT_RUN, it tells the same registry when its container stops, and `posted` is what
// reached the operator's webhook.
async function queuedRun(tenant = "alpha") {
  const posted = webhook();
  const made = await runObject(tenant, { ...EVERY_BINDING, [OPERATOR_WEBHOOK]: HOOK });
  made.env.TENANT_RUN = { getByName: () => made.object };
  const registry = made.env.REGISTRY.getByName(REGISTRY_NAME);
  await registry.setSchedule(tenant, { times: ["08:00", "20:00"], timeZone: "UTC", maxDigestTimes: 2 });
  return { ...made, registry, posted };
}

// What the run object reaches as its registry: the real one, but for `runEnded`, which `stopped`
// answers instead, as a stop report that fails or one the runtime's alarm follows at once.
function reportStopsWith(env, stopped) {
  const registries = env.REGISTRY;
  env.REGISTRY = {
    getByName: (...args) =>
      new Proxy(registries.getByName(...args), {
        get: (target, key) => (key === "runEnded" ? stopped : typeof target[key] === "function" ? target[key].bind(target) : target[key]),
      }),
  };
}

const morning = { period: "morning", date: "2026-11-20", origin: "operator" };

describe("a queued run's end reaches the operator once, from the run queue or the run object", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });
  const exit = (exitCode) => Object.assign(new Error(`exit code ${exitCode}`), { exitCode });

  it("tells a run whose every start found no instance once, when the queue fails it, naming the tenant", async () => {
    vi.useFakeTimers();
    vi.spyOn(console, "log").mockImplementation(() => {});
    const { container, registry, posted } = await queuedRun();
    container.start = () => {
      throw new Error(NO_INSTANCE_ERROR);
    };
    container.destroy = async () => {};

    await registry.queueRun("alpha", morning);
    for (let start = 1; start <= MAX_START_ATTEMPTS; start++) {
      // Each start gives up after its 90 s retry; the queue holds it, so the run object alerts nobody.
      const pass = drainAlarms(registry);
      await vi.runAllTimersAsync();
      await pass;
      if (start < MAX_START_ATTEMPTS) expect(posted, `start ${start}`).toEqual([]);
      vi.setSystemTime(Date.now() + DISPATCH_EVERY_MS);
    }

    expect(registry.runs("alpha")[0]).toMatchObject({ state: "failed", attempts: MAX_START_ATTEMPTS });
    expect(posted).toEqual([
      "[alpha] Digest run could not start\n```\nperiod=morning date=2026-11-20 run=failed starts=4\n" +
        "no container instance, after 4 starts\n```",
    ]);
  });

  it("tells a queued start that throws at once once, from the queue, and a paused tenant's refusal not at all", async () => {
    vi.spyOn(console, "log").mockImplementation(() => {});
    const { env, registry, posted } = await queuedRun();
    env.CYRIS_STORE_HOSTED_DOMAIN = "";

    await registry.queueRun("alpha", morning);
    await drainAlarms(registry);

    expect(registry.runs("alpha")[0]).toMatchObject({ state: "failed", attempts: 1 });
    expect(posted).toHaveLength(1);
    expect(posted[0]).toMatch(/^\[alpha\] Digest run could not start\n```\nperiod=morning date=2026-11-20 run=failed starts=1\n/);
    expect(posted[0]).toContain("CYRIS_STORE_HOSTED_DOMAIN is not set");

    const paused = await queuedRun("bravo");
    await paused.registry.queueRun("bravo", morning);
    await paused.registry.register("bravo", { status: "paused" });
    await drainAlarms(paused.registry);

    expect(paused.registry.runs("bravo")[0]).toMatchObject({ state: "failed" });
    expect(paused.posted).toEqual([]);
  });

  it("tells a run whose lease ran out with no stop report once, naming the tenant, and queues it again", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-11-20T08:00:00Z"));
    vi.spyOn(console, "log").mockImplementation(() => {});
    const { container, registry, posted } = await queuedRun();
    await registry.queueRun("alpha", morning);
    await drainAlarms(registry);
    expect(registry.runs("alpha")[0]).toMatchObject({ state: "running" });

    // Its container stopped while no monitor watched it, as after a deploy that replaced its object.
    container.running = false;
    vi.setSystemTime(Date.now() + RUN_LEASE_MS + 1_000);
    await drainAlarms(registry);

    expect(registry.runs("alpha").map((run) => [run.state, run.attempts])).toEqual([
      ["running", 2],
      ["lost", 1],
    ]);
    expect(posted).toEqual([
      "[alpha] Digest run lost: no stop report, queued again\n```\nperiod=morning date=2026-11-20 run=lost starts=1\n" +
        "no stop reported within 20 minutes; queued again\n```",
    ]);
  });

  it("does not tell a lost run again when its run object, or its run, already told how it stopped", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.spyOn(console, "log").mockImplementation(() => {});
    for (const runReported of [false, true]) {
      vi.setSystemTime(new Date("2026-11-20T08:00:00Z"));
      const { object, container, ctx, env, registry, posted } = await queuedRun();
      // The stop happens, but its report never reaches the registry.
      reportStopsWith(env, async () => {
        throw new Error("registry unreachable");
      });
      const exits = [];
      container.monitor = () => new Promise((_, reject) => exits.push(reject));
      await registry.queueRun("alpha", morning);
      await drainAlarms(registry);
      const watched = [...ctx.waited];

      if (runReported) await object.alertReported("run_failed");
      container.running = false;
      exits[0](exit(1));
      await Promise.allSettled(watched);
      expect(await object.endTold({ id: 1, attempt: 1 }), `run reported: ${runReported}`).toBe(true);
      vi.setSystemTime(Date.now() + RUN_LEASE_MS + 1_000);
      await drainAlarms(registry);

      expect(registry.runs("alpha")[1]).toMatchObject({ state: "lost" });
      // The run's own alert went to the gateway, not this webhook; the stop's went here, once.
      expect(posted).toEqual(runReported ? [] : [expect.stringContaining("[alpha] Digest run stopped before it reported")]);
      // The next start of the lost run's issue forgets both records.
      expect(ctx.storage.map.has(REPORTED) || ctx.storage.map.has(TOLD)).toBe(false);
    }
  });

  it("tells a queued run's abnormal exit once, from the run object, though the queue records it as failed", async () => {
    vi.spyOn(console, "log").mockImplementation(() => {});
    const { container, ctx, registry, posted } = await queuedRun();
    const exits = [];
    container.monitor = () => new Promise((_, reject) => exits.push(reject));
    await registry.queueRun("alpha", morning);
    await drainAlarms(registry);

    exits[0](exit(137));
    await Promise.allSettled(ctx.waited);

    expect(registry.runs("alpha")[0]).toMatchObject({ state: "failed", outcome: "exit 137: Error: exit code 137" });
    expect(posted).toEqual([expect.stringContaining("[alpha] Digest run stopped before it reported\n```\nexitCode=137")]);
  });

  it("reads the run's own report before the stop report frees the tenant for a start that clears it", async () => {
    vi.spyOn(console, "log").mockImplementation(() => {});
    const { object, container, ctx, env, registry, posted } = await queuedRun();
    // The runtime may run the registry's alarm as soon as the stop is reported, so the tenant's
    // next queued run starts, and its interception clears the record, before the monitor goes on.
    reportStopsWith(env, async (id, stopped) => {
      const run = await registry.runEnded(id, stopped);
      container.running = false;
      await drainAlarms(registry);
      return run;
    });
    const exits = [];
    container.monitor = () => new Promise((_, reject) => exits.push(reject));
    await registry.queueRun("alpha", morning);
    await drainAlarms(registry);
    await registry.queueRun("alpha", { ...morning, period: "evening" });
    const watched = [...ctx.waited];

    await object.alertReported("run_failed");
    exits[0](exit(1));
    await Promise.allSettled(watched);

    expect(registry.runs("alpha").map((run) => [run.period, run.state])).toEqual([
      ["evening", "running"],
      ["morning", "failed"],
    ]);
    expect(ctx.storage.map.has(REPORTED)).toBe(false);
    expect(posted).toEqual([]);
  });

  it("reports the run its start ran from an object restarted mid-run, which adopts the container", async () => {
    vi.spyOn(console, "log").mockImplementation(() => {});
    const { container, ctx, env, registry } = await queuedRun();
    const exits = [];
    container.monitor = () => new Promise((resolve) => exits.push(resolve));
    await registry.queueRun("alpha", morning);
    await drainAlarms(registry);

    // A deploy replaces the object; the new one finds the container running and watches it.
    ctx.waited = [];
    const restarted = new TenantContainer(ctx, env);
    env.TENANT_RUN = { getByName: () => restarted };
    await new Promise((resolve) => setImmediate(resolve));
    exits[1]();
    await Promise.allSettled(ctx.waited);

    expect(registry.runs("alpha")[0]).toMatchObject({ id: 1, state: "done", outcome: "exit 0" });
  });

  // The webhook's first post hangs until `release()`, heeding no abort, as the review's probe had
  // it; every later post is answered at once. `posted` is each post's first line.
  function hangingWebhook() {
    const posted = [];
    let release;
    vi.stubGlobal("fetch", (url, init) => {
      posted.push(JSON.parse(init.body).content.split("\n")[0]);
      if (posted.length > 1) return Promise.resolve(new Response(null, { status: 204 }));
      return new Promise((resolve) => {
        release = () => resolve(new Response(null, { status: 204 }));
      });
    });
    return { posted, release: () => release() };
  }
  const turn = () => new Promise((resolve) => setImmediate(resolve));

  it("reports an abnormal stop once its alert's post times out, so a hung webhook alerts once and the next run runs", async () => {
    vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout"] });
    vi.setSystemTime(new Date("2026-11-20T08:00:00Z"));
    vi.spyOn(console, "log").mockImplementation(() => {});
    const { container, ctx, object, registry } = await queuedRun();
    const { posted, release } = hangingWebhook();
    const exits = [];
    container.monitor = () => new Promise((_, reject) => exits.push(reject));
    await registry.queueRun("alpha", morning);
    await drainAlarms(registry);
    const watched = [...ctx.waited];

    container.running = false;
    exits[0](exit(1));
    await turn();
    await vi.advanceTimersByTimeAsync(ALERT_TIMEOUT_MS - 1);
    // The stop report waits on the alert, and no longer than its timeout.
    expect(registry.runs("alpha")[0]).toMatchObject({ state: "running" });
    await vi.advanceTimersByTimeAsync(1);
    await Promise.allSettled(watched);
    expect(registry.runs("alpha")[0]).toMatchObject({ state: "failed", outcome: "exit 1: Error: exit code 1" });

    // Past the lease, which no longer holds the run, and on to the tenant's next run.
    vi.setSystemTime(Date.now() + RUN_LEASE_MS + 1_000);
    await drainAlarms(registry);
    await registry.queueRun("alpha", { ...morning, period: "evening" });
    await drainAlarms(registry);
    release();
    await turn();

    expect(posted).toEqual(["[alpha] Digest run stopped before it reported"]);
    expect(registry.runs("alpha").map((run) => [run.period, run.state, run.attempts])).toEqual([
      ["evening", "running", 1],
      ["morning", "failed", 1],
    ]);
    expect(container.running).toBe(true);
    expect(container.starts).toHaveLength(2);
    expect(ctx.storage.map.has(TOLD)).toBe(false);
    expect(await object.endTold({ id: 2, attempt: 1 })).toBe(false);
  });

  it("ends nothing of the tenant's next run with a stop report heard after the lease ended its own", async () => {
    vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout"] });
    vi.setSystemTime(new Date("2026-11-20T08:00:00Z"));
    vi.spyOn(console, "log").mockImplementation(() => {});
    const { container, ctx, object, registry } = await queuedRun();
    const { posted } = hangingWebhook();
    const exits = [];
    container.monitor = () => new Promise((_, reject) => exits.push(reject));
    await registry.queueRun("alpha", morning);
    await drainAlarms(registry);
    const watched = [...ctx.waited];

    // The run stops a second before its lease ends, and its alert is still posting when the lease
    // ends it as lost and starts its issue again.
    vi.setSystemTime(Date.now() + RUN_LEASE_MS - 1_000);
    container.running = false;
    exits[0](exit(1));
    await turn();
    vi.setSystemTime(Date.now() + 2_000);
    await drainAlarms(registry);
    expect(registry.runs("alpha").map((run) => [run.id, run.state])).toEqual([
      [2, "running"],
      [1, "lost"],
    ]);
    expect(ctx.storage.map.get(RUN)).toEqual({ id: 2, attempt: 2 });

    await vi.advanceTimersByTimeAsync(ALERT_TIMEOUT_MS);
    await Promise.allSettled(watched);

    // The first start's report names its own run, which the queue no longer holds.
    expect(registry.runs("alpha").map((run) => [run.id, run.state, run.outcome])).toEqual([
      [2, "running", null],
      [1, "lost", "no stop reported within 20 minutes; queued again"],
    ]);
    expect(container.running).toBe(true);
    // Its alert's record names its own start, so the second start's lease end still tells.
    expect(ctx.storage.map.get(TOLD)).toEqual({ id: 1, attempt: 1 });
    expect(await object.endTold({ id: 2, attempt: 2 })).toBe(false);
    expect(posted).toEqual(["[alpha] Digest run stopped before it reported", "[alpha] Digest run lost: no stop report, queued again"]);
  });
});

describe("the seed and the report", () => {
  const AT = new Date("2026-11-18T06:00:00Z");
  const seeded = async (tenant, TENANT_DB = namespace()) => {
    const db = TENANT_DB.getByName(tenant);
    const answer = await seedTenant({
      db,
      tenant,
      schemaSql: SCHEMA_SQL,
      hostedSchemaSql: HOSTED_SCHEMA_SQL,
      startingValues: STARTING_VALUES,
      seed: SEED,
      catalog: FEED_CATALOG,
      schedule: SEEDED_SCHEDULE,
      now: AT,
    });
    return { db, answer, TENANT_DB };
  };

  it("gives each tenant one digest hour of its own, never the backup's, the same one every time", () => {
    const hours = Array.from({ length: 24 }, (_, hour) => hour).filter((hour) => !backupHours().includes(hour));
    expect(backupHours()).toEqual([4, 5, 6]);
    const ids = Array.from({ length: 200 }, (_, i) => `tenant-${i}`);
    const given = ids.map((id) => spreadHour(id, hours));

    expect(given.every((hour) => hours.includes(hour))).toBe(true);
    expect(ids.map((id) => spreadHour(id, hours))).toEqual(given);
    // Spread over every hour a run may start in, none taking a crowd.
    const perHour = hours.map((hour) => given.filter((g) => g === hour).length);
    expect(Math.min(...perHour)).toBeGreaterThan(0);
    expect(Math.max(...perHour)).toBeLessThan(3 * (ids.length / hours.length));
    // The runbook's two tenants are not due together, and each is one hour in UTC.
    expect(seedSchedule(SEED, "alpha", hours)).toEqual({ times: ["08:00"], timeZone: "UTC" });
    expect(seedSchedule(SEED, "bravo", hours)).toEqual({ times: ["00:00"], timeZone: "UTC" });
  });

  it("writes the schema, every starting value under the seed's own, and the tenant's feeds", async () => {
    const { db, answer } = await seeded("alpha");

    const settings = Object.fromEntries(
      (await db.query("SELECT key, value, updated_at FROM settings ORDER BY key")).rows.map((row) => [
        row.key,
        [JSON.parse(row.value), row.updated_at],
      ]),
    );
    const expected = seedSettings(STARTING_VALUES, SEED, SEEDED_SCHEDULE);
    expect(Object.keys(settings).sort()).toEqual(Object.keys(expected).sort());
    for (const [key, value] of Object.entries(expected)) {
      expect(settings[key]).toEqual([value, "2026-11-18T06:00:00+00:00"]);
    }
    expect(settings["llm_provider.provider"][0]).toBe("anthropic");
    expect(settings["llm_provider.model"][0]).toBe("claude-haiku-5-5");

    const sources = (await db.query("SELECT name, url, type, config FROM sources ORDER BY name")).rows;
    expect(sources.map((row) => row.name).sort()).toEqual([...SEED.sources.alpha].sort());
    for (const row of sources) {
      const feed = FEED_CATALOG.feeds.find((entry) => entry.name === row.name);
      expect(row.url).toBe(feed.url);
      expect(row.type).toBe("rss");
      expect(JSON.parse(row.config)).not.toHaveProperty("topics");
      expect(JSON.parse(row.config).lane).toBe(feed.lane);
    }
    expect(answer).toEqual({
      tenant: "alpha",
      settings: Object.keys(expected).length,
      sources: SEED.sources.alpha,
      tables: [...(SCHEMA_SQL + HOSTED_SCHEMA_SQL).matchAll(/CREATE TABLE IF NOT EXISTS (\w+)/g)].length,
    });
    // The tenant's pages table is the hosted schema's, never schema.sql's.
    expect(SCHEMA_SQL).not.toContain("hosted_pages");
    expect(Object.keys(await db.tables())).toContain("hosted_pages");
  });

  it("puts the same rows back when seeded again", async () => {
    const { db, TENANT_DB } = await seeded("alpha");
    const provider = "SELECT value FROM settings WHERE key = 'llm_provider.provider'";
    await db.query("UPDATE settings SET value = ? WHERE key = ?", ['"openai"', "llm_provider.provider"]);

    await seeded("alpha", TENANT_DB);

    expect((await db.query("SELECT count(*) AS n FROM sources")).rows[0].n).toBe(SEED.sources.alpha.length);
    const [{ value }] = (await db.query(provider, [])).rows;
    expect(JSON.parse(value)).toBe("anthropic");
  });

  it("gives each tenant its own feeds in its own database", async () => {
    const TENANT_DB = namespace();
    await seeded("alpha", TENANT_DB);
    await seeded("bravo", TENANT_DB);

    const names = async (tenant) => (await tenantReport({ db: TENANT_DB.getByName(tenant), tenant })).sources;
    expect(await names("alpha")).toEqual([...SEED.sources.alpha].sort());
    expect(await names("bravo")).toEqual([...SEED.sources.bravo].sort());
  });

  it("refuses a tenant the seed names no sources for, before writing anything", async () => {
    const TENANT_DB = namespace();

    await expect(seeded("charlie", TENANT_DB)).rejects.toThrow("the seed names no sources for charlie");
    expect(await TENANT_DB.getByName("charlie").tables()).toEqual({});
  });

  it("reports the runs, the issues and where the stored articles came from", async () => {
    const { db } = await seeded("bravo");
    await db.query(
      "INSERT INTO digest_runs (finished_at, status, period, dry_run, fetched) VALUES (?, ?, ?, ?, ?)",
      ["2026-11-18T06:05:00+00:00", "ok", "morning", 0, 12],
    );
    await db.query("INSERT INTO digests (date, period, content, raw_page, saved_at) VALUES (?, ?, ?, ?, ?)", [
      "2026-11-18",
      "morning",
      JSON.stringify({ articles_included: 7 }),
      0,
      "2026-11-18T06:05:00+00:00",
    ]);
    for (const url of ["https://a.test/1", "https://a.test/2"]) {
      await db.query(
        "INSERT INTO stored_articles (url, published_at, source_name, source_lane, first_seen_at) " +
          "VALUES (?, ?, ?, ?, ?)",
        [url, "2026-11-18T05:00:00+00:00", "Aeon", "skim", "2026-11-18T06:00:00+00:00"],
      );
    }

    const report = await tenantReport({ db, tenant: "bravo" });

    expect(report.digest_runs).toEqual([
      {
        id: 1,
        finished_at: "2026-11-18T06:05:00+00:00",
        status: "ok",
        period: "morning",
        dry_run: 0,
        fetched: 12,
      },
    ]);
    expect(report.digests).toEqual([
      { date: "2026-11-18", period: "morning", saved_at: "2026-11-18T06:05:00+00:00", articles_included: 7 },
    ]);
    expect(report.stored_articles).toEqual([{ source_name: "Aeon", articles: 2 }]);
    expect(report.tables.digest_runs).toBe(1);
    expect(Object.keys(report.tables).some((name) => name.startsWith("_"))).toBe(false);
  });

  it("reports a tenant that was never seeded as empty, not as an error", async () => {
    const TENANT_DB = namespace();

    expect(await tenantReport({ db: TENANT_DB.getByName("alpha"), tenant: "alpha" })).toEqual({
      tenant: "alpha",
      tables: {},
      sources: [],
      stored_articles: [],
      digest_runs: [],
      digests: [],
      newsletters: [],
      latest_page: null,
    });
  });
});

const TEAM = "team.example";
const AUD = "AUD-HOSTED";
const { privateKey, publicKey } = await generateKeyPair("RS256");
const JWK = { ...(await exportJWK(publicKey)), kid: "k1", alg: "RS256", use: "sig" };
const ACCESS_KEYS = { keySet: createLocalJWKSet({ keys: [JWK] }), issuer: `https://${TEAM}` };
// The service token the operator signs in with: its Client ID, which Access signs in as `common_name`.
const OPERATOR = "operator-token.access";
const ENV = { CYRIS_ACCESS_TEAM_DOMAIN: TEAM, CYRIS_ACCESS_AUD: AUD, CYRIS_HOSTED_OPERATORS: OPERATOR };

const accessJwt = (claims = { common_name: OPERATOR }) =>
  new SignJWT(claims)
    .setProtectedHeader({ alg: "RS256", kid: "k1" })
    .setIssuer(`https://${TEAM}`)
    .setAudience(AUD)
    .setIssuedAt()
    .setExpirationTime(Math.floor(Date.now() / 1000) + 3600)
    .sign(privateKey);

async function operator(method, path, { signedIn = true, claims } = {}) {
  const headers = signedIn ? { "Cf-Access-Jwt-Assertion": await accessJwt(claims) } : {};
  return new Request(`https://cyris-hosted.example.workers.dev${path}`, { method, headers });
}

// What the registry answers for each tenant the routes are asked about.
const REGISTERED = {
  alpha: { id: "alpha", status: "active", timezone: "UTC" },
  bravo: { id: "bravo", status: "active", timezone: "UTC" },
  paused: { id: "paused", status: "paused", timezone: "UTC" },
};

function hostedDeps({ timeZone = "UTC", queued = { queued: { id: 1 } } } = {}) {
  const calls = [];
  const record =
    (name, answer) =>
    async (...args) => {
      calls.push([name, ...args]);
      return answer;
    };
  return {
    calls,
    accessKeys: ACCESS_KEYS,
    register: record("register", { tenant: { id: "registered", status: "active" } }),
    tenant: async (tenant) => (REGISTERED[tenant] ? { ...REGISTERED[tenant], timezone: timeZone } : null),
    schedule: record("schedule", { tenant: { id: "scheduled" }, settings: true }),
    seed: record("seed", { seeded: true }),
    report: record("report", { reported: true }),
    queueRun: record("queueRun", queued),
    now: () => new Date("2026-11-18T06:00:00Z"),
  };
}

describe("the hosted Worker's operator routes", () => {
  it("refuse every request without an Access sign-in, before naming any tenant", async () => {
    for (const [method, path] of [
      ["POST", "/hosted/tenants/alpha/register"],
      ["POST", "/hosted/tenants/alpha/seed"],
      ["POST", "/hosted/tenants/alpha/run?period=morning"],
      ["GET", "/hosted/tenants/alpha/report"],
      ["GET", "/hosted/tenants/nobody/report"],
      ["GET", "/"],
      ["GET", "/settings"],
    ]) {
      const deps = hostedDeps();
      const response = await handleHostedRequest(await operator(method, path, { signedIn: false }), ENV, deps);
      expect(response.status).toBe(403);
      expect(deps.calls).toEqual([]);
    }
  });

  it("refuse them all on a Worker with no Access settings", async () => {
    const deps = hostedDeps();
    const response = await handleHostedRequest(await operator("POST", "/hosted/tenants/alpha/seed"), {}, deps);

    expect(response.status).toBe(403);
    expect(deps.calls).toEqual([]);
  });

  it("refuse a valid Access sign-in whose identity the operator list does not name, before naming any tenant", async () => {
    const env = { ...ENV, CYRIS_HOSTED_OPERATORS: `${OPERATOR}, Owner@Example.com` };
    for (const claims of [
      // A reader added to the Access application by mistake, and another service token.
      { email: "reader@example.com" },
      { common_name: "another-token.access" },
      // An entry with an @ names an email, never a service token, and the other way round.
      { common_name: "owner@example.com" },
      { email: OPERATOR },
      {},
    ]) {
      for (const [method, path] of [
        ["POST", "/hosted/tenants/alpha/register?status=paused"],
        ["POST", "/hosted/tenants/alpha/seed"],
        ["POST", "/hosted/tenants/alpha/run?period=morning"],
        ["POST", "/hosted/tenants/alpha/query"],
        ["GET", "/hosted/tenants/alpha/report"],
        ["GET", "/hosted/tenants/alpha/page"],
      ]) {
        const deps = hostedDeps();
        const response = await handleHostedRequest(await operator(method, path, { claims }), env, deps);
        expect(response.status, `${JSON.stringify(claims)} ${path}`).toBe(403);
        expect((await response.json()).detail).toMatch(/not one the hosted Worker's operators list names/);
        expect(deps.calls).toEqual([]);
      }
    }
  });

  it("admit the service token and the operator's email the list names, the email in any case", async () => {
    const env = { ...ENV, CYRIS_HOSTED_OPERATORS: ` ${OPERATOR} ,Owner@Example.com,` };
    for (const claims of [{ common_name: OPERATOR }, { email: "owner@example.com" }, { email: "OWNER@example.COM" }]) {
      const deps = hostedDeps();
      const response = await handleHostedRequest(await operator("GET", "/hosted/tenants/alpha/report", { claims }), env, deps);
      expect(response.status, JSON.stringify(claims)).toBe(200);
      expect(deps.calls).toEqual([["report", "alpha"]]);
    }
  });

  it("refuse every operator route on a Worker that names no operator, whoever Access signed in", async () => {
    for (const operators of [undefined, "", " , "]) {
      const deps = hostedDeps();
      const env = { ...ENV, CYRIS_HOSTED_OPERATORS: operators };
      const response = await handleHostedRequest(await operator("GET", "/hosted/tenants/alpha/report"), env, deps);
      expect(response.status).toBe(403);
      expect((await response.json()).detail).toBe("The Worker has no CYRIS_HOSTED_OPERATORS set, so no operator can be admitted.");
      expect(deps.calls).toEqual([]);
    }
  });

  it("mark every answer noindex and no-referrer, a refusal, a miss and an operator's JSON alike", async () => {
    const answers = [
      await handleHostedRequest(await operator("GET", "/hosted/tenants/alpha/report"), ENV, hostedDeps()),
      await handleHostedRequest(await operator("POST", "/hosted/tenants/alpha/seed"), ENV, hostedDeps()),
      await handleHostedRequest(await operator("GET", "/hosted/tenants/alpha/seed"), ENV, hostedDeps()),
      await handleHostedRequest(await operator("GET", "/hosted/tenants/charlie/report"), ENV, hostedDeps()),
      await handleHostedRequest(await operator("GET", "/hosted/tenants/alpha/report", { signedIn: false }), ENV, hostedDeps()),
      await handleHostedRequest(await operator("GET", "/"), ENV, hostedDeps()),
      await handleHostedRequest(await operator("GET", "/t/alpha/", { signedIn: false }), ENV, hostedDeps()),
    ];

    expect(answers.map((response) => response.status)).toEqual([200, 200, 405, 404, 403, 404, 302]);
    for (const response of answers) {
      expect(response.headers.get("X-Robots-Tag")).toBe("noindex, nofollow");
      expect(response.headers.get("Referrer-Policy")).toBe("no-referrer");
      expect(response.headers.get("X-Content-Type-Options")).toBe("nosniff");
    }
    // The answer is otherwise as the route made it.
    expect(answers[0].headers.get("Content-Type")).toBe("application/json");
    expect(await answers[0].json()).toEqual({ reported: true });
  });

  it("seed, report and queue a run for a named tenant, dated today in its zone when no date is named", async () => {
    const deps = hostedDeps();

    const seeded = await handleHostedRequest(await operator("POST", "/hosted/tenants/alpha/seed"), ENV, deps);
    const reported = await handleHostedRequest(await operator("GET", "/hosted/tenants/bravo/report"), ENV, deps);
    const run = await operator("POST", "/hosted/tenants/bravo/run?period=evening");
    const ran = await handleHostedRequest(run, ENV, deps);

    expect([seeded.status, reported.status, ran.status]).toEqual([200, 200, 202]);
    expect(await ran.json()).toEqual({ queued: { id: 1 } });
    expect(deps.calls).toEqual([
      ["seed", "alpha"],
      ["report", "bravo"],
      ["queueRun", "bravo", { period: "evening", date: "2026-11-18", origin: "operator" }],
    ]);
  });

  it("answer a run the registry will not queue with its reason: 409 for one already live, 429 at the cap", async () => {
    for (const [code, status] of [
      ["live", 409],
      ["inactive", 409],
      ["cap", 429],
    ]) {
      const deps = hostedDeps({ queued: { refused: `refused: ${code}`, code } });
      const response = await handleHostedRequest(await operator("POST", "/hosted/tenants/alpha/run?period=morning"), ENV, deps);

      expect(response.status, code).toBe(status);
      expect((await response.json()).error).toBe(`refused: ${code}`);
    }
  });

  it("set a tenant's schedule from the query, and answer the registry's refusal with 400", async () => {
    const deps = hostedDeps();
    const path = "/hosted/tenants/alpha/schedule?times=07:00,19:00&timezone=Asia/Taipei&daily_run_cap=4";

    const scheduled = await handleHostedRequest(await operator("POST", path), ENV, deps);
    const refusing = { ...hostedDeps(), schedule: async () => ({ refused: "not a time zone: Mars" }) };
    const refused = await handleHostedRequest(await operator("POST", path), ENV, refusing);

    expect(scheduled.status).toBe(200);
    expect(deps.calls).toEqual([["schedule", "alpha", { times: ["07:00", "19:00"], timeZone: "Asia/Taipei", dailyRunCap: "4" }]]);
    expect(refused.status).toBe(400);
    expect(await refused.json()).toEqual({ error: "not a time zone: Mars" });
  });

  it("serve no page and no tenant the registry does not hold", async () => {
    const deps = hostedDeps();
    for (const path of ["/", "/settings", "/run", "/hosted/tenants/charlie/report", "/hosted/tenants/alpha"]) {
      expect((await handleHostedRequest(await operator("GET", path), ENV, deps)).status).toBe(404);
    }
    for (const path of ["/hosted/tenants/charlie/seed", "/hosted/tenants/charlie/run?period=morning"]) {
      const response = await handleHostedRequest(await operator("POST", path), ENV, deps);
      expect(response.status).toBe(404);
      expect(await response.json()).toEqual({ error: "charlie is not a registered tenant" });
    }
    expect(deps.calls).toEqual([]);
  });

  it("register a tenant, with the status the operator names or active", async () => {
    const deps = hostedDeps();

    const active = await handleHostedRequest(await operator("POST", "/hosted/tenants/charlie/register"), ENV, deps);
    const paused = await operator("POST", "/hosted/tenants/charlie/register?status=paused");

    expect(active.status).toBe(200);
    expect(await active.json()).toEqual({ id: "registered", status: "active" });
    expect((await handleHostedRequest(paused, ENV, deps)).status).toBe(200);
    expect(deps.calls).toEqual([
      ["register", "charlie", undefined],
      ["register", "charlie", "paused"],
    ]);
  });

  it("answer a registration the registry refuses with 400 and its reason", async () => {
    const deps = { ...hostedDeps(), register: async () => ({ refused: "a tenant's status is one of active" }) };
    const response = await handleHostedRequest(
      await operator("POST", "/hosted/tenants/charlie/register?status=gone"),
      ENV,
      deps,
    );

    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ error: "a tenant's status is one of active" });
  });

  it("start no run for a tenant that is not active, but still seed and report it", async () => {
    const deps = hostedDeps();
    const ask = async (method, path) => handleHostedRequest(await operator(method, path), ENV, deps);

    const run = await ask("POST", "/hosted/tenants/paused/run?period=morning");

    expect(run.status).toBe(409);
    expect(await run.json()).toEqual({ error: "paused is paused, not active" });
    expect((await ask("POST", "/hosted/tenants/paused/seed")).status).toBe(200);
    expect((await ask("GET", "/hosted/tenants/paused/report")).status).toBe(200);
    expect(deps.calls).toEqual([
      ["seed", "paused"],
      ["report", "paused"],
    ]);
  });

  it("take each route by its own method only", async () => {
    const deps = hostedDeps();
    for (const [method, path] of [
      ["GET", "/hosted/tenants/alpha/register"],
      ["GET", "/hosted/tenants/alpha/schedule"],
      ["GET", "/hosted/tenants/alpha/seed"],
      ["GET", "/hosted/tenants/alpha/run?period=morning"],
      ["POST", "/hosted/tenants/alpha/report"],
    ]) {
      expect((await handleHostedRequest(await operator(method, path), ENV, deps)).status).toBe(405);
    }
    expect(deps.calls).toEqual([]);
  });

  it("start a run only for a named period, and a date only today or yesterday in the tenant's zone", async () => {
    const deps = hostedDeps({ timeZone: "Asia/Taipei" });
    const run = async (query) =>
      handleHostedRequest(await operator("POST", `/hosted/tenants/alpha/run${query}`), ENV, deps);

    expect((await run("")).status).toBe(400);
    expect((await run("?period=noon")).status).toBe(400);
    expect((await run("?period=morning&date=2026-11-01")).status).toBe(400);
    expect((await run("?period=morning&date=2026-11-18")).status).toBe(202);
    expect(deps.calls).toEqual([["queueRun", "alpha", { period: "morning", date: "2026-11-18", origin: "operator" }]]);
  });

  it("queue no run for a tenant the registry holds no schedule for: it has no zone to date the issue in", async () => {
    const deps = hostedDeps({ timeZone: null });
    const response = await handleHostedRequest(
      await operator("POST", "/hosted/tenants/alpha/run?period=morning&date=2026-11-18"),
      ENV,
      deps,
    );

    expect(response.status).toBe(409);
    expect((await response.json()).error).toBe(
      "alpha has no schedule in the registry; POST /hosted/tenants/alpha/schedule first",
    );
    expect(deps.calls).toEqual([]);
  });
});

describe("the hosted Worker's backup routes", () => {
  const restorePath = "/hosted/tenants/alpha-copy/restore?date=2026-11-22&from=alpha";
  const backupDeps = (restored = { tenant: "alpha-copy", restored: { sources: 1 } }) => {
    const deps = hostedDeps();
    deps.restore = async (...args) => {
      deps.calls.push(["restore", ...args]);
      return restored;
    };
    deps.backup = async () => {
      deps.calls.push(["backup"]);
      return { date: "2026-11-22", exports: 3, failed: [] };
    };
    return deps;
  };

  it("restore an export into an id the registry does not hold, which the registry is never asked to create", async () => {
    const deps = backupDeps();

    const response = await handleHostedRequest(await operator("POST", restorePath), ENV, deps);

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ tenant: "alpha-copy", restored: { sources: 1 } });
    expect(deps.calls).toEqual([["restore", "alpha-copy", { date: "2026-11-22", from: "alpha" }]]);
  });

  it("answer a refused restore with its status", async () => {
    const deps = backupDeps({ refused: "alpha-copy is already registered; restore into a new tenant id", status: 409 });

    const response = await handleHostedRequest(await operator("POST", restorePath), ENV, deps);

    expect(response.status).toBe(409);
    expect((await response.json()).error).toMatch(/already registered/);
  });

  it("run today's backup now and answer what it did", async () => {
    const deps = backupDeps();

    const response = await handleHostedRequest(await operator("POST", "/hosted/backup"), ENV, deps);

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ date: "2026-11-22", exports: 3, failed: [] });
    expect(deps.calls).toEqual([["backup"]]);
  });

  it("take only an operator's POST from this site, before restoring or backing up anything", async () => {
    for (const path of [restorePath, "/hosted/backup"]) {
      const refused = [
        [await operator("POST", path, { signedIn: false }), 403],
        [await operator("POST", path, { claims: { email: "reader@example.com" } }), 403],
        [await operator("GET", path), 405],
        [new Request((await operator("POST", path)).url, { method: "POST", headers: { "Cf-Access-Jwt-Assertion": await accessJwt(), "Sec-Fetch-Site": "cross-site" } }), 403],
      ];
      for (const [request, status] of refused) {
        const deps = backupDeps();
        const response = await handleHostedRequest(request, ENV, deps);
        expect(response.status, `${request.method} ${path}`).toBe(status);
        expect(deps.calls).toEqual([]);
      }
    }
  });
});
