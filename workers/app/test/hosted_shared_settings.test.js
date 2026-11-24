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
import worker from "../src/hosted.js";
import { seedTenant } from "../src/hosted_seed.js";
import { refusalText, translator } from "../src/hosted_settings.js";
import { checkSettings } from "../src/hosted_settings_rules.js";
import { handleSharedSettings, hostedValues, sharedPagePolicy } from "../src/hosted_shared_settings.js";
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
  it.each([
    ["/api/sources", { writable: false, sources: [] }],
    ["/api/topics", { writable: false, topics: [], embedding_model: "" }],
  ])("answer %s read-only and empty", async (path, answer) => {
    const session = await signIn();

    expect(await (await call(path, { session })).json()).toEqual(answer);
  });
});
