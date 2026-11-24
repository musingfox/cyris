import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { env } from "cloudflare:workers";
import FEED_CATALOG from "../../../src/cyris/onboarding_feeds.json" with { type: "json" };
import EN from "../../../src/cyris/locales/en.json" with { type: "json" };
import ZH_HANT from "../../../src/cyris/locales/zh-Hant.json" with { type: "json" };
import STARTING_VALUES from "../../../src/cyris/settings_starting_values.json" with { type: "json" };
import SEED from "../src/hosted_seed.json" with { type: "json" };
import POLICY from "../src/hosted_settings_policy.json" with { type: "json" };
import BUILT from "../src/shared_settings.build.json" with { type: "json" };
import RULES from "../../../src/cyris/feed_health_rules.json" with { type: "json" };
import worker from "../src/hosted.js";
import { seedTenant } from "../src/hosted_seed.js";
import { QUIET_DAYS, UNHEALTHY_FAILURE_STREAK, findings } from "../src/hosted_feed_health.js";
import { refusalText, translator } from "../src/hosted_settings.js";
import { checkSettings } from "../src/hosted_settings_rules.js";
import { handleSharedSettings, hostedValues, isSharedPath, sharedPagePolicy } from "../src/hosted_shared_settings.js";
import { SESSION_COOKIE } from "../src/hosted_session.js";
import { tenantRegistry } from "../src/tenant_registry.js";
import CASES from "./shared_settings_cases.json" with { type: "json" };
import RULE_CASES from "./hosted_settings_cases.json" with { type: "json" };
import { HOSTED_SCHEMA_SQL, SCHEMA_SQL, namespace, registryNamespace, tenantDb } from "./sql_storage.js";

// The shared settings page on the hosted Worker (ADR-0043): built from settings.html.j2, served
// to a session under its own policy, and answering the contract TriageServer answers, from the
// cases tests/test_shared_settings_cases.py runs against TriageServer.

const DOMAIN = "cyris-hosted.example.workers.dev";
const ORIGIN = `https://${DOMAIN}`;
const STATIC_DIR = new URL("../../../src/cyris/entrypoints/static/", import.meta.url);

let bound;
let ctx;

beforeEach(async () => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-11-24T06:00:00Z"));
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
    CYRIS_ACCESS_AUD: "AUD-SHARED",
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
      schedule: { times: ["08:00"], timeZone: "UTC" },
    });
    await registry.setSchedule(tenant, { times: ["07:00"], timeZone: "Asia/Taipei" });
  }
  await registry.invite("alpha", "ana@example.com");
});

afterEach(() => {
  vi.useRealTimers();
  for (const key of Object.keys(env)) delete env[key];
});

async function call(path, { method = "GET", session, headers = {}, body, raw } = {}) {
  const sent = new Headers(headers);
  if (session) sent.set("Cookie", `${SESSION_COOKIE}=${session}`);
  let payload = raw;
  if (body !== undefined) {
    sent.set("Content-Type", "application/json");
    payload = JSON.stringify(body);
  }
  const response = await worker.fetch(new Request(`${ORIGIN}${path}`, { method, headers: sent, body: payload }), env, ctx);
  await Promise.all(ctx.pending.splice(0));
  return response;
}

async function signIn(email = "ana@example.com") {
  const form = { Origin: ORIGIN, "Content-Type": "application/x-www-form-urlencoded" };
  await call("/login", { method: "POST", headers: form, raw: new URLSearchParams({ email }).toString() });
  const token = /\/login\/confirm#([A-Za-z0-9_-]{43})/.exec(bound.MAIL.sent.at(-1).text)[1];
  const confirmed = await call("/login/confirm", { method: "POST", headers: form, raw: new URLSearchParams({ token }).toString() });
  return new RegExp(`^${SESSION_COOKIE}=([^;]*)`).exec(confirmed.headers.get("Set-Cookie"))[1];
}

// A save from the page itself: a same-origin fetch, which carries the page's Origin.
const save = (session, body, headers = { Origin: ORIGIN }) => call("/api/settings/values", { method: "POST", session, headers, body });
const saveHours = (session, body, headers = { Origin: ORIGIN }) => call("/api/settings/schedule", { method: "POST", session, headers, body });
const settingsOf = (tenant) => bound.TENANT_DB.getByName(tenant).settings();
const rowsOf = async (tenant) =>
  (await bound.TENANT_DB.getByName(tenant).query("SELECT key, value FROM settings ORDER BY key")).rows;
const sha256 = (text) => `sha256-${createHash("sha256").update(text, "utf8").digest("base64")}`;

