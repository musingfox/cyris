import { afterEach, describe, it, expect, vi } from "vitest";
import { generateKeyPair, exportJWK, SignJWT, createLocalJWKSet } from "jose";
import FEED_CATALOG from "../../../src/cyris/onboarding_feeds.json" with { type: "json" };
import STARTING_VALUES from "../../../src/cyris/settings_starting_values.json" with { type: "json" };
import SEED from "../src/hosted_seed.json" with { type: "json" };
import { serveTenantPage, sha256Hex } from "../src/hosted_pages.js";
import { handleHostedRequest } from "../src/hosted_router.js";
import { createSitePage } from "../src/hosted_site.js";
import { seedTenant, tenantReport } from "../src/hosted_seed.js";
import { SESSION_COOKIE, newToken, tokenHash } from "../src/hosted_session.js";
import { LOCATION_HINT } from "../src/tenant_db.js";
import { typeScaleStyle } from "../src/type_scale.js";
import { HOSTED_SCHEMA_SQL, SCHEMA_SQL, namespace } from "./sql_storage.js";

// The schedule the seed writes a tenant the registry has none for: one hour, in UTC.
const SEEDED_SCHEDULE = { times: ["08:00"], timeZone: "UTC" };

const TEAM = "team.example";
const AUD = "AUD-HOSTED";
const { privateKey, publicKey } = await generateKeyPair("RS256");
const JWK = { ...(await exportJWK(publicKey)), kid: "k1", alg: "RS256", use: "sig" };
const ACCESS_KEYS = { keySet: createLocalJWKSet({ keys: [JWK] }), issuer: `https://${TEAM}` };
// The service token the operator signs in with: its Client ID, which Access signs in as `common_name`.
const OPERATOR = "operator-token.access";
const ENV = { CYRIS_ACCESS_TEAM_DOMAIN: TEAM, CYRIS_ACCESS_AUD: AUD, CYRIS_HOSTED_OPERATORS: OPERATOR };
const ORIGIN = "https://cyris-hosted.example.workers.dev";

const accessJwt = () =>
  new SignJWT({ common_name: OPERATOR })
    .setProtectedHeader({ alg: "RS256", kid: "k1" })
    .setIssuer(`https://${TEAM}`)
    .setAudience(AUD)
    .setIssuedAt()
    .setExpirationTime(Math.floor(Date.now() / 1000) + 3600)
    .sign(privateKey);

// Each tenant's reader holds a session of that tenant (hosted_session.js), and the operator an
// Access JWT, which opens no page under /t/.
const SESSIONS = { alpha: newToken(), bravo: newToken(), charlie: newToken() };
const SESSION_TENANTS = new Map(
  await Promise.all(Object.entries(SESSIONS).map(async ([tenant, token]) => [await tokenHash(token), { tenant_id: tenant }])),
);

