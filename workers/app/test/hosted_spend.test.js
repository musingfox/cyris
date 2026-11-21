import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { env } from "cloudflare:workers";
import { exportJWK, generateKeyPair, SignJWT } from "jose";
import worker from "../src/hosted.js";
import { DEFAULT_MONTHLY_AI_BUDGET_USD, MAX_MONTHLY_AI_BUDGET_USD, spendMonth } from "../src/ai_budget.js";
import { SPEND_HOST, SpendGateway } from "../src/hosted_spend.js";
import { REGISTRY_NAME, TenantRegistry } from "../src/tenant_registry.js";
import { alarmStorage, namespace, registryNamespace, registryObject, sqlStorage } from "./sql_storage.js";

const NOV = new Date("2026-11-18T06:00:00Z");
// The last second of November and the first of December, in UTC.
const NOV_END = new Date("2026-11-30T23:59:59Z");
const DEC = new Date("2026-12-01T00:00:00Z");

// One Haiku 5.5 run as measured on 2026-11-13, and an embedding beside it.
// Each run's report carries an id of its own, as `SpendLedger` draws one per run.
const report = (spend) => ({ report_id: crypto.randomUUID().replaceAll("-", ""), ...spend });

const RUN = { llm_usd: 0.0168, embedding_usd: 0.003, input_tokens: 68_393, output_tokens: 20_052, unpriced_tokens: 0 };

const registered = (...ids) => {
  const registry = registryObject();
  for (const id of ids) registry.register(id, { now: NOV });
  return registry;
};

