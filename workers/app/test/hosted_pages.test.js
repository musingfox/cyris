import { describe, it, expect } from "vitest";
import { generateKeyPair, exportJWK, SignJWT, createLocalJWKSet } from "jose";
import FEED_CATALOG from "../../../src/cyris/onboarding_feeds.json" with { type: "json" };
import STARTING_VALUES from "../../../src/cyris/settings_starting_values.json" with { type: "json" };
import SEED from "../src/hosted_seed.json" with { type: "json" };
import { serveTenantPage } from "../src/hosted_pages.js";
import { handleHostedRequest } from "../src/hosted_router.js";
import { seedTenant } from "../src/hosted_seed.js";
import { typeScaleStyle } from "../src/type_scale.js";
import { HOSTED_SCHEMA_SQL, SCHEMA_SQL, namespace } from "./sql_storage.js";

const TEAM = "team.example";
const AUD = "AUD-HOSTED";
const { privateKey, publicKey } = await generateKeyPair("RS256");
const JWK = { ...(await exportJWK(publicKey)), kid: "k1", alg: "RS256", use: "sig" };
const ACCESS_KEYS = { keySet: createLocalJWKSet({ keys: [JWK] }), issuer: `https://${TEAM}` };
const ENV = { CYRIS_ACCESS_TEAM_DOMAIN: TEAM, CYRIS_ACCESS_AUD: AUD };
const ORIGIN = "https://cyris-hosted.example.workers.dev";

const accessJwt = () =>
  new SignJWT({})
    .setProtectedHeader({ alg: "RS256", kid: "k1" })
    .setIssuer(`https://${TEAM}`)
    .setAudience(AUD)
    .setIssuedAt()
    .setExpirationTime(Math.floor(Date.now() / 1000) + 3600)
    .sign(privateKey);

async function reader(path, { method = "GET", signedIn = true } = {}) {
  const headers = signedIn ? { "Cf-Access-Jwt-Assertion": await accessJwt() } : {};
  return new Request(`${ORIGIN}${path}`, { method, headers });
}

// The page a run renders, reduced to what serving reads back: its receipt names the issue.
const page = (tenant, slug, filler = "") =>
  `<!doctype html><html><head><meta name="cyris-issue" content="${slug}"><title>${tenant}</title></head>` +
  `<body><a href="${slug}-raw.html">raw</a>${filler}</body></html>`;

// What the run's HostedPages writes, through the same query the gateway hands the object.
const UPSERT =
  "INSERT INTO hosted_pages (path, body, content_type, updated_at) VALUES (?, ?, ?, ?) " +
  "ON CONFLICT(path) DO UPDATE SET body = excluded.body, content_type = excluded.content_type, " +
  "updated_at = excluded.updated_at";
const HTML = "text/html; charset=utf-8";

async function publish(db, date, period, { savedAt, tenant, filler = "" }) {
  const slug = `${date}-${period}`;
  await db.query("INSERT OR REPLACE INTO digests (date, period, content, raw_page, saved_at) VALUES (?, ?, ?, ?, ?)", [
    date,
    period,
    "{}",
    1,
    savedAt,
  ]);
  await db.query(UPSERT, [`/${slug}-raw.html`, `<html><head></head><body>raw ${tenant} ${slug}${filler}</body></html>`, HTML, savedAt]);
  await db.query(UPSERT, [`/${slug}.html`, page(tenant, slug), HTML, savedAt]);
  await db.query(UPSERT, ["/index.html", `<html><head></head><body>archive of ${tenant}</body></html>`, HTML, savedAt]);
  await db.query(UPSERT, ["/favicon.svg", "<svg/>", "image/svg+xml", savedAt]);
}

const seed = (TENANT_DB, tenant) =>
  seedTenant({
    db: TENANT_DB.getByName(tenant),
    tenant,
    schemaSql: SCHEMA_SQL,
    hostedSchemaSql: HOSTED_SCHEMA_SQL,
    startingValues: STARTING_VALUES,
    seed: SEED,
    catalog: FEED_CATALOG,
  });

// As index.js's HTMLRewriter does it: the style at the end of <head>.
const injectStyle = (response, html) => {
  const body =
    response.body &&
    new ReadableStream({
      async start(controller) {
        const text = await response.text();
        controller.enqueue(new TextEncoder().encode(text.replace("</head>", `${html}</head>`)));
        controller.close();
      },
    });
  return new Response(body, response);
};

// The hosted Worker as hosted.js wires it, over two seeded tenants: alpha has an older and a
// newer issue, bravo one issue saved after both of alpha's.
async function hosted({ scales = {} } = {}) {
  const TENANT_DB = namespace();
  for (const tenant of ["alpha", "bravo"]) await seed(TENANT_DB, tenant);
  const alpha = TENANT_DB.getByName("alpha");
  const bravo = TENANT_DB.getByName("bravo");
  await publish(alpha, "2026-11-17", "evening", { savedAt: "2026-11-17T20:05:00+00:00", tenant: "alpha" });
  await publish(alpha, "2026-11-18", "morning", { savedAt: "2026-11-18T08:05:00+00:00", tenant: "alpha" });
  await publish(bravo, "2026-11-18", "evening", { savedAt: "2026-11-18T20:05:00+00:00", tenant: "bravo" });
  const reads = [];
  const deps = {
    accessKeys: ACCESS_KEYS,
    tenants: ["alpha", "bravo", "charlie"],
    page: (request, tenant, rest) => {
      reads.push(tenant);
      return serveTenantPage(request, {
        tenant,
        rest,
        db: TENANT_DB.getByName(tenant),
        deps: { typeScale: { current: async () => scales[tenant] ?? 1 }, injectStyle },
      });
    },
  };
  const get = async (path, options) => handleHostedRequest(await reader(path, options), ENV, deps);
  return { get, reads, alpha, TENANT_DB };
}