// A request for `path` by the reader of `as`, by default the tenant the path names; `as: null`
// is a request with no session, and `operator` adds the operator's Access JWT.
async function reader(path, { method = "GET", as = /^\/t\/([^/]+)/.exec(path)?.[1], operator = false } = {}) {
  const headers = {};
  if (as && SESSIONS[as]) headers.Cookie = `${SESSION_COOKIE}=${SESSIONS[as]}`;
  if (operator) headers["Cf-Access-Jwt-Assertion"] = await accessJwt();
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
    schedule: SEEDED_SCHEDULE,
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
    // The registry: charlie is registered but has no pages; delta is not registered, so no
    // session is delta's.
    tenant: async (tenant) => (["alpha", "bravo", "charlie"].includes(tenant) ? { id: tenant, status: "active" } : null),
    session: async (hash) => SESSION_TENANTS.get(hash) ?? null,
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
  it("answer the latest issue, an older issue and a raw page with 200 to the tenant's signed-in reader", async () => {
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

  it("send every request without a session to sign in, before reading any tenant, an operator's included", async () => {
    const { get, reads } = await hosted();
    for (const path of ["/t/alpha/", "/t/alpha/2026-11-17-evening", "/t/bravo/2026-11-18-evening-raw", "/t/nobody/"]) {
      for (const response of [await get(path, { as: null }), await get(path, { as: null, operator: true })]) {
        expect(response.status).toBe(302);
        expect(response.headers.get("Location")).toBe("/login");
        expect(await response.text()).not.toContain("<html");
      }
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
    // The URL parser resolves that dot segment to bravo's own route before any read, so it is
    // bravo's reader's page and alpha's reader is refused it.
    expect((await get("/t/alpha/../bravo/2026-11-18-evening", { as: "alpha" })).status).toBe(404);
    expect((await get("/t/alpha/../bravo/2026-11-18-evening", { as: "bravo" })).status).toBe(200);
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

  it("answer 404 for a tenant the registry does not hold, one never seeded, and a page the site lacks", async () => {
    const { get } = await hosted();

    expect((await get("/t/delta/", { as: "alpha" })).status).toBe(404);
    expect((await get("/t/charlie/")).status).toBe(404);
    expect((await get("/t/alpha/2020-01-01-morning")).status).toBe(404);
    expect((await get("/t/alpha/nested/2026-11-17-evening")).status).toBe(404);
  });

  it("take each page's type from its name, never from the type its row stores, and serve no other kind of file", async () => {
    const { get, alpha } = await hosted();
    // Rows a run container could write into its own database: a page stored as script, an icon
    // stored as HTML, the latest issue stored as plain text, and files the run never writes.
    await alpha.query(UPSERT, ["/2026-11-17-evening.html", page("alpha", "2026-11-17-evening"), "text/javascript", ""]);
    await alpha.query(UPSERT, ["/favicon.svg", "<svg/>", "text/html", ""]);
    await alpha.query(UPSERT, ["/2026-11-18-morning.html", page("alpha", "2026-11-18-morning"), "text/plain", ""]);
    await alpha.query(UPSERT, ["/evil.js", "alert(1)", "text/html", ""]);
    await alpha.query(UPSERT, ["/notes.txt", "<script>alert(1)</script>", "text/html", ""]);

    const older = await get("/t/alpha/2026-11-17-evening");
    const icon = await get("/t/alpha/favicon.svg");
    const latest = await get("/t/alpha/");

    expect([older.status, icon.status, latest.status]).toEqual([200, 200, 200]);
    expect(older.headers.get("Content-Type")).toBe(HTML);
    expect(icon.headers.get("Content-Type")).toBe("image/svg+xml");
    expect(latest.headers.get("Content-Type")).toBe(HTML);
    for (const response of [older, icon, latest]) expect(response.headers.get("X-Content-Type-Options")).toBe("nosniff");
    for (const path of ["/t/alpha/evil.js", "/t/alpha/notes.txt"]) {
      const refused = await get(path);
      expect(refused.status, path).toBe(404);
      expect(refused.headers.get("Content-Type"), path).toBe("application/json");
      expect(await refused.text(), path).not.toContain("alert");
    }
  });

  it("answer HEAD without a body and refuse a write", async () => {
    const { get } = await hosted();

    const head = await get("/t/alpha/", { method: "HEAD" });
    const post = await get("/t/alpha/", { method: "POST" });

    expect(head.status).toBe(200);
    expect(await head.text()).toBe("");
    expect(post.status).toBe(405);
  });

  it("are named in the tenant's report by the page its site root serves and that page's hash", async () => {
    const { get, TENANT_DB } = await hosted();

    const served = await (await get("/t/alpha/")).text();
    const report = await tenantReport({ db: TENANT_DB.getByName("alpha"), tenant: "alpha" });
    const other = await tenantReport({ db: TENANT_DB.getByName("bravo"), tenant: "bravo" });

    expect(report.latest_page).toEqual({ path: "/2026-11-18-morning.html", sha256: await sha256Hex(served) });
    expect(report.latest_page.sha256).toMatch(/^[0-9a-f]{64}$/);
    expect(other.latest_page.sha256).not.toBe(report.latest_page.sha256);
  });
});

// The wiring hosted.js hands the router, built by the same factory it calls: only the databases
// and workerd's HTMLRewriter are stand-ins. HTMLRewriter is the one global the factory reads.
describe("the hosted Worker's page wiring", () => {
  afterEach(() => vi.unstubAllGlobals());

  class FakeHTMLRewriter {
    on(selector, handlers) {
      this.selector = selector;
      this.handlers = handlers;
      return this;
    }
    transform(response) {
      const appended = [];
      this.handlers.element({ append: (html, options) => appended.push([html, options]) });
      expect(this.selector).toBe("head");
      return injectStyle(response, appended.map(([html]) => html).join(""));
    }
  }

  async function wired() {
    vi.stubGlobal("HTMLRewriter", FakeHTMLRewriter);
    const TENANT_DB = namespace();
    for (const tenant of ["alpha", "bravo"]) await seed(TENANT_DB, tenant);
    await publish(TENANT_DB.getByName("alpha"), "2026-11-18", "morning", { savedAt: "2026-11-18T08:05:00+00:00", tenant: "alpha" });
    await publish(TENANT_DB.getByName("bravo"), "2026-11-18", "evening", { savedAt: "2026-11-18T20:05:00+00:00", tenant: "bravo" });
    TENANT_DB.gets.length = 0;
    const setScale = (tenant, scale) =>
      TENANT_DB.getByName(tenant).query("UPDATE settings SET value = ? WHERE key = 'digest.type_scale'", [JSON.stringify(scale)]);
    const sitePage = createSitePage({ TENANT_DB });
    const get = (tenant, path = "/") => sitePage(new Request(`${ORIGIN}/t/${tenant}${path}`), tenant, path);
    return { get, setScale, TENANT_DB };
  }

  it("reads a tenant's page from that tenant's object, and never from another's", async () => {
    const { get, TENANT_DB } = await wired();

    expect(await (await get("alpha")).text()).toBe(page("alpha", "2026-11-18-morning"));
    expect(await (await get("bravo")).text()).toBe(page("bravo", "2026-11-18-evening"));
    expect((await get("alpha", "/2026-11-18-evening")).status).toBe(404);

    expect(new Set(TENANT_DB.gets.map(({ name }) => name))).toEqual(new Set(["alpha", "bravo"]));
    expect(TENANT_DB.gets.every(({ options }) => options.locationHint === LOCATION_HINT)).toBe(true);
    // Nothing read for alpha's request touched bravo's object: every read of the first request names alpha.
    TENANT_DB.gets.length = 0;
    await get("alpha");
    expect(TENANT_DB.gets.map(({ name }) => name)).toEqual(expect.arrayContaining(["alpha"]));
    expect(TENANT_DB.gets.map(({ name }) => name)).not.toContain("bravo");
  });

  it("reads each tenant's type size from its own object, and holds it per tenant", async () => {
    const { get, setScale } = await wired();
    await setScale("alpha", 1.125);

    const alpha = await (await get("alpha")).text();
    const bravo = await (await get("bravo")).text();

    expect(alpha).toContain(`${typeScaleStyle(1.125)}</head>`);
    expect(bravo).toBe(page("bravo", "2026-11-18-evening"));
    // Each tenant's size is held on its own: a change to bravo's is not read as alpha's.
    await setScale("bravo", 0.875);
    await setScale("alpha", 1);
    expect(await (await get("alpha")).text()).toContain(typeScaleStyle(1.125));
    expect(await (await get("bravo")).text()).toBe(page("bravo", "2026-11-18-evening"));
  });
});
