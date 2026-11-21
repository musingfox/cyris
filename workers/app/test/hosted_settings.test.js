import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";
import { createHash } from "node:crypto";
import { env } from "cloudflare:workers";
import { SignJWT, exportJWK, generateKeyPair } from "jose";
import EN from "../../../src/cyris/locales/en.json" with { type: "json" };
import ZH_HANT from "../../../src/cyris/locales/zh-Hant.json" with { type: "json" };
import FEED_CATALOG from "../../../src/cyris/onboarding_feeds.json" with { type: "json" };
import STARTING_VALUES from "../../../src/cyris/settings_starting_values.json" with { type: "json" };
import SEED from "../src/hosted_seed.json" with { type: "json" };
import POLICY from "../src/hosted_settings_policy.json" with { type: "json" };
import worker from "../src/hosted.js";
import { seedTenant } from "../src/hosted_seed.js";
import { CATALOGS, REFUSAL_REASONS, handleTenantSettings } from "../src/hosted_settings.js";
import { WRITABLE, catalogTag, checkSettings, rssSource, urlRefusal } from "../src/hosted_settings_rules.js";
import { SESSION_COOKIE } from "../src/hosted_session.js";
import { tenantRegistry } from "../src/tenant_registry.js";
import CASES from "./hosted_settings_cases.json" with { type: "json" };
import { HOSTED_SCHEMA_SQL, SCHEMA_SQL, namespace, registryNamespace, tenantDb } from "./sql_storage.js";

const DOMAIN = "cyris-hosted.example.workers.dev";
const ORIGIN = `https://${DOMAIN}`;
const AT = new Date("2026-11-22T06:00:00Z");

describe("a tenant's settings save", () => {
  it.each(CASES.settings.accepted)("takes $key = $value", ({ key, value }) => {
    expect(checkSettings({ [key]: value })).toEqual({ rows: [[key, value]], schedule: null });
  });

  it.each(CASES.settings.refused)("refuses $key = $value as $reason", ({ key, value, reason }) => {
    expect(checkSettings({ [key]: value }).refused).toMatchObject({ reason, key });
  });

  it("writes exactly the policy's writable keys", () => {
    expect([...WRITABLE].sort()).toEqual([...POLICY.writable_settings].sort());
  });

  it("takes JSON's own types only: a number in a string is refused", () => {
    expect(checkSettings({ "digest.max_articles_per_digest": "5" }).refused.reason).toBe("whole_number");
    expect(checkSettings({ "digest.rank_by_preference": "true" }).refused.reason).toBe("on_off");
  });

  it("refuses the whole save for one bad key, so nothing of it is written", () => {
    const answer = checkSettings({ "digest.style_prompt": "Short", "llm_provider.provider": "gemini" });

    expect(answer).toEqual({ refused: { reason: "operator_key", key: "llm_provider.provider" } });
  });

  it("takes the schedule apart from the rows, and refuses hours that are not two whole ones", () => {
    expect(checkSettings({ "general.digest_schedule": ["07:00", "19:00"], "general.timezone": "Asia/Taipei" })).toEqual({
      rows: [],
      schedule: { times: ["07:00", "19:00"], timeZone: "Asia/Taipei" },
    });
    for (const times of [["07:00"], ["07:00", "07:00"], ["07:30", "19:00"], "07:00,19:00"]) {
      expect(checkSettings({ "general.digest_schedule": times }).refused.reason).toBe("schedule");
    }
  });

  it("refuses a body that is not an object of settings, and an empty one", () => {
    for (const body of [null, [], "x", 5]) expect(checkSettings(body).refused.reason).toBe("body");
    expect(checkSettings({}).refused.reason).toBe("empty");
  });
});

describe("a tenant's source save", () => {
  const own = [DOMAIN];

  it.each(CASES.sources.written)("writes $body.name as its row", ({ body, row }) => {
    expect(rssSource(body, own)).toEqual({ row });
  });

  it.each(CASES.sources.refused)("refuses $body.name as $reason", ({ body, reason }) => {
    expect(rssSource(body, own).refused.reason).toBe(reason);
  });

  it("takes a URL of the policy's length and no longer", () => {
    const base = "https://example.org/";
    const at = base + "a".repeat(POLICY.source_url.max_length - base.length);

    expect(urlRefusal(at)).toBeNull();
    expect(urlRefusal(`${at}a`)).toEqual({ reason: "url_length", max: String(POLICY.source_url.max_length) });
  });

  it("refuses this Worker's own host, whatever its name", () => {
    expect(urlRefusal("https://digest.example.com/hosted/spend", ["digest.example.com"])).toEqual({ reason: "url_service" });
    expect(urlRefusal("https://digest.example.com/feed")).toBeNull();
  });
});