describe("GET /t/<tenant>/settings-next", () => {
  it("is the page TriageServer renders, built per catalog, in the tenant's", async () => {
    const session = await signIn();

    const response = await call("/t/alpha/settings-next", { session });

    expect(response.status).toBe(200);
    expect(response.headers.get("Content-Type")).toBe("text/html; charset=utf-8");
    expect(await response.text()).toBe(BUILT.pages.en.html);
    expect(Object.keys(BUILT.pages).sort()).toEqual(["en", "zh-Hant"]);
  });

  it("is worded in the catalog the tenant's output language reaches", async () => {
    const session = await signIn();
    await bound.TENANT_DB.getByName("alpha").saveSettings([["digest.output_language", "zh-Hant"]]);

    const html = await (await call("/t/alpha/settings-next", { session })).text();

    expect(html).toBe(BUILT.pages["zh-Hant"].html);
    expect(html).toContain('<html lang="zh-Hant">');
    expect(html).toContain('<meta name="i18n-catalog" content="/static/i18n/zh-Hant.json">');
  });

  it("runs under a policy of its own: this Worker's /static/ and the hashes of its own inline blocks", async () => {
    const session = await signIn();

    const response = await call("/t/alpha/settings-next", { session });
    const policy = response.headers.get("Content-Security-Policy");
    const html = await response.text();

    expect(policy).toBe(sharedPagePolicy(ORIGIN, BUILT.pages.en));
    expect(policy).not.toContain("sandbox");
    expect(policy).not.toContain("unsafe-inline");
    const directive = (name) => policy.split("; ").find((part) => part.startsWith(`${name} `));
    const inline = (tag) => [...html.matchAll(new RegExp(`<${tag}>([\\s\\S]*?)</${tag}>`, "g"))].map((match) => `'${sha256(match[1])}'`);
    expect(directive("script-src")).toBe(`script-src ${ORIGIN}/static/ ${inline("script").join(" ")}`);
    expect(directive("style-src")).toBe(`style-src ${ORIGIN}/static/ ${inline("style").join(" ")} https://fonts.googleapis.com`);
    for (const fixed of ["default-src 'none'", "connect-src 'self'", "form-action 'none'", "base-uri 'none'", "frame-ancestors 'none'"]) {
      expect(policy.split("; ")).toContain(fixed);
    }
    expect(response.headers.get("Cache-Control")).toBe("no-store");
    expect(response.headers.get("Referrer-Policy")).toBe("no-referrer");
  });

  it("is served only after the session check: none signs in first, another tenant's finds nothing", async () => {
    const session = await signIn();

    const signedOut = await call("/t/alpha/settings-next");
    const other = await call("/t/bravo/settings-next", { session });

    expect(signedOut.status).toBe(302);
    expect(signedOut.headers.get("Location")).toBe("/login");
    expect(other.status).toBe(404);
  });

  it("leaves the hosted page beside it as it was", async () => {
    const session = await signIn();

    const old = await call("/t/alpha/settings", { session });

    expect(old.status).toBe(200);
    expect(await old.text()).toContain('<form class="panel" action="api/settings"');
  });
});

describe("the shared page's /static/ files", () => {
  // Each path the page names (its scripts, its style, its catalog file) and each module those import.
  const named = (html) => [
    ...[...html.matchAll(/<(?:script|link)\b[^>]*\b(?:src|href)="(\/static\/[^"]+)"/g)].map((match) => match[1]),
    /<meta name="i18n-catalog" content="([^"]+)">/.exec(html)[1],
  ];

  it("are every file each page loads, and every module those import, as the source holds them", async () => {
    const session = await signIn();
    const wanted = new Set(Object.values(BUILT.pages).flatMap(({ html }) => named(html)));
    for (const path of Object.keys(BUILT.files).filter((path) => path.endsWith(".js"))) {
      for (const [, name] of BUILT.files[path].body.matchAll(/^\s*import\b[^;]*?\bfrom\s+["'](\.\/[^"']+)["']/gm)) {
        wanted.add(new URL(name, `${ORIGIN}${path}`).pathname);
      }
    }

    expect(Object.keys(BUILT.files).sort()).toEqual([...wanted].sort());
    for (const path of wanted) {
      const response = await call(path, { session });
      expect(response.status, path).toBe(200);
      if (!path.startsWith("/static/i18n/")) {
        expect(await response.text(), path).toBe(readFileSync(new URL(path.slice("/static/".length), STATIC_DIR), "utf8"));
      }
    }
    expect((await call("/static/settings.js", { session })).headers.get("Content-Type")).toBe("text/javascript; charset=utf-8");
    expect((await call("/static/style.css", { session })).headers.get("Content-Type")).toBe("text/css; charset=utf-8");
  });

  it("are kept by the browser and asked for again, and a match is answered 304", async () => {
    const session = await signIn();
    const first = await call("/static/settings.js", { session });

    const again = await call("/static/settings.js", { session, headers: { "If-None-Match": first.headers.get("ETag") } });

    expect(first.headers.get("Cache-Control")).toBe("private, no-cache");
    expect(again.status).toBe(304);
  });

  it("are served only to a session, and no other path under /static/ is", async () => {
    const session = await signIn();

    expect((await call("/static/settings.js")).status).toBe(302);
    expect((await call("/static/onboarding.js", { session })).status).toBe(404);
    expect((await call("/static/../hosted.js", { session })).status).not.toBe(200);
  });
});