describe("a hosted tenant's pages", () => {
  it("answer the latest issue, an older issue and a raw page with 200 to a signed-in operator", async () => {
    const { get } = await hosted();

    const latest = await get("/t/alpha/");
    const older = await get("/t/alpha/2026-11-17-evening");
    const raw = await get("/t/alpha/2026-11-18-morning-raw");

    expect([latest.status, older.status, raw.status]).toEqual([200, 200, 200]);
    expect(latest.headers.get("Content-Type")).toBe(HTML);
    expect(await latest.text()).toBe(page("alpha", "2026-11-18-morning"));
    expect(await older.text()).toBe(page("alpha", "2026-11-17-evening"));
    expect(await raw.text()).toContain("raw alpha 2026-11-18-morning");
  });

  it("answer a page by its .html name as Pages does, and the archive and the favicon", async () => {
    const { get } = await hosted();

    const named = await get("/t/alpha/2026-11-17-evening.html");
    const archive = await get("/t/alpha/index.html");
    const icon = await get("/t/alpha/favicon.svg");

    expect(await named.text()).toBe(page("alpha", "2026-11-17-evening"));
    expect(await archive.text()).toContain("archive of alpha");
    expect(icon.headers.get("Content-Type")).toBe("image/svg+xml");
  });

  it("refuse every request without an Access sign-in, before reading any tenant", async () => {
    const { get, reads } = await hosted();
    for (const path of ["/t/alpha/", "/t/alpha/2026-11-17-evening", "/t/bravo/2026-11-18-evening-raw", "/t/nobody/"]) {
      const response = await get(path, { signedIn: false });
      expect(response.status).toBe(403);
      expect(await response.text()).not.toContain("<html");
    }
    expect(reads).toEqual([]);
  });

  it("never answer one tenant's route with another tenant's page", async () => {
    const { get, reads } = await hosted();

    // bravo's issue is the newest saved anywhere, and alpha's latest is still alpha's own.
    expect(await (await get("/t/alpha/")).text()).toBe(page("alpha", "2026-11-18-morning"));
    expect(await (await get("/t/bravo/")).text()).toBe(page("bravo", "2026-11-18-evening"));
    expect((await get("/t/alpha/2026-11-18-evening")).status).toBe(404);
    expect((await get("/t/bravo/2026-11-17-evening")).status).toBe(404);
    expect((await get("/t/alpha/..%2Fbravo%2F2026-11-18-evening")).status).toBe(404);
    expect((await get("/t/alpha/../bravo/2026-11-18-evening")).status).toBe(200);
    // The URL parser resolved that dot segment to bravo's own route before any read.
    expect(reads.at(-1)).toBe("bravo");
  });

  it("serve a page over a Durable Object statement's 100 KB whole", async () => {
    const { get, alpha } = await hosted();
    const filler = "x".repeat(150_000);
    await publish(alpha, "2026-11-18", "evening", { savedAt: "2026-11-18T20:10:00+00:00", tenant: "alpha", filler });

    const raw = await (await get("/t/alpha/2026-11-18-evening-raw")).text();

    expect(raw.length).toBeGreaterThan(150_000);
    expect(raw).toBe(`<html><head></head><body>raw alpha 2026-11-18-evening${filler}</body></html>`);
    // Bound, it went in; written into the statement, the object refuses it.
    const inline = `INSERT INTO hosted_pages (path, body, content_type, updated_at) VALUES ('/x.html', '${filler}', 'text/html', '')`;
    expect(() => alpha.query(inline)).toThrow("statement too long");
  });

  it("carry the tenant's own type size, and pass a page untouched at 1", async () => {
    const { get } = await hosted({ scales: { alpha: 1.125 } });

    const sized = await get("/t/alpha/");
    const plain = await get("/t/bravo/");

    expect(await sized.text()).toContain(`${typeScaleStyle(1.125)}</head>`);
    expect(await plain.text()).toBe(page("bravo", "2026-11-18-evening"));
  });

  it("send the site root to its slashed form, so relative links stay in the tenant's site", async () => {
    const { get } = await hosted();

    const response = await get("/t/alpha");

    expect(response.status).toBe(301);
    expect(response.headers.get("Location")).toBe("/t/alpha/");
  });

  it("answer 404 for a tenant outside the list, one never seeded, and a page the site lacks", async () => {
    const { get } = await hosted();

    expect((await get("/t/delta/")).status).toBe(404);
    expect((await get("/t/charlie/")).status).toBe(404);
    expect((await get("/t/alpha/2020-01-01-morning")).status).toBe(404);
    expect((await get("/t/alpha/nested/2026-11-17-evening")).status).toBe(404);
  });

  it("answer HEAD without a body and refuse a write", async () => {
    const { get } = await hosted();

    const head = await get("/t/alpha/", { method: "HEAD" });
    const post = await get("/t/alpha/", { method: "POST" });

    expect(head.status).toBe(200);
    expect(await head.text()).toBe("");
    expect(post.status).toBe(405);
  });
});