describe("the registry's AI spend ledger", () => {
  it("charges each run to its own tenant's month and keeps tenants apart", () => {
    const registry = registered("alpha", "bravo");

    registry.addSpend("alpha", report(RUN), { now: NOV });
    registry.addSpend("alpha", report(RUN), { now: NOV });
    registry.addSpend("bravo", report({ llm_usd: 0.5 }), { now: NOV });

    expect(registry.budget("alpha", { now: NOV })).toMatchObject({
      tenant: "alpha",
      month: "2026-11",
      budget_usd: DEFAULT_MONTHLY_AI_BUDGET_USD,
      runs: 2,
      input_tokens: 2 * RUN.input_tokens,
      output_tokens: 2 * RUN.output_tokens,
      spent: false,
    });
    expect(registry.budget("alpha", { now: NOV }).llm_usd).toBeCloseTo(2 * RUN.llm_usd, 10);
    expect(registry.budget("alpha", { now: NOV }).embedding_usd).toBeCloseTo(2 * RUN.embedding_usd, 10);
    expect(registry.budget("alpha", { now: NOV }).cost_usd).toBeCloseTo(2 * (RUN.llm_usd + RUN.embedding_usd), 10);
    expect(registry.budget("bravo", { now: NOV })).toMatchObject({ runs: 1, cost_usd: 0.5, llm_usd: 0.5, embedding_usd: 0 });
  });

  it("lists every registered tenant's month for the operator, a tenant that spent nothing with zeros", () => {
    const registry = registered("alpha", "bravo", "charlie");
    registry.register("charlie", { status: "paused", now: NOV });
    registry.addSpend("bravo", report(RUN), { now: NOV });

    const listed = registry.spend({ now: NOV });

    expect(listed.month).toBe("2026-11");
    expect(listed.tenants.map(({ tenant, status, runs }) => [tenant, status, runs])).toEqual([
      ["alpha", "active", 0],
      ["bravo", "active", 1],
      ["charlie", "paused", 0],
    ]);
    expect(listed.tenants[0]).toMatchObject({ cost_usd: 0, spent: false, budget_usd: DEFAULT_MONTHLY_AI_BUDGET_USD });
  });

  it("calls the allowance spent once the month's spend reaches it, and an allowance of 0 always spent", () => {
    const registry = registered("alpha", "bravo");
    registry.setBudget("alpha", "0.05", { now: NOV });
    registry.setBudget("bravo", "0", { now: NOV });

    registry.addSpend("alpha", report({ llm_usd: 0.03 }), { now: NOV });
    expect(registry.budget("alpha", { now: NOV }).spent).toBe(false);
    registry.addSpend("alpha", report({ llm_usd: 0.02 }), { now: NOV });
    expect(registry.budget("alpha", { now: NOV })).toMatchObject({ budget_usd: 0.05, spent: true });
    expect(registry.budget("bravo", { now: NOV })).toMatchObject({ budget_usd: 0, cost_usd: 0, spent: true });
  });

  it("starts every tenant's allowance again in a new UTC month, and keeps the last month's ledger", () => {
    const registry = registered("alpha");
    registry.addSpend("alpha", report({ llm_usd: DEFAULT_MONTHLY_AI_BUDGET_USD }), { now: NOV });
    expect(registry.budget("alpha", { now: NOV_END }).spent).toBe(true);

    expect(registry.budget("alpha", { now: DEC })).toMatchObject({ month: "2026-12", cost_usd: 0, runs: 0, spent: false });
    // A run reported in December is December's, whenever it started.
    registry.addSpend("alpha", report(RUN), { now: DEC });
    expect(registry.budget("alpha", { now: DEC }).runs).toBe(1);
    expect(registry.spend({ month: "2026-11", now: DEC }).tenants[0]).toMatchObject({
      month: "2026-11",
      cost_usd: DEFAULT_MONTHLY_AI_BUDGET_USD,
      spent: true,
    });
    expect(spendMonth(NOV_END)).toBe("2026-11");
    expect(spendMonth(DEC)).toBe("2026-12");
  });

  it("counts a run's Clef questions in its month's total, so they spend the allowance too", () => {
    const registry = registered("alpha");
    registry.setBudget("alpha", "0.05", { now: NOV });

    registry.addSpend("alpha", report({ llm_usd: 0.03, clef_usd: 0.02 }), { now: NOV });

    expect(registry.budget("alpha", { now: NOV })).toMatchObject({ clef_usd: 0.02, llm_usd: 0.03, spent: true });
    expect(registry.budget("alpha", { now: NOV }).cost_usd).toBeCloseTo(0.05, 10);
    expect(registry.addSpend("alpha", report({ clef_usd: -0.01 }), { now: NOV }).refused).toMatch(/clef_usd/);
  });

  it("refuses a report that would credit the month, or is not numbers, and writes nothing", () => {
    const registry = registered("alpha");
    const refused = [{ llm_usd: -1 }, { embedding_usd: "0.1" }, { input_tokens: 1.5 }, { llm_usd: Infinity }].map(report);
    for (const bad of [...refused, null, { ...RUN }, { ...RUN, report_id: "not-a-run's" }]) {
      expect(registry.addSpend("alpha", bad, { now: NOV }).refused, JSON.stringify(bad)).toBeTruthy();
    }
    expect(registry.addSpend("not a tenant!", report(RUN), { now: NOV }).refused).toBeTruthy();
    expect(registry.ctx.storage.sql.exec("SELECT count(*) AS n FROM spend_rollup").one().n).toBe(0);
  });

  it("adds a report its run retried once, and keeps its id a day, per tenant", () => {
    const registry = registered("alpha", "bravo");
    const once = report(RUN);

    expect(registry.addSpend("alpha", once, { now: NOV }).repeated).toBe(false);
    // The post timed out after the registry wrote it, and the run posted it again.
    expect(registry.addSpend("alpha", once, { now: NOV }).repeated).toBe(true);
    expect(registry.budget("alpha", { now: NOV })).toMatchObject({ runs: 1, input_tokens: RUN.input_tokens });
    // Another tenant naming that id is charged all the same: it cannot drop alpha's, or its own.
    const halfDayLater = new Date(NOV.getTime() + 12 * 60 * 60 * 1000);
    expect(registry.addSpend("bravo", once, { now: halfDayLater }).repeated).toBe(false);
    expect(registry.budget("bravo", { now: NOV }).runs).toBe(1);

    // A day on, alpha's id is dropped as the next report arrives, and bravo's, younger, kept.
    const dayLater = new Date(NOV.getTime() + 24 * 60 * 60 * 1000 + 1000);
    registry.addSpend("alpha", report(RUN), { now: dayLater });
    const kept = registry.ctx.storage.sql.exec("SELECT tenant_id FROM spend_reports WHERE report_id = ?", once.report_id).toArray();
    expect(kept).toEqual([{ tenant_id: "bravo" }]);
  });

  it("charges a tenant the registry no longer holds, since what its run spent was spent", () => {
    const registry = registryObject();

    expect(registry.addSpend("gone", report(RUN), { now: NOV }).spend).toMatchObject({ tenant: "gone", runs: 1 });
    expect(registry.budget("gone", { now: NOV })).toBeNull();
  });

  it("keeps the default allowance in its constant alone, never in a tenant's row or the column", () => {
    const registry = registered("alpha", "bravo");
    registry.setBudget("bravo", String(DEFAULT_MONTHLY_AI_BUDGET_USD), { now: NOV });
    const sql = registry.ctx.storage.sql;

    // No SQL default to freeze 2.25 into every row: a release that changes the constant changes
    // every tenant the operator has not set, and a set allowance is told from the default.
    const [column] = sql.exec("SELECT dflt_value FROM pragma_table_info('tenants') WHERE name = 'monthly_ai_budget_usd'").toArray();
    expect(column.dflt_value).toBeNull();
    expect(registry.tenant("alpha").monthly_ai_budget_usd).toBeNull();
    expect(registry.budget("alpha", { now: NOV })).toMatchObject({ budget_usd: DEFAULT_MONTHLY_AI_BUDGET_USD, budget_set: false });
    expect(registry.budget("bravo", { now: NOV })).toMatchObject({ budget_usd: DEFAULT_MONTHLY_AI_BUDGET_USD, budget_set: true });
  });

  it("takes an allowance only from 0 to the bound, for a registered tenant", () => {
    const registry = registered("alpha");

    for (const usd of [null, "", "-1", "abc", String(MAX_MONTHLY_AI_BUDGET_USD + 1)]) {
      expect(registry.setBudget("alpha", usd, { now: NOV }).refused, String(usd)).toBeTruthy();
    }
    expect(registry.setBudget("bravo", "1", { now: NOV }).refused).toMatch(/not a registered tenant/);
    expect(registry.setBudget("alpha", "3.5", { now: NOV }).tenant).toMatchObject({ monthly_ai_budget_usd: 3.5 });
  });

  it("gains the ledger's columns in place on a registry the first hosted Worker created, keeping its rows", () => {
    const sql = sqlStorage();
    sql.exec(
      "CREATE TABLE spend_rollup (tenant_id TEXT NOT NULL, month TEXT NOT NULL, cost_usd REAL NOT NULL DEFAULT 0, runs INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, PRIMARY KEY (tenant_id, month))",
    );
    sql.exec("INSERT INTO spend_rollup VALUES ('alpha', '2026-11', 0.25, 3, '2026-11-18T06:00:00+00:00')");

    const registry = new TenantRegistry({ storage: alarmStorage(sql) }, {});
    registry.register("alpha", { now: NOV });
    registry.addSpend("alpha", report({ llm_usd: 0.25 }), { now: NOV });

    expect(registry.budget("alpha", { now: NOV })).toMatchObject({ cost_usd: 0.5, llm_usd: 0.25, runs: 4 });
  });
});