describe("GET /api/settings on the hosted Worker", () => {
  it("answers TriageServer's shape, and the session's tenant's values", async () => {
    const session = await signIn();

    const data = await (await call("/api/settings", { session })).json();

    expect(Object.keys(data).sort()).toEqual(CASES.answer_keys);
    for (const provider of [...data.providers, ...data.embedding_providers]) {
      expect(Object.keys(provider).sort()).toEqual(CASES.provider_keys);
    }
    for (const field of Object.values(data.fields)) expect(Object.keys(field)).toEqual(expect.arrayContaining(CASES.field_keys));
    expect(data.languages).toEqual(BUILT.languages);
    expect(data.sent_as_shown).toEqual(BUILT.sent_as_shown);
    expect(data.writable).toBe(true);
  });

  it("locks every key no tenant writes, for the operator, and opens the policy's and the schedule", async () => {
    const session = await signIn();

    const data = await (await call("/api/settings", { session })).json();

    expect(data.locked).toEqual(CASES.editions.hosted.locked);
    for (const key of [...POLICY.writable_settings, "general.digest_schedule", "general.timezone"]) expect(data.locked).not.toHaveProperty(key);
    expect(data.missing.filter((key) => !(key in data.locked))).toEqual([]);
  });

  it("names the tenant's limits: the registry's most digest times, and the policy's article cap", async () => {
    const session = await signIn();

    const one = await (await call("/api/settings", { session })).json();
    await tenantRegistry(env).setSchedule("alpha", { times: ["07:00"], timeZone: "Asia/Taipei", maxDigestTimes: 2 });
    const two = await (await call("/api/settings", { session })).json();

    expect(one.limits).toEqual(CASES.editions.hosted.limits);
    expect(two.limits).toEqual({ ...CASES.editions.hosted.limits, digest_times_max: 2 });
  });

  it("names a provider's key ready only when the operator set every variable its run is handed", async () => {
    const session = await signIn();
    const embedder = STARTING_VALUES["vote_similarity.provider"];
    const [llmVariable] = BUILT.key_env.providers[POLICY.provider];
    const embedderVariables = BUILT.key_env.embedding_providers[embedder];

    const unset = await (await call("/api/settings", { session })).json();
    Object.assign(env, { [llmVariable]: "sk-operator", [embedderVariables[0]]: "cf-operator" });
    const partly = await (await call("/api/settings", { session })).json();
    for (const variable of embedderVariables) env[variable] = "set";
    const set = await (await call("/api/settings", { session })).json();

    expect(unset.providers).toEqual([{ name: POLICY.provider, env_var: llmVariable, default_model: POLICY.model, configured: false }]);
    expect(unset.embedding_providers.map(({ env_var: name, configured }) => [name, configured])).toEqual([[embedderVariables[0], false]]);
    expect(partly.providers[0].configured).toBe(true);
    expect(partly.embedding_providers[0].configured).toBe(embedderVariables.length === 1);
    expect(set.embedding_providers[0].configured).toBe(true);
  });

  it("shows the provider and model the run is pinned to, and the registry's schedule", async () => {
    const session = await signIn();
    // A row the tenant's own container could have written.
    await bound.TENANT_DB.getByName("alpha").query("UPDATE settings SET value = ? WHERE key = ?", ['"gemini"', "llm_provider.provider"]);

    const { values, providers } = await (await call("/api/settings", { session })).json();

    expect(values["llm_provider.provider"]).toBe("anthropic");
    expect(values["llm_provider.model"]).toBe("claude-haiku-5-5");
    expect(providers.map((provider) => provider.name)).toEqual(["anthropic"]);
    expect(values["general.digest_schedule"]).toEqual(["07:00"]);
    expect(values["general.timezone"]).toBe("Asia/Taipei");
  });

  it("shows every grade-D key the tenant's rows hold, not only the ones it writes", async () => {
    const session = await signIn();

    const { values, missing, embedding_providers: embedders } = await (await call("/api/settings", { session })).json();

    expect(values["digest.scoring_snippet_length"]).toBe(STARTING_VALUES["digest.scoring_snippet_length"]);
    expect(values["vote_similarity.provider"]).toBe(STARTING_VALUES["vote_similarity.provider"]);
    expect(embedders.map((embedder) => embedder.name)).toEqual([STARTING_VALUES["vote_similarity.provider"]]);
    // The one key no seed writes: an absent reasoning effort is the provider's default (ADR-0018).
    expect(missing).toEqual(["llm_provider.reasoning_effort"]);
  });

  it("reads through TenantDb, which hands out no notify target whoever asks", async () => {
    const db = bound.TENANT_DB.getByName("alpha");
    await db.query("UPDATE settings SET value = ? WHERE key = ?", ['"reader@example.org"', "notify.email_to"]);

    const read = await db.settings(["notify.email_to", "digest.style_prompt", "no.such_key"]);

    expect(read).toEqual({ "digest.style_prompt": STARTING_VALUES["digest.style_prompt"] });
  });

  it("never reads a notify target out of the tenant's rows", async () => {
    const session = await signIn();
    await bound.TENANT_DB.getByName("alpha").query("UPDATE settings SET value = ? WHERE key = ?", [
      '"https://discord.com/api/webhooks/1/container-wrote-it"',
      "notify.discord_webhook_url",
    ]);

    const text = await (await call("/api/settings", { session })).text();

    expect(text).not.toContain("container-wrote-it");
    expect(JSON.parse(text).values["notify.discord_webhook_url"]).toBe("");
  });

  it("signs a request with no session in first, before any tenant is read", async () => {
    const response = await call("/api/settings");

    expect(response.status).toBe(302);
    expect(response.headers.get("Location")).toBe("/login");
  });

  it.each(CASES.pins)("pins as the run does: $name", ({ stored, shown }) => {
    const values = hostedValues(stored, null);

    expect(Object.fromEntries(Object.keys(shown).map((key) => [key, values[key]]))).toEqual(shown);
  });
});

// The registry's row of `tenant`, as a save could change it.
const registryOf = async (tenant) => {
  const { digest_times: times, timezone, daily_run_cap: cap, max_digest_times: most } = await tenantRegistry(env).tenant(tenant);
  return { times, timezone, cap, most };
};

// One case of the shared file against the hosted Worker: its status and answer, or the field a
// refusal names; nothing written when refused, and what it saved read back.
async function holdsTo(session, response, { status, answer, refuses, reads_back: readsBack = {} }, before) {
  const got = await response.json();
  expect(response.status, JSON.stringify(got)).toBe(status);
  if (refuses) expect(got).toMatchObject({ ok: false, field: refuses });
  else expect(got).toEqual(answer);
  if (status !== 200) {
    expect(await rowsOf("alpha")).toEqual(before.rows);
    expect(await registryOf("alpha")).toEqual(before.registry);
    return;
  }
  const { values } = await (await call("/api/settings", { session })).json();
  expect(Object.fromEntries(Object.keys(readsBack).map((key) => [key, values[key]]))).toEqual(readsBack);
}

const stateOf = async (tenant) => ({ rows: await rowsOf(tenant), registry: await registryOf(tenant) });

