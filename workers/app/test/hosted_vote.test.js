import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";
import { env } from "cloudflare:workers";
import { SignJWT, exportJWK, generateKeyPair } from "jose";
import FEED_CATALOG from "../../../src/cyris/onboarding_feeds.json" with { type: "json" };
import STARTING_VALUES from "../../../src/cyris/settings_starting_values.json" with { type: "json" };
import SEED from "../src/hosted_seed.json" with { type: "json" };
import worker from "../src/hosted.js";
import { SITE_POLICY, VOTE_TOKEN_META, votePolicy } from "../src/hosted_pages.js";
import { seedTenant } from "../src/hosted_seed.js";
import { SESSION_COOKIE, SESSION_TTL_MS } from "../src/hosted_session.js";
import { MAX_QUEUED_VOTES } from "../src/tenant_votes.js";
import { tenantRegistry } from "../src/tenant_registry.js";
import { FakeHTMLRewriter } from "./html_rewriter.js";
import { HOSTED_SCHEMA_SQL, SCHEMA_SQL, namespace, registryNamespace } from "./sql_storage.js";

// The hosted Worker's entry itself, over the real registry and tenant databases, as
// hosted_login.test.js builds it; only the Email Service binding, the Access certs and workerd's
// HTMLRewriter are stand-ins.
const DOMAIN = "cyris-hosted.example.workers.dev";
const ORIGIN = `https://${DOMAIN}`;
const HTML = "text/html; charset=utf-8";
const AT = new Date("2026-11-21T06:00:00Z");
const ALPHA_URL = "https://alpha.example/story";
const BRAVO_URL = "https://bravo.example/story";
const PROMOTE = "https://cyris-promote.example.workers.dev";

// The page's own inline script is a stand-in; the vote button's real one is the template's.
const page = (tenant) => `<!doctype html><html><head><title>${tenant}</title></head><body>${tenant}</body></html>`;

let bound;
let privateKey;
let fetched;
let ctx;

async function seedWithArticle(tenant, articleUrl) {
  const db = bound.TENANT_DB.getByName(tenant);
  await seedTenant({
    db,
    tenant,
    schemaSql: SCHEMA_SQL,
    hostedSchemaSql: HOSTED_SCHEMA_SQL,
    startingValues: STARTING_VALUES,
    seed: SEED,
    catalog: FEED_CATALOG,
  });
  await db.query("INSERT INTO digests (date, period, content, raw_page, saved_at) VALUES (?, ?, ?, ?, ?)", [
    "2026-11-21",
    "morning",
    "{}",
    1,
    "2026-11-21T05:05:00+00:00",
  ]);
  await db.query(
    "INSERT INTO hosted_pages (path, body, content_type, updated_at) VALUES (?, ?, ?, ?)",
    ["/2026-11-21-morning.html", page(tenant), HTML, "2026-11-21T05:05:00+00:00"],
  );
  await db.query(
    "INSERT INTO stored_articles (url, title, content, published_at, source_name, source_lane, first_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
    [articleUrl, "A story", "text", "2026-11-21T01:00:00+00:00", "Source", "read", "2026-11-21T02:00:00+00:00"],
  );
}

beforeEach(async () => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(AT);
  vi.stubGlobal("HTMLRewriter", FakeHTMLRewriter);
  const keys = await generateKeyPair("RS256");
  privateKey = keys.privateKey;
  const jwk = { ...(await exportJWK(keys.publicKey)), kid: "k1", alg: "RS256", use: "sig" };
  fetched = [];
  vi.stubGlobal("fetch", async (input) => {
    fetched.push(String(input?.url ?? input));
    return Response.json({ keys: [jwk] });
  });
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
    CYRIS_ACCESS_AUD: "AUD-VOTE",
    CYRIS_STORE_HOSTED_DOMAIN: DOMAIN,
    CYRIS_HOSTED_MAIL_FROM: "cyris@mail.example",
    // The shared promote Worker, configured as on any deployment: a hosted vote never reaches it.
    CYRIS_PROMOTE_WORKER_URL: PROMOTE,
    CYRIS_PROMOTE_TOKEN: "promote-bearer",
  });
  ctx = { pending: [], waitUntil: (promise) => ctx.pending.push(promise) };

  const registry = tenantRegistry(env);
  for (const tenant of ["alpha", "bravo"]) await registry.register(tenant);
  await seedWithArticle("alpha", ALPHA_URL);
  await seedWithArticle("bravo", BRAVO_URL);
  await registry.invite("alpha", "ana@example.com");
  await registry.invite("bravo", "bea@example.com");
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  for (const key of Object.keys(env)) delete env[key];
});