describe("SpendGateway", () => {
  let registryEnv;
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(NOV);
    registryEnv = { REGISTRY: registryNamespace() };
    registryEnv.REGISTRY.getByName(REGISTRY_NAME).register("alpha", { now: NOV });
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  const gateway = (props) => new SpendGateway({ props }, registryEnv);
  const request = (path, init) => new Request(`http://${SPEND_HOST}${path}`, init);
  const post = (path, body) => request(path, { method: "POST", body: JSON.stringify(body) });

  it("answers its own tenant's month and charges its run's spend to it", async () => {
    vi.spyOn(console, "log").mockImplementation(() => {});
    const alpha = gateway({ tenant: "alpha" });

    const before = await alpha.fetch(request("/t/alpha/budget"));
    expect(before.status).toBe(200);
    expect(await before.json()).toMatchObject({ tenant: "alpha", month: "2026-11", cost_usd: 0, spent: false });

    const charged = await alpha.fetch(post("/t/alpha/spend", report(RUN)));
    expect(charged.status).toBe(202);
    expect(await charged.json()).toMatchObject({ tenant: "alpha", runs: 1, spent: false });
    expect(console.log).toHaveBeenCalledWith("ai spend", expect.objectContaining({ tenant: "alpha", month: "2026-11" }));
  });

  it("refuses another tenant's path, any other shape, and a gateway bound to no tenant, charging nothing", async () => {
    const alpha = gateway({ tenant: "alpha" });
    for (const refused of [
      post("/t/bravo/spend", report(RUN)),
      request("/t/bravo/budget"),
      request("/t/alpha/spend"),
      post("/t/alpha/budget", {}),
      post("/t/alpha/query", { sql: "SELECT 1" }),
      request("/registry/spend"),
    ]) {
      expect((await alpha.fetch(refused)).status, refused.url).toBe(403);
    }
    expect((await gateway(undefined).fetch(post("/t/alpha/spend", report(RUN)))).status).toBe(403);
    expect(registryEnv.REGISTRY.getByName(REGISTRY_NAME).budget("alpha").runs).toBe(0);
  });

  it("refuses a body that is not a spend report with 400", async () => {
    vi.spyOn(console, "log").mockImplementation(() => {});
    const alpha = gateway({ tenant: "alpha" });

    expect((await alpha.fetch(request("/t/alpha/spend", { method: "POST", body: "not json" }))).status).toBe(400);
    expect((await alpha.fetch(post("/t/alpha/spend", report({ llm_usd: -2.25 })))).status).toBe(400);
    expect(registryEnv.REGISTRY.getByName(REGISTRY_NAME).budget("alpha").runs).toBe(0);
  });
});