describe("POST /api/settings/values on the hosted Worker", () => {
  it.each(CASES.writes)("gives TriageServer's answer: $name", async (written) => {
    const session = await signIn();
    const before = await stateOf("alpha");

    await holdsTo(session, await save(session, written.body), written, before);
  });

  it.each(RULE_CASES.settings.accepted)("takes $key = $value, as the hosted page does", async ({ key, value }) => {
    const session = await signIn();

    const response = await save(session, { values: { [key]: value } });

    expect(response.status).toBe(200);
    expect((await response.json()).values).toEqual({ [key]: value });
  });

  it.each(RULE_CASES.settings.refused.filter(({ key }) => POLICY.writable_settings.includes(key)))(
    "refuses $key = $value, as the hosted page does, in its words",
    async ({ key, value, reason }) => {
      const session = await signIn();
      const before = await rowsOf("alpha");
      const refusal = checkSettings({ [key]: value }).refused;

      const response = await save(session, { values: { [key]: value } });

      expect(refusal.reason).toBe(reason);
      expect(response.status).toBe(400);
      expect(await response.json()).toEqual({ ok: false, field: key, error: refusalText(translator("en"), refusal) });
      expect(await rowsOf("alpha")).toEqual(before);
    },
  );

  it("takes JSON's own types only: a number or a switch in a string is refused", async () => {
    const session = await signIn();

    for (const [key, value] of [["digest.max_articles_per_digest", "5"], ["digest.rank_by_preference", "true"]]) {
      const response = await save(session, { values: { [key]: value } });
      expect(response.status).toBe(400);
      expect((await response.json()).field).toBe(key);
    }
  });

  it("refuses an article cap above the policy's, naming the field and its bounds", async () => {
    const session = await signIn();

    const response = await save(session, { values: { "digest.max_articles_per_digest": POLICY.max_articles_per_digest + 1 } });

    expect(response.status).toBe(400);
    expect((await response.json()).error).toBe(`${EN["settings_field.digest.max_articles_per_digest"]} is a whole number from 1 to ${POLICY.max_articles_per_digest}.`);
  });

  it.each(["__proto__", "constructor", "hasOwnProperty", "toString", "valueOf", "max_digest_times", "daily_run_cap", "monthly_ai_budget_usd"])(
    "refuses %s as no setting, writing nothing",
    async (key) => {
      const session = await signIn();
      const before = await stateOf("alpha");

      const response = await save(session, { values: { "digest.style_prompt": "x", [key]: 2 } });

      expect(response.status).toBe(400);
      expect(await response.json()).toEqual({ ok: false, error: `'${key}' is not a setting` });
      expect(await stateOf("alpha")).toEqual(before);
    },
  );

  it("refuses a body that names no values, or not as an object", async () => {
    const session = await signIn();

    for (const body of [{}, { values: null }, { values: [] }, { values: "x" }, { values: 5 }, { values: {} }]) {
      const response = await save(session, body);
      expect(response.status).toBe(400);
      expect(await response.json()).toEqual({ ok: false, error: EN["settings_api.values_empty"] });
    }
  });

  it("words a refusal in the tenant's own catalog", async () => {
    const session = await signIn();
    await save(session, { values: { "digest.output_language": "zh-Hant" } });

    const response = await save(session, { values: { "llm_provider.provider": "gemini" } });

    const label = ZH_HANT["settings_field.llm_provider.provider"].text;
    const reason = ZH_HANT["lock_reason.operator-set"].text;
    expect((await response.json()).error).toBe(ZH_HANT["settings_api.locked"].text.replace("{$label}", label).replace("{$reason}", reason));
  });

  it("writes the zone through the registry, keeps its hours and the operator's run cap, and copies it to the tenant", async () => {
    const session = await signIn();
    const held = await registryOf("alpha");

    const response = await save(session, { values: { "general.timezone": "Europe/Paris" } });

    expect(response.status).toBe(200);
    expect(await registryOf("alpha")).toEqual({ ...held, timezone: "Europe/Paris" });
    expect(await settingsOf("alpha")).toMatchObject({ "general.digest_schedule": ["07:00"], "general.timezone": "Europe/Paris" });
    expect((await registryOf("bravo")).timezone).toBe("Asia/Taipei");
  });

  it("refuses a zone the registry would not take, writing nothing", async () => {
    const session = await signIn();
    const before = await stateOf("alpha");

    const response = await save(session, { values: { "general.timezone": "+08:00", "digest.style_prompt": "Short." } });

    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ ok: false, field: "general.timezone", error: EN["hosted_settings.refused_time_zone"].replace("{$zone}", "+08:00") });
    expect(await stateOf("alpha")).toEqual(before);
  });

  it("names no tenant from the body but the session's", async () => {
    const session = await signIn();
    const bravo = await stateOf("bravo");

    const response = await save(session, { tenant: "bravo", values: { "digest.style_prompt": "From alpha" } });

    expect(response.status).toBe(200);
    expect((await settingsOf("alpha"))["digest.style_prompt"]).toBe("From alpha");
    expect(await stateOf("bravo")).toEqual(bravo);
  });

  it("writes the session's tenant's own settings, and no other's", async () => {
    const session = await signIn();
    const bravo = await rowsOf("bravo");

    await save(session, { values: { "digest.output_language": "zh-Hant" } });

    expect((await rowsOf("alpha")).find((row) => row.key === "digest.output_language").value).toBe('"zh-Hant"');
    expect(await rowsOf("bravo")).toEqual(bravo);
  });

  it("then serves the page in the language saved", async () => {
    const session = await signIn();

    await save(session, { values: { "digest.output_language": "zh-Hant" } });

    expect(await (await call("/t/alpha/settings-next", { session })).text()).toBe(BUILT.pages["zh-Hant"].html);
  });

  it("refuses a write from no page of this origin, and one with no session", async () => {
    const session = await signIn();
    const before = await rowsOf("alpha");
    const body = { values: { "digest.output_language": "zh-Hant" } };

    expect((await save(session, body, {})).status).toBe(403);
    expect((await save(session, body, { Origin: "https://elsewhere.example" })).status).toBe(403);
    expect((await save(undefined, body)).status).toBe(401);
    expect(await rowsOf("alpha")).toEqual(before);
  });

  it.each([
    ["no Origin", {}],
    ["Origin null", { Origin: "null" }],
    ["another site's Origin", { Origin: "https://evil.example" }],
    ["this Origin from another site", { Origin: ORIGIN, "Sec-Fetch-Site": "cross-site" }],
  ])("refuses a save with %s, saying why as TriageServer marks it, writing nothing", async (_name, headers) => {
    const session = await signIn();
    const before = await stateOf("alpha");

    for (const response of [
      await save(session, { values: { "digest.style_prompt": "Forged", "general.timezone": "Europe/Paris" } }, headers),
      await saveHours(session, { times: ["03:00"] }, headers),
    ]) {
      expect(response.status).toBe(403);
      expect(await response.json()).toEqual({ ok: false, error: EN["hosted_settings.refused_cross_origin"], reason: "cross_origin" });
    }
    expect(await stateOf("alpha")).toEqual(before);
  });

  it("refuses a body over the policy's size unread, and one that is not JSON", async () => {
    const session = await signIn();

    for (const path of ["/api/settings/values", "/api/settings/schedule"]) {
      const big = await call(path, { method: "POST", session, headers: { Origin: ORIGIN }, body: { values: { "digest.style_prompt": "x".repeat(POLICY.max_body_bytes) } } });
      const junk = await call(path, { method: "POST", session, headers: { Origin: ORIGIN }, raw: "digest.style_prompt=x" });

      expect(big.status).toBe(413);
      expect(junk.status).toBe(400);
      expect((await junk.json()).error).toBe(EN["hosted_settings.refused_body"]);
    }
  });

  it("refuses a value the run would refuse", async () => {
    const session = await signIn();

    const response = await save(session, { values: { "digest.output_language": "  " } });

    expect(response.status).toBe(400);
    expect((await response.json()).field).toBe("digest.output_language");
  });
});

