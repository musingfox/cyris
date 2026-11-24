import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { env } from "cloudflare:workers";
import FEED_CATALOG from "../../../src/cyris/onboarding_feeds.json" with { type: "json" };
import STARTING_VALUES from "../../../src/cyris/settings_starting_values.json" with { type: "json" };
import SEED from "../src/hosted_seed.json" with { type: "json" };
import BUILT from "../src/shared_settings.build.json" with { type: "json" };
import worker from "../src/hosted.js";
import { seedTenant } from "../src/hosted_seed.js";
import { hostedValues, sharedPagePolicy } from "../src/hosted_shared_settings.js";
import { SESSION_COOKIE } from "../src/hosted_session.js";
import { tenantRegistry } from "../src/tenant_registry.js";
import CASES from "./shared_settings_cases.json" with { type: "json" };
import { HOSTED_SCHEMA_SQL, SCHEMA_SQL, namespace, registryNamespace } from "./sql_storage.js";

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

  it("locks every key but the open one, each for its reason, and hides Areas", async () => {
    const session = await signIn();

    const data = await (await call("/api/settings", { session })).json();

    expect(data.locked).toEqual(CASES.editions.hosted.locked);
    expect(data.hidden_tabs).toEqual(CASES.editions.hosted.hidden_tabs);
    expect(data.missing.filter((key) => !(key in data.locked))).toEqual([]);
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

describe("POST /api/settings/values on the hosted Worker", () => {
  it.each(CASES.writes)("gives TriageServer's answer: $name", async ({ body, status, answer, reads_back: readsBack }) => {
    const session = await signIn();
    const before = await rowsOf("alpha");

    const response = await save(session, body);

    expect(response.status).toBe(status);
    expect(await response.json()).toEqual(answer);
    if (status !== 200) {
      expect(await rowsOf("alpha")).toEqual(before);
      return;
    }
    const { values } = await (await call("/api/settings", { session })).json();
    expect(Object.fromEntries(Object.keys(readsBack).map((key) => [key, values[key]]))).toEqual(readsBack);
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

  it("refuses a value the run would refuse", async () => {
    const session = await signIn();

    const response = await save(session, { values: { "digest.output_language": "  " } });

    expect(response.status).toBe(400);
    expect((await response.json()).field).toBe("digest.output_language");
  });
});

describe("the tabs this milestone leaves empty", () => {
  it.each([
    ["/api/sources", { writable: false, sources: [] }],
    ["/api/topics", { writable: false, topics: [], embedding_model: "" }],
    ["/api/areas", { writable: false, areas: [], birth_threshold: null }],
  ])("answer %s read-only and empty", async (path, answer) => {
    const session = await signIn();

    expect(await (await call(path, { session })).json()).toEqual(answer);
  });
});