const accessJwt = () =>
  new SignJWT({})
    .setProtectedHeader({ alg: "RS256", kid: "k1" })
    .setIssuer(`https://${env.CYRIS_ACCESS_TEAM_DOMAIN}`)
    .setAudience("AUD-VOTE")
    .setIssuedAt()
    .setExpirationTime(Math.floor(Date.now() / 1000) + 3600)
    .sign(privateKey);

async function call(path, { method = "GET", session, operator = false, headers = {}, body, raw } = {}) {
  const sent = new Headers(headers);
  if (operator) sent.set("Cf-Access-Jwt-Assertion", await accessJwt());
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

async function signIn(email) {
  await call("/login", { method: "POST", headers: { Origin: ORIGIN, "Content-Type": "application/x-www-form-urlencoded" }, raw: new URLSearchParams({ email }).toString() });
  const token = /\/login\/confirm#([A-Za-z0-9_-]{43})/.exec(bound.MAIL.sent.at(-1).text)?.[1];
  const confirmed = await call("/login/confirm", {
    method: "POST",
    headers: { Origin: ORIGIN, "Content-Type": "application/x-www-form-urlencoded" },
    raw: new URLSearchParams({ token }).toString(),
  });
  return new RegExp(`^${SESSION_COOKIE}=([^;]*)`).exec(confirmed.headers.get("Set-Cookie"))[1];
}

// What a page's vote button reads: the token in the <meta> of the page the session was served.
async function voteTokenOf(session, tenant) {
  const served = await call(`/t/${tenant}/`, { session });
  const text = await served.text();
  return { served, token: new RegExp(`<meta name="${VOTE_TOKEN_META}" content="([^"]+)">`).exec(text)?.[1] };
}

// The page's own request: sandboxed, so its Origin is the literal "null", and the token rides in a header.
const castVote = (token, body, { headers = {}, path = "/api/vote", session } = {}) =>
  call(path, {
    method: "POST",
    session,
    headers: { Origin: "null", "Sec-Fetch-Site": "cross-site", ...(token ? { "X-Cyris-Vote": token } : {}), ...headers },
    body,
  });

const queued = async (tenant) => (await bound.TENANT_DB.getByName(tenant).query("SELECT url, vote, ts FROM hosted_votes ORDER BY id", [])).rows;

describe("a tenant page's vote token", () => {
  it("is in the page a session is served, which may post its votes to the vote route and nowhere else", async () => {
    const session = await signIn("ana@example.com");

    const { served, token } = await voteTokenOf(session, "alpha");

    expect(token).toBeTruthy();
    expect(served.headers.get("Content-Security-Policy")).toBe(votePolicy(ORIGIN));
    expect(votePolicy(ORIGIN)).toContain(`connect-src ${ORIGIN}/api/vote;`);
    expect(votePolicy(ORIGIN)).not.toContain("allow-same-origin");
    // Every other answer under /t/ keeps connect-src 'none', and the cookie is not in the page.
    expect((await call("/t/alpha/missing", { session })).headers.get("Content-Security-Policy")).toBe(SITE_POLICY);
    expect(await (await call("/t/alpha/", { session })).text()).not.toContain(session);
  });

  it("is never in the operator's copy of a page, which may post no vote at all", async () => {
    const served = await call("/hosted/tenants/alpha/page", { operator: true });

    expect(served.status).toBe(200);
    expect(await served.text()).not.toContain(VOTE_TOKEN_META);
    expect(served.headers.get("Content-Security-Policy")).toBe(SITE_POLICY);
    expect(SITE_POLICY).toContain("connect-src 'none'");
    // A page with no session is sent to sign in, with no page and no token.
    const unsigned = await call("/t/alpha/");
    expect(unsigned.status).toBe(302);
    expect(await unsigned.text()).not.toContain(VOTE_TOKEN_META);
  });

  it("opens no page: the token in a cookie is no session, and the session's cookie is no vote", async () => {
    const session = await signIn("ana@example.com");
    const { token } = await voteTokenOf(session, "alpha");

    expect((await call("/t/alpha/", { session: token })).status).toBe(302);
    expect((await castVote(null, { url: ALPHA_URL, vote: "up" }, { session })).status).toBe(401);
    expect(await queued("alpha")).toEqual([]);
  });
});

describe("a vote", () => {
  it("is queued in the session's tenant with the Worker's own clock, and is answered to the sandboxed page", async () => {
    const session = await signIn("ana@example.com");
    const { token } = await voteTokenOf(session, "alpha");

    const answer = await castVote(token, { url: ALPHA_URL, vote: "down", digest_date: "2026-11-21", ts: "1999-01-01T00:00:00Z" });

    expect(answer.status).toBe(200);
    expect(await answer.json()).toEqual({ ok: true });
    // The page is Origin "null": it can read the answer only because the route names it.
    expect(answer.headers.get("Access-Control-Allow-Origin")).toBe("*");
    expect(answer.headers.get("Cache-Control")).toBe("no-store");
    expect(await queued("alpha")).toEqual([{ url: ALPHA_URL, vote: "down", ts: AT.toISOString() }]);
    expect(await queued("bravo")).toEqual([]);
  });

  it("shows the buttons to a live session's page only: the probe asks with the token, and no Settings opens", async () => {
    const session = await signIn("ana@example.com");
    const { token } = await voteTokenOf(session, "alpha");
    const probe = (headers) => call("/api/vote", { headers: { Origin: "null", "Sec-Fetch-Site": "cross-site", ...headers } });

    const open = await probe({ "X-Cyris-Vote": token });
    expect(open.status).toBe(200);
    expect(await open.json()).toEqual({ authorized: true, settings: false });
    expect(open.headers.get("Access-Control-Allow-Origin")).toBe("*");
    // The cookie alone, a made-up token and another site each learn nothing.
    expect((await call("/api/vote", { session, headers: { Origin: "null" } })).status).toBe(401);
    expect((await probe({ "X-Cyris-Vote": `${token}x` })).status).toBe(401);
    expect((await probe({ "X-Cyris-Vote": token, Origin: "https://evil.example" })).status).toBe(403);
    expect(await queued("alpha")).toEqual([]);
  });

  it("is a preflight the page's own header can pass", async () => {
    const preflight = await call("/api/vote", { method: "OPTIONS", headers: { Origin: "null", "Access-Control-Request-Method": "POST" } });

    expect(preflight.status).toBe(204);
    expect(preflight.headers.get("Access-Control-Allow-Headers")).toContain("X-Cyris-Vote");
    expect(preflight.headers.get("Access-Control-Allow-Methods")).toBe("GET, POST");
  });

  it("never reaches the shared promote Worker", async () => {
    const session = await signIn("ana@example.com");
    const { token } = await voteTokenOf(session, "alpha");
    fetched.length = 0;

    await castVote(token, { url: ALPHA_URL, vote: "up" });

    expect(fetched.filter((url) => url.startsWith(PROMOTE))).toEqual([]);
    expect(fetched).toEqual([]);
  });

  it("is not taken for an article the tenant has not stored", async () => {
    const session = await signIn("ana@example.com");
    const { token } = await voteTokenOf(session, "alpha");

    for (const url of ["https://alpha.example/other", "https://alpha.example/story ", "javascript:alert(1)"]) {
      expect((await castVote(token, { url, vote: "up" })).status, url).toBe(404);
    }
    expect(await queued("alpha")).toEqual([]);
  });

  it("takes only up and down, a URL and a small JSON body", async () => {
    const session = await signIn("ana@example.com");
    const { token } = await voteTokenOf(session, "alpha");

    expect((await castVote(token, { url: ALPHA_URL, vote: "deep" })).status).toBe(400);
    expect((await castVote(token, { url: ALPHA_URL })).status).toBe(400);
    expect((await castVote(token, { url: 5, vote: "up" })).status).toBe(400);
    expect((await castVote(token, null)).status).toBe(400);
    expect((await call("/api/vote", { method: "POST", headers: { Origin: "null", "X-Cyris-Vote": token }, raw: "{not json" })).status).toBe(400);
    expect((await castVote(token, { url: ALPHA_URL, vote: "up", pad: "x".repeat(5000) })).status).toBe(413);
    expect((await call("/api/vote", { method: "PUT", headers: { "X-Cyris-Vote": token } })).status).toBe(405);
    expect(await queued("alpha")).toEqual([]);
  });

  it("is refused once a tenant's queue is full, until its run drains it", async () => {
    const session = await signIn("ana@example.com");
    const { token } = await voteTokenOf(session, "alpha");
    const db = bound.TENANT_DB.getByName("alpha");
    for (let i = 0; i < MAX_QUEUED_VOTES; i++) {
      await db.query("INSERT INTO hosted_votes (url, vote, ts) VALUES (?, 'up', ?)", [ALPHA_URL, AT.toISOString()]);
    }

    expect((await castVote(token, { url: ALPHA_URL, vote: "up" })).status).toBe(429);
    await db.query("DELETE FROM hosted_votes", []);
    expect((await castVote(token, { url: ALPHA_URL, vote: "up" })).status).toBe(200);
  });
});

describe("one tenant's session and another tenant's votes", () => {
  it("cannot cast a vote for bravo, whatever the request names", async () => {
    const ana = await signIn("ana@example.com");
    const { token } = await voteTokenOf(ana, "alpha");
    const named = {
      path: "/api/vote?tenant=bravo",
      headers: { Referer: `${ORIGIN}/t/bravo/`, "X-Cyris-Tenant": "bravo" },
    };

    // Bravo's article is not alpha's: refused, and neither queue holds it.
    expect((await castVote(token, { url: BRAVO_URL, vote: "up", tenant: "bravo", tenant_id: "bravo" }, named)).status).toBe(404);
    expect(await queued("bravo")).toEqual([]);
    expect(await queued("alpha")).toEqual([]);

    // Alpha's own article, with every way of naming bravo: it is alpha's vote.
    const own = await castVote(token, { url: ALPHA_URL, vote: "up", tenant: "bravo", tenant_id: "bravo" }, named);
    expect(own.status).toBe(200);
    expect(await queued("alpha")).toHaveLength(1);
    expect(await queued("bravo")).toEqual([]);
  });

  it("gives each reader's page a token of its own tenant: bea's votes land in bravo", async () => {
    const ana = await signIn("ana@example.com");
    const bea = await signIn("bea@example.com");
    const { token: anaToken } = await voteTokenOf(ana, "alpha");
    const { token: beaToken } = await voteTokenOf(bea, "bravo");
    expect(anaToken).not.toBe(beaToken);

    expect((await castVote(beaToken, { url: BRAVO_URL, vote: "down" })).status).toBe(200);
    expect((await castVote(beaToken, { url: ALPHA_URL, vote: "down" })).status).toBe(404);

    expect(await queued("bravo")).toHaveLength(1);
    expect(await queued("alpha")).toEqual([]);
    // And a session cannot be served the other's page, so can never read the other's token.
    expect((await call("/t/alpha/", { session: bea })).status).toBe(404);
  });

  it("refuses a token whose session hash is another's, however the rest is made up", async () => {
    const ana = await signIn("ana@example.com");
    const bea = await signIn("bea@example.com");
    const { token: anaToken } = await voteTokenOf(ana, "alpha");
    const { token: beaToken } = await voteTokenOf(bea, "bravo");
    const [anaHash, anaMac] = anaToken.split(".");
    const [beaHash, beaMac] = beaToken.split(".");

    for (const forged of [`${beaHash}.${anaMac}`, `${anaHash}.`, anaHash, `${anaHash}.${anaMac}.x`, `${beaHash}.${"0".repeat(64)}`, ".", ""]) {
      expect((await castVote(forged, { url: BRAVO_URL, vote: "up" })).status, forged).toBe(401);
    }
    expect(beaMac).not.toBe(anaMac);
    expect(await queued("bravo")).toEqual([]);
  });
});

describe("a vote and forged requests", () => {
  it("is not cast by a cross-site form or fetch carrying the reader's cookie", async () => {
    const session = await signIn("ana@example.com");
    const { token } = await voteTokenOf(session, "alpha");
    const vote = { url: ALPHA_URL, vote: "up" };

    // The cookie alone, as a form on another site sends it: no token, so no vote.
    expect((await call("/api/vote", { method: "POST", session, headers: { Origin: "https://evil.example", "Sec-Fetch-Site": "cross-site", "Content-Type": "text/plain" }, raw: JSON.stringify(vote) })).status).toBe(403);
    expect((await call("/api/vote", { method: "POST", session, raw: JSON.stringify(vote) })).status).toBe(401);
    // Even a page that learned the token, from a browser that names its site.
    expect((await castVote(token, vote, { headers: { Origin: "https://evil.example" }, session })).status).toBe(403);
    // A browser that sends no Origin but says the request is cross-site.
    const noOrigin = await call("/api/vote", { method: "POST", headers: { "Sec-Fetch-Site": "cross-site", "X-Cyris-Vote": token }, body: vote });
    expect(noOrigin.status).toBe(403);
    expect(await queued("alpha")).toEqual([]);
  });

  it("is taken from the Worker's own origin, and from a client that names no origin", async () => {
    const session = await signIn("ana@example.com");
    const { token } = await voteTokenOf(session, "alpha");

    const sameOrigin = await call("/api/vote", { method: "POST", headers: { Origin: ORIGIN, "Sec-Fetch-Site": "same-origin", "X-Cyris-Vote": token }, body: { url: ALPHA_URL, vote: "up" } });
    const curl = await call("/api/vote", { method: "POST", headers: { "X-Cyris-Vote": token }, body: { url: ALPHA_URL, vote: "down" } });

    expect([sameOrigin.status, curl.status]).toEqual([200, 200]);
  });

  it("ends with the session: sign-out, revoke and expiry each end the token at the next vote", async () => {
    const registry = tenantRegistry(env);
    const out = await signIn("ana@example.com");
    const revoked = await signIn("ana@example.com");
    const { token: outToken } = await voteTokenOf(out, "alpha");
    const { token: revokedToken } = await voteTokenOf(revoked, "alpha");
    expect((await castVote(outToken, { url: ALPHA_URL, vote: "up" })).status).toBe(200);

    await call("/logout", { method: "POST", session: out, headers: { Origin: ORIGIN } });
    const afterLogout = await castVote(outToken, { url: ALPHA_URL, vote: "up" });
    expect(afterLogout.status).toBe(401);
    expect(await afterLogout.json()).toEqual({ error: "sign in again to vote", code: "vote_session_ended" });
    // The other session of the same reader is a session of its own.
    expect((await castVote(revokedToken, { url: ALPHA_URL, vote: "up" })).status).toBe(200);

    await registry.revoke("alpha", "ana@example.com");
    expect((await castVote(revokedToken, { url: ALPHA_URL, vote: "up" })).status).toBe(401);

    vi.setSystemTime(new Date(AT.getTime() + SESSION_TTL_MS + 1000));
    expect((await castVote(revokedToken, { url: ALPHA_URL, vote: "up" })).status).toBe(401);
    expect(await queued("alpha")).toHaveLength(2);
  });
});

describe("the registry's vote token", () => {
  it("is made for a live session only, and a new registry key ends every token", async () => {
    const registry = tenantRegistry(env);
    const session = await signIn("ana@example.com");
    const { token } = await voteTokenOf(session, "alpha");

    expect(await registry.voteToken("0".repeat(64))).toBeNull();
    expect((await registry.voteSession(token)).tenant_id).toBe("alpha");

    const sql = bound.REGISTRY.getByName("cyris:registry").ctx.storage.sql;
    sql.exec("UPDATE vote_key SET key = 'rotated'");
    expect(await registry.voteSession(token)).toBeNull();
  });
});