describe("POST /api/settings/schedule on the hosted Worker", () => {
  it.each(CASES.schedules)("gives TriageServer's answer: $name", async (scheduled) => {
    await tenantRegistry(env).setSchedule("alpha", { times: ["07:00"], timeZone: "Asia/Taipei", maxDigestTimes: scheduled.max_digest_times });
    const session = await signIn();
    const before = await stateOf("alpha");

    await holdsTo(session, await saveHours(session, scheduled.body), scheduled, before);
  });

  it("writes the hours through the registry, keeps its zone and the operator's run cap, and copies them to the tenant", async () => {
    await tenantRegistry(env).setSchedule("alpha", { times: ["07:00"], timeZone: "Asia/Taipei", maxDigestTimes: 2, dailyRunCap: 6 });
    const session = await signIn();

    const response = await saveHours(session, { times: ["06:00", "18:00"] });

    expect(response.status).toBe(200);
    expect(await registryOf("alpha")).toEqual({ times: '["06:00","18:00"]', timezone: "Asia/Taipei", cap: 6, most: 2 });
    expect(await settingsOf("alpha")).toMatchObject({ "general.digest_schedule": ["06:00", "18:00"], "general.timezone": "Asia/Taipei" });
    expect((await registryOf("bravo")).times).toBe('["07:00"]');
  });

  it("takes one hour from a tenant allowed one, and refuses two, writing nothing; a reader cannot raise its maximum", async () => {
    const session = await signIn();

    const two = await saveHours(session, { times: ["06:00", "18:00"] });
    const kept = await registryOf("alpha");
    const one = await saveHours(session, { times: ["06:00"] });

    expect(two.status).toBe(400);
    expect(kept).toMatchObject({ times: '["07:00"]', most: 1 });
    expect(one.status).toBe(200);
    expect(await registryOf("alpha")).toMatchObject({ times: '["06:00"]', most: 1 });
    expect(await settingsOf("alpha")).toMatchObject({ "general.digest_schedule": ["06:00"] });
    expect((await save(session, { values: { max_digest_times: 2 } })).status).toBe(400);
    expect((await registryOf("alpha")).most).toBe(1);
  });

  it("refuses hours sent as a settings value, which have their own route", async () => {
    const session = await signIn();

    const response = await save(session, { values: { "general.digest_schedule": ["06:00"] } });

    expect(response.status).toBe(400);
    expect((await registryOf("alpha")).times).toBe('["07:00"]');
  });

  it("takes POST only", async () => {
    const session = await signIn();

    expect((await call("/api/settings/schedule", { session })).status).toBe(405);
  });
});