describe("the operator's spend routes", () => {
  const AUD = "AUD-SPEND";
  // The service token the operator signs in with: its Client ID, which Access signs in as `common_name`.
  const OPERATOR = "operator-token.access";
  let privateKey;
  let bound;

  beforeEach(async () => {
    const keys = await generateKeyPair("RS256");
    privateKey = keys.privateKey;
    const jwk = { ...(await exportJWK(keys.publicKey)), kid: "k1", alg: "RS256", use: "sig" };
    vi.stubGlobal("fetch", async () => Response.json({ keys: [jwk] }));
    bound = { TENANT_DB: namespace(), TENANT_RUN: { getByName: () => ({}) } };
    bound.REGISTRY = registryNamespace(bound);
    Object.assign(env, bound, { CYRIS_ACCESS_TEAM_DOMAIN: `${crypto.randomUUID()}.spend-team.example`, CYRIS_ACCESS_AUD: AUD, CYRIS_HOSTED_OPERATORS: OPERATOR });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    for (const key of Object.keys(env)) delete env[key];
  });

  const signIn = () =>
    new SignJWT({ common_name: OPERATOR })
      .setProtectedHeader({ alg: "RS256", kid: "k1" })
      .setIssuer(`https://${env.CYRIS_ACCESS_TEAM_DOMAIN}`)
      .setAudience(AUD)
      .setIssuedAt()
      .setExpirationTime(Math.floor(Date.now() / 1000) + 3600)
      .sign(privateKey);
  const operator = async (method, path, headers = {}) =>
    worker.fetch(
      new Request(`https://cyris-hosted.example.workers.dev${path}`, {
        method,
        headers: { "Cf-Access-Jwt-Assertion": await signIn(), ...headers },
      }),
    );
  const registry = () => bound.REGISTRY.getByName(REGISTRY_NAME);

  it("list every tenant's spend this month, or a named month", async () => {
    registry().register("alpha");
    registry().register("bravo");
    registry().addSpend("bravo", report(RUN));
    registry().addSpend("alpha", report(RUN), { now: new Date("2026-10-15T00:00:00Z") });

    const now = await operator("GET", "/hosted/spend");
    expect(now.status).toBe(200);
    const listed = await now.json();
    expect(listed.month).toBe(spendMonth(new Date()));
    expect(listed.tenants.map(({ tenant, runs }) => [tenant, runs])).toEqual([
      ["alpha", 0],
      ["bravo", 1],
    ]);

    const october = await (await operator("GET", "/hosted/spend?month=2026-10")).json();
    expect(october.tenants.map(({ tenant, runs }) => [tenant, runs])).toEqual([
      ["alpha", 1],
      ["bravo", 0],
    ]);
    expect((await operator("GET", "/hosted/spend?month=2026-13")).status).toBe(400);
    expect((await operator("POST", "/hosted/spend")).status).toBe(405);
  });

  it("refuse the list without the Access JWT", async () => {
    const response = await worker.fetch(new Request("https://cyris-hosted.example.workers.dev/hosted/spend"));

    expect(response.status).toBe(403);
  });

  it("set a registered tenant's allowance, refuse a bad one or another site's post, and show it in the report", async () => {
    registry().register("alpha");

    const set = await operator("POST", "/hosted/tenants/alpha/budget?usd=4");
    expect(set.status).toBe(200);
    expect(await set.json()).toMatchObject({ id: "alpha", monthly_ai_budget_usd: 4 });
    expect((await operator("POST", "/hosted/tenants/alpha/budget?usd=-1")).status).toBe(400);
    expect((await operator("POST", "/hosted/tenants/bravo/budget?usd=1")).status).toBe(404);
    const crossSite = await operator("POST", "/hosted/tenants/alpha/budget?usd=0", { "Sec-Fetch-Site": "cross-site" });
    expect(crossSite.status).toBe(403);
    expect(registry().tenant("alpha").monthly_ai_budget_usd).toBe(4);

    const report = await (await operator("GET", "/hosted/tenants/alpha/report")).json();
    expect(report.spend).toMatchObject({ tenant: "alpha", budget_usd: 4, cost_usd: 0, spent: false });
  });
});