describe("the catalog a tenant's settings are worded in", () => {
  it.each(CASES.languages)("is $catalog for $tag", ({ tag, catalog }) => {
    expect(catalogTag(tag, Object.keys(CATALOGS))).toBe(catalog);
  });

  it("is every catalog's message for each refusal", () => {
    for (const reason of REFUSAL_REASONS) {
      expect(EN).toHaveProperty(`hosted_settings.refused_${reason}`);
      expect(ZH_HANT).toHaveProperty(`hosted_settings.refused_${reason}`);
    }
  });
});

// TenantDb over real SQLite: a seeded tenant's object.
async function seeded(tenant = "alpha") {
  const { db } = tenantDb();
  await seedTenant({
    db,
    tenant,
    schemaSql: SCHEMA_SQL,
    hostedSchemaSql: HOSTED_SCHEMA_SQL,
    startingValues: STARTING_VALUES,
    seed: SEED,
    catalog: FEED_CATALOG,
  });
  return db;
}

const rss = (name) => ({ name, url: `https://${name.toLowerCase().replaceAll(" ", "-")}.example/feed`, type: "rss", config: { lane: "skim", tags: [] } });

describe("TenantDb's settings and sources", () => {
  it("refuses a key the policy does not let a tenant write, whoever calls it", async () => {
    const db = await seeded();
    const before = db.query("SELECT key, value FROM settings ORDER BY key").rows;

    const answer = db.saveSettings([
      ["digest.style_prompt", "Short"],
      ["llm_provider.provider", "gemini"],
    ]);

    expect(answer).toMatchObject({ refused: "operator_key", key: "llm_provider.provider" });
    expect(db.query("SELECT key, value FROM settings ORDER BY key").rows).toEqual(before);
  });

  it("writes each value as JSON, as the container's store reads one", async () => {
    const db = await seeded();

    db.saveSettings([
      ["digest.max_articles_per_digest", 120],
      ["digest.rank_by_preference", false],
    ]);

    expect(db.settings()).toMatchObject({ "digest.max_articles_per_digest": 120, "digest.rank_by_preference": false });
    expect(db.query("SELECT value FROM settings WHERE key = ?", ["digest.max_articles_per_digest"]).rows[0].value).toBe("120");
  });

  it("shows no key but the writable ones and the schedule's copy", async () => {
    const db = await seeded();

    expect(Object.keys(db.settings()).sort()).toEqual([...POLICY.writable_settings, "general.digest_schedule", "general.timezone"].sort());
  });

  it("lets two saves at once take the last place under the cap, and only one", async () => {
    const db = await seeded();
    const held = db.query("SELECT count(*) AS n FROM sources").rows[0].n;
    for (let i = held; i < POLICY.max_sources - 1; i++) db.saveSource(rss(`Feed ${i}`), POLICY.max_sources);

    const answers = await Promise.all([rss("Last A"), rss("Last B")].map(async (row) => db.saveSource(row, POLICY.max_sources)));

    expect(answers.filter((answer) => answer.source)).toHaveLength(1);
    expect(answers.filter((answer) => answer.refused === "source_cap")).toHaveLength(1);
    expect(db.query("SELECT count(*) AS n FROM sources").rows[0].n).toBe(POLICY.max_sources);
  });

  it("counts newsletter sources against the cap, and still rewrites a source it holds when full", async () => {
    const db = await seeded();
    db.query("INSERT INTO sources (name, url, type, config) VALUES (?, NULL, 'newsletter', ?)", ["Letter", '{"email_match":"from:a@b.example"}']);
    const held = db.query("SELECT count(*) AS n FROM sources").rows[0].n;
    for (let i = held; i < POLICY.max_sources; i++) db.saveSource(rss(`Feed ${i}`), POLICY.max_sources);

    expect(db.saveSource(rss("One more"), POLICY.max_sources).refused).toBe("source_cap");
    expect(db.saveSource({ ...rss("Feed 20"), config: { lane: "read", tags: [] } }, POLICY.max_sources).source).toBeTruthy();
    expect(db.saveSource(rss("Letter"), POLICY.max_sources).refused).toBe("source_taken");
  });

  it("keeps what else a rewritten source's config held, its tags too", async () => {
    const db = await seeded();
    db.query("INSERT INTO sources (name, url, type, config) VALUES (?, ?, 'rss', ?)", ["Kept", "https://kept.example/feed", '{"lane":"skim","tags":["x"],"area_id":3}']);

    db.saveSource(rssSource({ name: "Kept", url: "https://kept.example/new", lane: "read" }).row, POLICY.max_sources);

    expect(db.query("SELECT url, config FROM sources WHERE name = 'Kept'").rows[0]).toEqual({
      url: "https://kept.example/new",
      config: JSON.stringify({ lane: "read", tags: ["x"], area_id: 3 }),
    });
  });

  it("answers a tenant never seeded with no settings and no sources, and writes nothing", () => {
    const { db } = tenantDb();

    expect(db.settings()).toBeNull();
    expect(db.sources()).toBeNull();
    expect(db.saveSettings([["digest.style_prompt", "x"]]).refused).toBe("not_seeded");
    expect(db.saveSource(rss("A"), 30).refused).toBe("not_seeded");
  });
});