// A save straight to the shared settings handler over a seeded tenant's object, with a registry
// whose schedule writes the test sees and can refuse.
describe("a shared save that writes settings rows and the zone", () => {
  const SCHEDULE = { times: ["08:00", "20:00"], timeZone: "UTC" };
  const body = { values: { "digest.style_prompt": "Short.", "general.timezone": "Asia/Taipei" } };
  const post = (deps, path = "/api/settings/values", sent = body) =>
    handleSharedSettings(
      new Request(`${ORIGIN}${path}`, { method: "POST", headers: { Origin: ORIGIN, "Content-Type": "application/json" }, body: JSON.stringify(sent) }),
      { path, registryRow: () => ({ digest_times: JSON.stringify(SCHEDULE.times), timezone: SCHEDULE.timeZone, max_digest_times: 2 }), ...deps },
    );
  const seeded = async () => {
    const { db } = tenantDb();
    await seedTenant({ db, tenant: "alpha", schemaSql: SCHEMA_SQL, hostedSchemaSql: HOSTED_SCHEMA_SQL, startingValues: STARTING_VALUES, seed: SEED, catalog: FEED_CATALOG, schedule: SCHEDULE });
    return db;
  };
  const rows = (db) => db.query("SELECT key, value FROM settings ORDER BY key").rows;

  it("writes no row when the registry refuses the zone", async () => {
    const db = await seeded();
    const before = rows(db);

    const answer = await post({ db, setSchedule: async () => ({ refused: "alpha is not a registered tenant" }) });

    expect(answer.status).toBe(400);
    expect((await answer.json()).field).toBe("general.timezone");
    expect(rows(db)).toEqual(before);
  });

  it("puts the registry's schedule back when the rows are refused", async () => {
    const db = await seeded();
    const written = [];
    const refusing = { settings: (keys) => db.settings(keys), saveSettings: () => ({ refused: "not_seeded", status: 409 }) };

    const answer = await post({ db: refusing, setSchedule: async (schedule) => (written.push(schedule), {}) });

    expect(answer.status).toBe(409);
    expect(written).toEqual([{ times: SCHEDULE.times, timeZone: "Asia/Taipei" }, SCHEDULE]);
  });

  it("writes no schedule for a tenant never seeded", async () => {
    const written = [];
    const unseeded = { settings: () => null, saveSettings: () => ({ refused: "not_seeded", status: 409 }) };

    for (const [path, sent] of [["/api/settings/values", body], ["/api/settings/schedule", { times: ["06:00"] }]]) {
      const answer = await post({ db: unseeded, registryRow: () => ({}), setSchedule: async (schedule) => (written.push(schedule), {}) }, path, sent);
      expect(answer.status).toBe(409);
    }
    expect(written).toEqual([]);
  });

  it("puts the registry's schedule back when writing the rows throws", async () => {
    const db = await seeded();
    const written = [];
    const throwing = { settings: (keys) => db.settings(keys), saveSettings: () => { throw new Error("storage reset"); } };

    await expect(post({ db: throwing, setSchedule: async (schedule) => (written.push(schedule), {}) })).rejects.toThrow("storage reset");

    expect(written).toEqual([{ times: SCHEDULE.times, timeZone: "Asia/Taipei" }, SCHEDULE]);
  });

  // The route-level lock rule TriageServer's routes keep, on the routes this Worker answers.
  it.each(CASES.route_locks.filter(({ path }) => path === "/api/settings/schedule"))(
    "refuses only a change to a locked key: $name",
    async ({ locked, stored, body: sent, status, answer }) => {
      const db = await seeded();
      const written = [];
      const locks = Object.fromEntries(Object.entries(locked).map(([key, reason]) => [key, { reason }]));
      const registryRow = () => ({ digest_times: JSON.stringify(stored["general.digest_schedule"]), timezone: "UTC", max_digest_times: 2 });

      const response = await post({ db, registryRow, locks, setSchedule: async (schedule) => (written.push(schedule), {}) }, "/api/settings/schedule", sent);

      expect(response.status).toBe(status);
      expect(await response.json()).toEqual(answer);
      expect(written.length).toBe(status === 200 ? 1 : 0);
    },
  );
});

describe("the tabs this milestone leaves empty", () => {
  it("answers /api/topics read-only and empty", async () => {
    const session = await signIn();

    expect(await (await call("/api/topics", { session })).json()).toEqual({ writable: false, topics: [], embedding_model: "" });
  });
});

// The Sources tab: the cases tests/test_shared_settings_cases.py runs against TriageServer, over
// a tenant whose tables each case sets.
const SOURCES = CASES.sources;
const expand = (value) => JSON.parse(JSON.stringify(value).replace(/\{pad:(\d+)\}/g, (_, n) => "a".repeat(Number(n))));
const dbOf = (tenant) => bound.TENANT_DB.getByName(tenant);
const put = (db, { name, type, url, config }) =>
  db.query("INSERT INTO sources (name, url, type, config) VALUES (?, ?, ?, ?)", [name, url, type, JSON.stringify(config)]);
const sourceRows = async (tenant) => (await dbOf(tenant).query("SELECT name, url, type, config FROM sources ORDER BY name")).rows;

// `tenant`'s tables as the case says: its sources, their poll records and stored articles.
async function arrange(tenant, { rows = [], feed_health: health = [], articles = [], fill_to_cap: fill = false }) {
  const db = dbOf(tenant);
  for (const table of ["sources", "feed_health", "stored_articles"]) await db.query(`DELETE FROM ${table}`);
  for (const row of rows) await put(db, row);
  for (let n = 1; fill && (await sourceRows(tenant)).length < SOURCES.max_sources.hosted; n++) {
    await put(db, { name: `Filler ${n}`, type: "rss", url: `https://filler${n}.example/feed`, config: { lane: "skim" } });
  }
  for (const row of health) {
    await db.query(
      "INSERT INTO feed_health (name, consecutive_failures, last_error, last_failed_at, last_ok_at) VALUES (?, ?, ?, ?, ?)",
      [row.name, row.consecutive_failures, row.last_error, row.last_failed_at, row.last_ok_at],
    );
  }
  for (const [n, row] of articles.entries()) {
    await db.query(
      "INSERT INTO stored_articles (url, published_at, source_name, source_lane, first_seen_at) VALUES (?, ?, ?, 'skim', ?)",
      [`https://stored.example/${n}`, row.first_seen_at, row.source_name, row.first_seen_at],
    );
  }
}

const SAME_ORIGIN = { Origin: ORIGIN };
const sourceCall = (session, method, path, body, headers = SAME_ORIGIN) => call(path, { method, session, headers, body });
const listed = async (session) => (await (await call("/api/sources", { session })).json()).sources;
const withoutHealth = (sources) => sources.map(({ health: _health, ...source }) => source);

