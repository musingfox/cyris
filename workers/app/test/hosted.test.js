import { describe, it, expect } from "vitest";
import { generateKeyPair, exportJWK, SignJWT, createLocalJWKSet } from "jose";
import FEED_CATALOG from "../../../src/cyris/onboarding_feeds.json" with { type: "json" };
import STARTING_VALUES from "../../../src/cyris/settings_starting_values.json" with { type: "json" };
import SEED from "../src/hosted_seed.json" with { type: "json" };
import { OPERATOR_WEBHOOK, OPS_HOST } from "../src/hosted_alerts.js";
import { START_RETRY, TenantContainer, hostedContainerEnv } from "../src/hosted_container.js";
import { handleHostedRequest } from "../src/hosted_router.js";
import { seedSettings, seedTenant, tenantReport } from "../src/hosted_seed.js";
import { DB_HOST } from "../src/tenant_db.js";
import { tenantRegistry } from "../src/tenant_registry.js";
import { HOSTED_SCHEMA_SQL, SCHEMA_SQL, namespace, registryNamespace } from "./sql_storage.js";

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

// A registry that holds `alpha` and `bravo` as active, and `paused` as paused.
async function registry() {
  const REGISTRY = registryNamespace();
  for (const tenant of ["alpha", "bravo"]) await tenantRegistry({ REGISTRY }).register(tenant);
  await tenantRegistry({ REGISTRY }).register("paused", { status: "paused" });
  return REGISTRY;
}

// A tenant's run object as the runtime builds it: named by the tenant, its container a fake
// that logs, in order, each interception registered and each start.
async function runObject(name, bindings = EVERY_BINDING) {
  const env = { ...bindings, REGISTRY: await registry() };
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
    },
    waitUntil() {},
    blockConcurrencyWhile: (run) => run(),
  };
  return { object: new TenantContainer(ctx, env), container, order };
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

  it("registers both gateways for its own tenant before the container starts", async () => {
    const { object, container, order } = await runObject("bravo");

    expect(await object.startRun({ period: "morning", date: "2026-11-18" })).toEqual({
      started: true,
      tenant: "bravo",
    });
    expect(order).toEqual([
      { intercept: DB_HOST, props: { tenant: "bravo" } },
      { intercept: OPS_HOST, props: { tenant: "bravo" } },
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

  it("retries a start no instance can be given for up to 90 seconds", () => {
    expect(START_RETRY.maxWaitMs).toBe(90_000);
    expect(START_RETRY.backoffMs).toEqual([1_000, 2_000, 4_000, 8_000, 10_000]);
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
      now: AT,
    });
    return { db, answer, TENANT_DB };
  };

  it("writes the schema, every starting value under the seed's own, and the tenant's feeds", async () => {
    const { db, answer } = await seeded("alpha");

    const settings = Object.fromEntries(
      (await db.query("SELECT key, value, updated_at FROM settings ORDER BY key")).rows.map((row) => [
        row.key,
        [JSON.parse(row.value), row.updated_at],
      ]),
    );
    const expected = seedSettings(STARTING_VALUES, SEED);
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
      latest_page: null,
    });
  });
});

const TEAM = "team.example";
const AUD = "AUD-HOSTED";
const { privateKey, publicKey } = await generateKeyPair("RS256");
const JWK = { ...(await exportJWK(publicKey)), kid: "k1", alg: "RS256", use: "sig" };
const ACCESS_KEYS = { keySet: createLocalJWKSet({ keys: [JWK] }), issuer: `https://${TEAM}` };
const ENV = { CYRIS_ACCESS_TEAM_DOMAIN: TEAM, CYRIS_ACCESS_AUD: AUD };

const accessJwt = () =>
  new SignJWT({})
    .setProtectedHeader({ alg: "RS256", kid: "k1" })
    .setIssuer(`https://${TEAM}`)
    .setAudience(AUD)
    .setIssuedAt()
    .setExpirationTime(Math.floor(Date.now() / 1000) + 3600)
    .sign(privateKey);

async function operator(method, path, { signedIn = true } = {}) {
  const headers = signedIn ? { "Cf-Access-Jwt-Assertion": await accessJwt() } : {};
  return new Request(`https://cyris-hosted.example.workers.dev${path}`, { method, headers });
}

// What the registry answers for each tenant the routes are asked about.
const REGISTERED = {
  alpha: { id: "alpha", status: "active" },
  bravo: { id: "bravo", status: "active" },
  paused: { id: "paused", status: "paused" },
};

function hostedDeps({ timeZone = "UTC" } = {}) {
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
    tenant: async (tenant) => REGISTERED[tenant] ?? null,
    seed: record("seed", { seeded: true }),
    report: record("report", { reported: true }),
    timeZone: record("timeZone", timeZone),
    startRun: record("startRun", { started: true }),
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

  it("seed, report and start a run for a named tenant", async () => {
    const deps = hostedDeps();

    const seeded = await handleHostedRequest(await operator("POST", "/hosted/tenants/alpha/seed"), ENV, deps);
    const reported = await handleHostedRequest(await operator("GET", "/hosted/tenants/bravo/report"), ENV, deps);
    const run = await operator("POST", "/hosted/tenants/bravo/run?period=evening");
    const ran = await handleHostedRequest(run, ENV, deps);

    expect([seeded.status, reported.status, ran.status]).toEqual([200, 200, 200]);
    expect(await ran.json()).toEqual({ started: true });
    expect(deps.calls).toEqual([
      ["seed", "alpha"],
      ["report", "bravo"],
      ["startRun", "bravo", "evening", undefined],
    ]);
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
    expect((await run("?period=morning&date=2026-11-18")).status).toBe(200);
    expect(deps.calls).toEqual([
      ["timeZone", "alpha"],
      ["timeZone", "alpha"],
      ["startRun", "alpha", "morning", "2026-11-18"],
    ]);
  });

  it("refuse a dated run when the tenant's time zone cannot be read", async () => {
    const deps = hostedDeps({ timeZone: null });
    const response = await handleHostedRequest(
      await operator("POST", "/hosted/tenants/alpha/run?period=morning&date=2026-11-18"),
      ENV,
      deps,
    );

    expect(response.status).toBe(503);
    expect(deps.calls.map(([name]) => name)).toEqual(["timeZone"]);
  });
});