// The hosted Worker's entry itself over the real registry and tenant objects, as
// hosted_vote.test.js builds it.
let bound;
let privateKey;
let ctx;

beforeEach(async () => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(AT);
  const keys = await generateKeyPair("RS256");
  privateKey = keys.privateKey;
  const jwk = { ...(await exportJWK(keys.publicKey)), kid: "k1", alg: "RS256", use: "sig" };
  vi.stubGlobal("fetch", async () => Response.json({ keys: [jwk] }));
  const MAIL = {
    sent: [],
    async send(message) {
      this.sent.push(message);
      return { messageId: `m${this.sent.length}` };
    },
  };
  bound = { REGISTRY: registryNamespace(), TENANT_DB: namespace(), MAIL };
  Object.assign(env, bound, {
    CYRIS_ACCESS_TEAM_DOMAIN: `${crypto.randomUUID()}.team.example`,
    CYRIS_ACCESS_AUD: "AUD-SETTINGS",
    CYRIS_HOSTED_OPERATORS: "operator-token.access",
    CYRIS_STORE_HOSTED_DOMAIN: DOMAIN,
    CYRIS_HOSTED_MAIL_FROM: "cyris@mail.example",
  });
  ctx = { pending: [], waitUntil: (promise) => ctx.pending.push(promise) };
  const registry = tenantRegistry(env);
  for (const tenant of ["alpha", "bravo"]) {
    await registry.register(tenant);
    await seedTenant({
      db: bound.TENANT_DB.getByName(tenant),
      tenant,
      schemaSql: SCHEMA_SQL,
      hostedSchemaSql: HOSTED_SCHEMA_SQL,
      startingValues: STARTING_VALUES,
      seed: SEED,
      catalog: FEED_CATALOG,
    });
    await registry.setSchedule(tenant, { times: ["08:00", "20:00"], timeZone: "UTC", dailyRunCap: 3 });
  }
  await registry.invite("alpha", "ana@example.com");
  await registry.invite("bravo", "bea@example.com");
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  for (const key of Object.keys(env)) delete env[key];
});

async function call(path, { method = "GET", session, headers = {}, body, raw, operator = false } = {}) {
  const sent = new Headers(headers);
  if (session) sent.set("Cookie", `${SESSION_COOKIE}=${session}`);
  if (operator) {
    const jwt = await new SignJWT({ common_name: "operator-token.access" })
      .setProtectedHeader({ alg: "RS256", kid: "k1" })
      .setIssuer(`https://${env.CYRIS_ACCESS_TEAM_DOMAIN}`)
      .setAudience("AUD-SETTINGS")
      .setIssuedAt()
      .setExpirationTime(Math.floor(Date.now() / 1000) + 3600)
      .sign(privateKey);
    sent.set("Cf-Access-Jwt-Assertion", jwt);
  }
  let payload = raw;
  if (body !== undefined) {
    sent.set("Content-Type", "application/json");
    payload = JSON.stringify(body);
  }
  const response = await worker.fetch(new Request(`${ORIGIN}${path}`, { method, headers: sent, body: payload }), env, ctx);
  await Promise.all(ctx.pending.splice(0));
  return response;
}