describe("the Sources tab on the hosted Worker: the cases TriageServer answers too", () => {
  it.each(SOURCES.cases)("gives TriageServer's answer: $name", async (one) => {
    const session = await signIn();
    await arrange("alpha", one);
    const before = await sourceRows("alpha");
    const bravo = await sourceRows("bravo");

    const response = await sourceCall(session, one.method, one.path, one.body && expand(one.body));
    const answer = await response.json();

    expect(response.status).toBe(one.status);
    if (one.method === "GET") {
      expect(answer.max_sources).toBe(SOURCES.max_sources.hosted);
      delete answer.max_sources;
    }
    expect(answer).toEqual(one.answer);
    if (one.method !== "GET") {
      const after = await listed(session);
      if (one.reads_back) expect(withoutHealth(after)).toEqual(one.reads_back);
      else if (one.holds) {
        expect(after).toHaveLength(one.count);
        expect(withoutHealth(after)).toEqual(expect.arrayContaining(one.holds));
      } else expect(await sourceRows("alpha")).toEqual(before);
    }
    expect(await sourceRows("bravo")).toEqual(bravo);
  });

  it("holds the cases' own cap equal to the policy's", () => {
    expect(SOURCES.max_sources.hosted).toBe(POLICY.max_sources);
    expect(SOURCES.max_sources.self_hosted).toBeNull();
  });

  it("is signed in first, and answers each path only its own methods", async () => {
    const session = await signIn();

    expect((await call("/api/sources")).status).toBe(302);
    expect((await call("/api/sources/Plain", { method: "DELETE", headers: SAME_ORIGIN })).status).toBe(401);
    expect((await sourceCall(session, "PUT", "/api/sources", {})).status).toBe(405);
    expect((await sourceCall(session, "DELETE", "/api/sources", undefined)).status).toBe(405);
    expect((await call("/api/sources/Plain", { session })).status).toBe(405);
    expect((await sourceCall(session, "POST", "/api/sources/Plain", {})).status).toBe(405);
  });

  it("names the paths it answers, /api/sources/<name> one segment deep", () => {
    expect(isSharedPath("/api/sources")).toBe(true);
    expect(isSharedPath("/api/sources/Plain")).toBe(true);
    expect(isSharedPath("/api/sources/Two%20Words")).toBe(true);
    expect(isSharedPath("/api/sources/")).toBe(false);
    expect(isSharedPath("/api/sources/a/b")).toBe(false);
    expect(isSharedPath("/api/sourcesx")).toBe(false);
  });
});

describe("the Sources tab: what a hosted tenant is held to", () => {
  it("lists the session's tenant's sources, whichever the body or the path names", async () => {
    const session = await signIn();
    await arrange("alpha", { rows: [{ name: "Alpha only", type: "rss", url: "https://alpha.example/feed", config: {} }] });
    await arrange("bravo", { rows: [{ name: "Bravo only", type: "rss", url: "https://bravo.example/feed", config: {} }] });

    const answer = await (await call("/api/sources?tenant=bravo", { session, headers: { "X-Tenant": "bravo" } })).json();

    expect(answer.sources.map((source) => source.name)).toEqual(["Alpha only"]);
  });

  it("refuses a body that names a tenant as a field a hosted digest does not set, and writes nothing", async () => {
    const session = await signIn();
    const bravo = await sourceRows("bravo");
    const alpha = await sourceRows("alpha");

    const answer = await sourceCall(session, "POST", "/api/sources", { name: "Sneak", url: "https://sneak.example/feed", tenant: "bravo" });

    expect(answer.status).toBe(400);
    expect((await answer.json()).field).toBe("tenant");
    expect(await sourceRows("bravo")).toEqual(bravo);
    expect(await sourceRows("alpha")).toEqual(alpha);
  });

  it("adds to the session's tenant, and removes from it, and no other's", async () => {
    const session = await signIn();
    const bravo = await sourceRows("bravo");

    const added = await sourceCall(session, "POST", "/api/sources", { name: "Example", url: "https://example.org/feed.xml", lane: "read" });
    const removed = await sourceCall(session, "DELETE", "/api/sources/Hacker%20News");

    expect(added.status).toBe(200);
    expect(removed.status).toBe(200);
    const names = (await listed(session)).map((source) => source.name);
    expect(names).toContain("Example");
    expect(names).not.toContain("Hacker News");
    expect(await sourceRows("bravo")).toEqual(bravo);
  });

  it.each(RULE_CASES.sources.written)("writes $body.name as the hosted page writes it", async ({ body, row }) => {
    const session = await signIn();

    const answer = await sourceCall(session, "POST", "/api/sources", body);

    expect(answer.status).toBe(200);
    const kept = (await sourceRows("alpha")).find((source) => source.name === row.name);
    expect({ ...kept, config: JSON.parse(kept.config) }).toEqual(row);
  });

  it.each(RULE_CASES.sources.refused)("refuses $body.name as $reason, as the hosted page does", async ({ body, reason }) => {
    const session = await signIn();
    const before = await sourceRows("alpha");

    const answer = await sourceCall(session, "POST", "/api/sources", body);
    const refusal = await answer.json();

    expect(answer.status).toBe(400);
    expect(refusal.ok).toBe(false);
    expect(refusal.error).toMatch(/\S/);
    expect(refusal.error).not.toContain("{$");
    expect(refusal.field).toBe(reason.startsWith("url_") ? "url" : reason.replace("source_", ""));
    expect(await sourceRows("alpha")).toEqual(before);
  });

  it("takes a URL of the policy's length and no longer", async () => {
    const session = await signIn();
    const base = "https://example.org/";
    const at = base + "a".repeat(POLICY.source_url.max_length - base.length);

    const taken = await sourceCall(session, "POST", "/api/sources", { name: "Long", url: at });
    const past = await sourceCall(session, "POST", "/api/sources", { name: "Longer", url: `${at}a` });

    expect(taken.status).toBe(200);
    expect(past.status).toBe(400);
    expect((await past.json()).error).toBe(EN["hosted_settings.refused_url_length"].replace("{$max}", String(POLICY.source_url.max_length)));
  });

  it("refuses this Worker's own host, whatever its name", async () => {
    const session = await signIn();
    env.CYRIS_STORE_HOSTED_DOMAIN = "digest.example.com";

    const own = await sourceCall(session, "POST", "/api/sources", { name: "Own", url: "https://digest.example.com/hosted/spend" });
    const other = await sourceCall(session, "POST", "/api/sources", { name: "Other", url: "https://digest.example.org/feed" });

    expect(own.status).toBe(400);
    expect((await own.json()).field).toBe("url");
    expect(other.status).toBe(200);
  });

  it("lets two saves at once take the last place under the cap, and only one", async () => {
    const session = await signIn();
    await arrange("alpha", { rows: [], fill_to_cap: true });
    await dbOf("alpha").query("DELETE FROM sources WHERE name = 'Filler 1'");

    const answers = await Promise.all(
      ["Last A", "Last B"].map((name) => sourceCall(session, "POST", "/api/sources", { name, url: `https://${name[5].toLowerCase()}.example/feed` })),
    );

    expect(answers.map((answer) => answer.status).sort()).toEqual([200, 409]);
    const refusal = await (await answers.find((answer) => answer.status === 409)).json();
    expect(refusal.error).toBe(EN["hosted_settings.refused_source_cap"].replace("{$max}", String(POLICY.max_sources)));
    expect(await sourceRows("alpha")).toHaveLength(POLICY.max_sources);
  });

  it("refuses a save from no page of this origin, and a removal too, and a save with no session", async () => {
    const session = await signIn();
    const before = await sourceRows("alpha");
    const body = { name: "Example", url: "https://example.org/feed" };

    for (const headers of [{}, { Origin: "https://elsewhere.example" }]) {
      const added = await sourceCall(session, "POST", "/api/sources", body, headers);
      const removed = await sourceCall(session, "DELETE", "/api/sources/Hacker%20News", undefined, headers);
      expect(added.status).toBe(403);
      expect((await added.json()).error).toBe(EN["hosted_settings.refused_cross_origin"]);
      expect(removed.status).toBe(403);
    }
    expect((await sourceCall(undefined, "POST", "/api/sources", body)).status).toBe(401);
    expect(await sourceRows("alpha")).toEqual(before);
  });

  it("refuses a body past the policy's size unread, and one that is no JSON", async () => {
    const session = await signIn();
    const before = await sourceRows("alpha");
    const big = JSON.stringify({ name: "Big", url: "https://example.org/feed", lane: "x".repeat(POLICY.max_body_bytes) });

    const large = await call("/api/sources", { method: "POST", session, headers: { ...SAME_ORIGIN, "Content-Type": "application/json" }, raw: big });
    const broken = await call("/api/sources", { method: "POST", session, headers: { ...SAME_ORIGIN, "Content-Type": "application/json" }, raw: "{not json" });

    expect(large.status).toBe(413);
    expect((await large.json()).error).toBe(EN["hosted_settings.refused_too_large"]);
    expect(broken.status).toBe(400);
    expect(await sourceRows("alpha")).toEqual(before);
  });

  it("words its refusals in the catalog the tenant's output language reaches", async () => {
    const session = await signIn();
    await dbOf("alpha").saveSettings([["digest.output_language", "zh-Hant"]]);

    const refused = await sourceCall(session, "POST", "/api/sources", { name: "Example", url: "ftp://example.org/feed" });
    const missing = await sourceCall(session, "DELETE", "/api/sources/Nothing");

    expect((await refused.json()).error).toBe(ZH_HANT["hosted_settings.refused_url_scheme"].text);
    expect((await missing.json()).error).toBe(ZH_HANT["settings_api.source_missing"].text.replace("{$name}", "Nothing"));
  });

  it("answers a tenant never seeded as a deployment with no source table does, and writes nothing", async () => {
    await tenantRegistry(env).register("charlie");
    await tenantRegistry(env).invite("charlie", "cy@example.com");
    const session = await signIn("cy@example.com");

    const list = await (await call("/api/sources", { session })).json();
    const added = await sourceCall(session, "POST", "/api/sources", { name: "Example", url: "https://example.org/feed" });
    const removed = await sourceCall(session, "DELETE", "/api/sources/Example");

    expect(list).toEqual({ writable: false, max_sources: POLICY.max_sources, sources: [] });
    expect([added.status, removed.status]).toEqual([409, 409]);
    expect((await added.json()).error).toBe(EN["hosted_settings.refused_not_seeded"]);
  });
});