async function signIn(email) {
  const form = { Origin: ORIGIN, "Content-Type": "application/x-www-form-urlencoded" };
  await call("/login", { method: "POST", headers: form, raw: new URLSearchParams({ email }).toString() });
  const token = /\/login\/confirm#([A-Za-z0-9_-]{43})/.exec(bound.MAIL.sent.at(-1).text)[1];
  const confirmed = await call("/login/confirm", { method: "POST", headers: form, raw: new URLSearchParams({ token }).toString() });
  return new RegExp(`^${SESSION_COOKIE}=([^;]*)`).exec(confirmed.headers.get("Set-Cookie"))[1];
}

// A save from the settings page itself: a same-origin fetch, which carries the page's Origin.
const save = (session, path, body, headers = { Origin: ORIGIN }) => call(path, { method: "POST", session, headers, body });

const settingsOf = (tenant) => bound.TENANT_DB.getByName(tenant).settings();
const rowOf = async (tenant, key) =>
  (await bound.TENANT_DB.getByName(tenant).query("SELECT value FROM settings WHERE key = ?", [key])).rows[0]?.value;

describe("GET /t/<tenant>/settings", () => {
  it("is the Worker's own page, not sandboxed, with one hashed script and forms posting to this origin", async () => {
    const session = await signIn("ana@example.com");

    const served = await call("/t/alpha/settings", { session });
    const html = await served.text();

    expect(served.status).toBe(200);
    expect(served.headers.get("Content-Type")).toBe("text/html; charset=utf-8");
    const policy = served.headers.get("Content-Security-Policy");
    expect(policy).not.toContain("sandbox");
    expect(policy).toContain("form-action 'self'");
    expect(policy).toContain("connect-src 'self'");
    expect(policy).not.toContain("'unsafe-inline' ;");
    const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((match) => match[1]);
    expect(scripts).toHaveLength(1);
    const hash = createHash("sha256").update(scripts[0]).digest("base64");
    expect(policy).toContain(`script-src 'sha256-${hash}'`);
    expect(html).toContain('action="api/settings"');
    expect(html).toContain('action="api/sources"');
    expect(html).toContain(EN["settings_field.digest.max_articles_per_digest"]);
    expect(html).not.toContain(EN["settings_field.llm_provider.provider"]);
    expect(html).not.toContain(EN["settings_field.notify.discord_webhook_url"]);
    expect(served.headers.get("X-Robots-Tag")).toBe("noindex, nofollow");
  });

  it("saves with a fetch that sends this Origin, though the page itself is no-referrer", async () => {
    const session = await signIn("ana@example.com");

    const served = await call("/t/alpha/settings", { session });
    const script = /<script>([\s\S]*?)<\/script>/.exec(await served.text())[1];

    expect(served.headers.get("Referrer-Policy")).toBe("no-referrer");
    expect(script).toMatch(/fetch\(form\.action, \{[^}]*referrerPolicy: "same-origin",/);
  });

  it("is worded in the catalog the tenant's output language reaches", async () => {
    const session = await signIn("ana@example.com");
    await save(session, "/t/alpha/api/settings", { "digest.output_language": "zh-TW" });

    const html = await (await call("/t/alpha/settings", { session })).text();

    expect(html).toContain('<html lang="zh-Hant">');
    expect(html).toContain(ZH_HANT["settings_field.digest.max_articles_per_digest"].text);
  });

  it("is another tenant's 404, and a signed-out reader's sign-in", async () => {
    const session = await signIn("bea@example.com");

    expect((await call("/t/alpha/settings", { session })).status).toBe(404);
    expect((await call("/t/alpha/api/settings", { session })).status).toBe(404);
    expect((await call("/t/alpha/settings")).headers.get("Location")).toBe("/login");
    expect((await save(undefined, "/t/alpha/api/settings", { "digest.style_prompt": "x" })).status).toBe(401);
  });

  it("leaves the operator's /settings refused, as every path outside /t/ is to a reader", async () => {
    const session = await signIn("ana@example.com");

    expect((await call("/settings", { session })).status).toBe(403);
  });
});

describe("POST /t/<tenant>/api/settings", () => {
  it("writes the session's tenant's settings, which its next run reads, and no other tenant's", async () => {
    const session = await signIn("ana@example.com");

    const answer = await save(session, "/t/alpha/api/settings", {
      "digest.max_articles_per_digest": 150,
      "digest.style_prompt": "Short.",
      "digest.type_scale": 1.125,
      "routing.score_threshold": 80,
    });

    expect(answer.status).toBe(200);
    expect((await answer.json()).note).toBe("Settings saved. Effective next run.");
    expect(await settingsOf("alpha")).toMatchObject({ "digest.max_articles_per_digest": 150, "digest.style_prompt": "Short.", "digest.type_scale": 1.125 });
    expect(await rowOf("alpha", "routing.score_threshold")).toBe("80");
    expect(await settingsOf("bravo")).toMatchObject({ "digest.max_articles_per_digest": 400, "digest.style_prompt": "" });
  });

  it("names no tenant from the body or the path but the session's", async () => {
    const session = await signIn("ana@example.com");

    const answer = await save(session, "/t/bravo/api/settings", { "digest.style_prompt": "From alpha" });
    const named = await save(session, "/t/alpha/api/settings", { tenant: "bravo", "digest.style_prompt": "x" });

    expect(answer.status).toBe(404);
    expect(named.status).toBe(400);
    expect((await settingsOf("bravo"))["digest.style_prompt"]).toBe("");
    expect((await settingsOf("alpha"))["digest.style_prompt"]).toBe("");
  });

  it.each([
    ["no Origin", {}],
    ["Origin null", { Origin: "null" }],
    ["another site's Origin", { Origin: "https://evil.example" }],
    ["this Origin from another site", { Origin: ORIGIN, "Sec-Fetch-Site": "cross-site" }],
  ])("refuses a save with %s, writing nothing", async (_name, headers) => {
    const session = await signIn("ana@example.com");

    for (const [path, body] of [
      ["/t/alpha/api/settings", { "digest.style_prompt": "Forged" }],
      ["/t/alpha/api/sources", { name: "Forged", url: "https://forged.example/feed" }],
      ["/t/alpha/api/sources/delete", { name: "Hacker News" }],
    ]) {
      const answer = await save(session, path, body, headers);
      expect(answer.status).toBe(403);
      expect((await answer.json()).error).toBe(EN["hosted_settings.refused_cross_origin"]);
    }
    expect((await settingsOf("alpha"))["digest.style_prompt"]).toBe("");
    expect((await bound.TENANT_DB.getByName("alpha").sources()).map((source) => source.name)).toContain("Hacker News");
    expect((await bound.TENANT_DB.getByName("alpha").sources()).map((source) => source.name)).not.toContain("Forged");
  });

  it.each([
    ["llm_provider.provider", "gemini", "operator_key"],
    ["llm_provider.model", "claude-opus-5-5", "operator_key"],
    ["notify.email_to", "x@example.com", "operator_key"],
    ["notify.discord_webhook_url", "https://discord.com/api/webhooks/1/x", "operator_key"],
    ["daily_run_cap", 24, "operator_key"],
    ["monthly_ai_budget_usd", 100, "operator_key"],
    ["digest.colour", "red", "unknown_key"],
    ["__proto__", "x", "unknown_key"],
    ["constructor", "x", "unknown_key"],
    ["hasOwnProperty", "x", "unknown_key"],
    ["toString", "x", "unknown_key"],
    ["valueOf", "x", "unknown_key"],
  ])("refuses %s with a catalog message naming why", async (key, value, reason) => {
    const session = await signIn("ana@example.com");
    const before = await bound.TENANT_DB.getByName("alpha").query("SELECT key, value FROM settings ORDER BY key", []);

    const answer = await save(session, "/t/alpha/api/settings", { "digest.style_prompt": "x", [key]: value });

    expect(answer.status).toBe(400);
    const message = EN[`hosted_settings.refused_${reason}`].replace("{$key}", key);
    expect(await answer.json()).toMatchObject({ ok: false, reason, error: message });
    expect(await bound.TENANT_DB.getByName("alpha").query("SELECT key, value FROM settings ORDER BY key", [])).toEqual(before);
  });

  it("refuses an article cap above the policy's, naming the field and its bounds", async () => {
    const session = await signIn("ana@example.com");

    const answer = await save(session, "/t/alpha/api/settings", { "digest.max_articles_per_digest": POLICY.max_articles_per_digest + 1 });

    expect(answer.status).toBe(400);
    expect((await answer.json()).error).toBe(`${EN["settings_field.digest.max_articles_per_digest"]} is a whole number from 1 to ${POLICY.max_articles_per_digest}.`);
  });

  it("words a refusal in the tenant's own catalog", async () => {
    const session = await signIn("ana@example.com");
    await save(session, "/t/alpha/api/settings", { "digest.output_language": "zh-Hant" });

    const answer = await save(session, "/t/alpha/api/settings", { "llm_provider.provider": "gemini" });

    expect((await answer.json()).error).toBe(ZH_HANT["hosted_settings.refused_operator_key"].text.replace("{$key}", "llm_provider.provider"));
  });

  it("writes the schedule through the registry, keeps the operator's run cap, and copies it to the tenant", async () => {
    const session = await signIn("ana@example.com");

    const answer = await save(session, "/t/alpha/api/settings", { "general.digest_schedule": ["06:00", "18:00"], "general.timezone": "Asia/Taipei" });

    expect(answer.status).toBe(200);
    const row = await tenantRegistry(env).tenant("alpha");
    expect(JSON.parse(row.digest_times)).toEqual(["06:00", "18:00"]);
    expect(row.timezone).toBe("Asia/Taipei");
    expect(row.daily_run_cap).toBe(3);
    expect(await settingsOf("alpha")).toMatchObject({ "general.digest_schedule": ["06:00", "18:00"], "general.timezone": "Asia/Taipei" });
    expect(JSON.parse((await tenantRegistry(env).tenant("bravo")).digest_times)).toEqual(["08:00", "20:00"]);
  });

  it("keeps the zone a save of the hours alone does not name, and refuses a zone the registry would not", async () => {
    const session = await signIn("ana@example.com");

    await save(session, "/t/alpha/api/settings", { "general.digest_schedule": ["07:00", "19:00"] });
    const zone = await save(session, "/t/alpha/api/settings", { "general.timezone": "+08:00" });
    const hours = await save(session, "/t/alpha/api/settings", { "general.digest_schedule": ["07:30", "19:00"] });

    expect((await tenantRegistry(env).tenant("alpha")).timezone).toBe("UTC");
    expect(JSON.parse((await tenantRegistry(env).tenant("alpha")).digest_times)).toEqual(["07:00", "19:00"]);
    expect(zone.status).toBe(400);
    expect((await zone.json()).reason).toBe("time_zone");
    expect((await hours.json()).reason).toBe("schedule");
  });

  it("refuses a body over the policy's size unread, and one that is not JSON", async () => {
    const session = await signIn("ana@example.com");

    const big = await save(session, "/t/alpha/api/settings", { "digest.style_prompt": "x".repeat(POLICY.max_body_bytes) });
    const junk = await call("/t/alpha/api/settings", { method: "POST", session, headers: { Origin: ORIGIN }, raw: "digest.style_prompt=x" });

    expect(big.status).toBe(413);
    expect(junk.status).toBe(400);
    expect((await junk.json()).error).toBe(EN["hosted_settings.refused_body"]);
  });

  it("answers GET with the writable settings, the registry's schedule and the caps", async () => {
    const session = await signIn("ana@example.com");

    const answer = await (await call("/t/alpha/api/settings", { session })).json();

    expect(Object.keys(answer.settings).sort()).toEqual([...POLICY.writable_settings].sort());
    expect(answer.schedule).toEqual({ times: ["08:00", "20:00"], timeZone: "UTC" });
    expect(answer.max_articles_per_digest).toBe(POLICY.max_articles_per_digest);
    expect(answer.max_sources).toBe(POLICY.max_sources);
  });
});

// A save straight to the settings handler over a seeded tenant's object, with a registry whose
// schedule writes the test sees and can refuse.
describe("a save that writes settings rows and the schedule", () => {
  const SCHEDULE = { times: ["08:00", "20:00"], timeZone: "UTC" };
  const body = { "digest.style_prompt": "Short.", "general.digest_schedule": ["06:00", "18:00"], "general.timezone": "Asia/Taipei" };
  const post = (deps) =>
    handleTenantSettings(
      new Request(`${ORIGIN}/t/alpha/api/settings`, { method: "POST", headers: { Origin: ORIGIN, "Content-Type": "application/json" }, body: JSON.stringify(body) }),
      { rest: "/api/settings", registryRow: () => ({ digest_times: JSON.stringify(SCHEDULE.times), timezone: SCHEDULE.timeZone }), ...deps },
    );

  it("writes no row when the registry refuses the schedule", async () => {
    const db = await seeded();
    const before = db.query("SELECT key, value FROM settings ORDER BY key").rows;

    const answer = await post({ db, setSchedule: async () => ({ refused: "alpha is not a registered tenant" }) });

    expect(answer.status).toBe(400);
    expect(db.query("SELECT key, value FROM settings ORDER BY key").rows).toEqual(before);
  });

  it("puts the registry's schedule back when the rows are refused", async () => {
    const db = await seeded();
    const written = [];
    const refusing = { settings: () => db.settings(), saveSettings: () => ({ refused: "not_seeded", status: 409 }) };

    const answer = await post({ db: refusing, setSchedule: async (schedule) => (written.push(schedule), {}) });

    expect(answer.status).toBe(409);
    expect(written).toEqual([{ times: ["06:00", "18:00"], timeZone: "Asia/Taipei" }, SCHEDULE]);
  });
});

describe("POST /t/<tenant>/api/sources", () => {
  it("adds an RSS source to the session's tenant, and removes one", async () => {
    const session = await signIn("ana@example.com");

    const added = await save(session, "/t/alpha/api/sources", { name: "Example", url: "https://example.org/feed.xml", lane: "read" });
    const removed = await save(session, "/t/alpha/api/sources/delete", { name: "Hacker News" });

    expect(added.status).toBe(200);
    expect(removed.status).toBe(200);
    const names = (await (await call("/t/alpha/api/sources", { session })).json()).sources.map((source) => source.name);
    expect(names).toContain("Example");
    expect(names).not.toContain("Hacker News");
    expect((await bound.TENANT_DB.getByName("bravo").sources()).map((source) => source.name)).not.toContain("Example");
  });

  it("rewrites a source of the name it names, keeping its tags", async () => {
    const session = await signIn("ana@example.com");
    const db = bound.TENANT_DB.getByName("alpha");
    await db.query("INSERT INTO sources (name, url, type, config) VALUES (?, ?, 'rss', ?)", ["Tagged", "https://tagged.example/feed", '{"lane":"skim","tags":["ai","policy"]}']);

    const answer = await save(session, "/t/alpha/api/sources", { name: "Tagged", url: "https://tagged.example/rss", lane: "read" });

    expect(answer.status).toBe(200);
    const row = (await db.query("SELECT url, config FROM sources WHERE name = 'Tagged'", [])).rows[0];
    expect(row.url).toBe("https://tagged.example/rss");
    expect(JSON.parse(row.config)).toEqual({ lane: "read", tags: ["ai", "policy"] });
  });

  it("refuses a URL the write rules refuse, with the catalog's reason", async () => {
    const session = await signIn("ana@example.com");

    const own = await save(session, "/t/alpha/api/sources", { name: "Own", url: `${ORIGIN}/hosted/spend` });
    const metadata = await save(session, "/t/alpha/api/sources", { name: "Meta", url: "http://169.254.169.254/latest" });

    expect(own.status).toBe(400);
    expect((await own.json()).error).toBe(EN["hosted_settings.refused_url_service"]);
    expect((await metadata.json()).error).toBe(EN["hosted_settings.refused_url_host"]);
  });

  it("refuses a source past the policy's cap, newsletters counted", async () => {
    const session = await signIn("ana@example.com");
    const db = bound.TENANT_DB.getByName("alpha");
    await db.query("INSERT INTO sources (name, url, type, config) VALUES ('Letter', NULL, 'newsletter', '{}')", []);
    for (let i = (await db.sources()).length; i < POLICY.max_sources; i++) {
      expect((await save(session, "/t/alpha/api/sources", { name: `Feed ${i}`, url: `https://feed${i}.example/rss` })).status).toBe(200);
    }

    const past = await save(session, "/t/alpha/api/sources", { name: "One more", url: "https://more.example/rss" });

    expect(past.status).toBe(409);
    expect((await past.json()).error).toBe(EN["hosted_settings.refused_source_cap"].replace("{$max}", String(POLICY.max_sources)));
    expect(await db.sources()).toHaveLength(POLICY.max_sources);
  });

  it("answers a missing source's removal with 404", async () => {
    const session = await signIn("ana@example.com");

    const answer = await save(session, "/t/alpha/api/sources/delete", { name: "Nothing" });

    expect(answer.status).toBe(404);
    expect((await answer.json()).error).toBe(EN["hosted_settings.refused_source_missing"].replace("{$name}", "Nothing"));
  });
});