describe("the Sources tab's feed health", () => {
  it("is judged by the thresholds cyris doctor is, from the one file both read", () => {
    expect([UNHEALTHY_FAILURE_STREAK, QUIET_DAYS]).toEqual([RULES.unhealthy_failure_streak, RULES.quiet_days]);
    const now = Date.parse("2026-11-24T06:00:00Z");
    const polled = { consecutive_failures: 0, newest_article_at: "2026-11-24T06:00:00.000Z" };

    expect(findings({ ...polled, consecutive_failures: UNHEALTHY_FAILURE_STREAK - 1 }, now)).toEqual([]);
    expect(findings({ ...polled, consecutive_failures: UNHEALTHY_FAILURE_STREAK }, now)).toEqual(["failing"]);
    expect(findings({ ...polled, newest_article_at: null }, now)).toEqual(["never_stored"]);
    const edge = new Date(now - QUIET_DAYS * 24 * 3600 * 1000);
    expect(findings({ ...polled, newest_article_at: edge.toISOString() }, now)).toEqual(["quiet"]);
    expect(findings({ ...polled, newest_article_at: new Date(edge.getTime() + 3600 * 1000).toISOString() }, now)).toEqual([]);
    // A stamp with no zone is UTC, as `_utc` reads it.
    expect(findings({ ...polled, newest_article_at: "2026-10-01 00:00:00" }, now)).toEqual(["quiet"]);
  });

  it("shows the record the tenant's own poll writes", async () => {
    const session = await signIn();
    await arrange("alpha", { rows: [{ name: "Down", type: "rss", url: "https://down.example/feed", config: { lane: "skim" } }] });
    await dbOf("alpha").query(
      "INSERT INTO feed_health (name, consecutive_failures, last_error, last_failed_at) VALUES ('Down', 1, 'HTTP 503', '2026-11-24T05:00:00.000Z')",
    );

    const [down] = await listed(session);

    expect(down.health).toMatchObject({ consecutive_failures: 1, last_error: "HTTP 503", problems: ["no article stored yet"] });
  });

  it("still lists the sources when the poll's tables cannot be read", async () => {
    const session = await signIn();
    await dbOf("alpha").query("DROP TABLE feed_health");

    const sources = await listed(session);

    expect(sources.length).toBeGreaterThan(0);
    expect(sources.map((source) => source.health)).toEqual(sources.map(() => null));
  });
});
