import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";
import { env } from "cloudflare:workers";
import { generateKeyPair, exportJWK, SignJWT } from "jose";
import FEED_CATALOG from "../../../src/cyris/onboarding_feeds.json" with { type: "json" };
import STARTING_VALUES from "../../../src/cyris/settings_starting_values.json" with { type: "json" };
import SEED from "../src/hosted_seed.json" with { type: "json" };
import worker from "../src/hosted.js";
import { createSignIn, signInMail } from "../src/hosted_login.js";
import { SITE_POLICY, votePolicy } from "../src/hosted_pages.js";
import { seedTenant } from "../src/hosted_seed.js";
import {
  SESSION_COOKIE,
  SESSION_TTL_MS,
  SIGN_IN_ADDRESS_CAP,
  SIGN_IN_DAILY_CEILING,
  SIGN_IN_GAP_MS,
  SIGN_IN_HOUR_MS,
  SIGN_IN_TENANT_CAP,
  SIGN_IN_TTL_MS,
  tokenHash,
} from "../src/hosted_session.js";
import { tenantRegistry } from "../src/tenant_registry.js";
import { FakeHTMLRewriter } from "./html_rewriter.js";
import { HOSTED_SCHEMA_SQL, SCHEMA_SQL, namespace, registryNamespace } from "./sql_storage.js";

// The hosted Worker's entry itself, hosted.js, over the real registry and tenant databases; only
// the Email Service binding, the Access certs and the runtime's waitUntil are stand-ins.
const DOMAIN = "cyris-hosted.example.workers.dev";
const ORIGIN = `https://${DOMAIN}`;
const SENDER = "cyris@mail.example";
const AUD = "AUD-LOGIN";
// The service token the operator signs in with: its Client ID, which Access signs in as `common_name`.
const OPERATOR = "operator-token.access";
const HTML = "text/html; charset=utf-8";
const AT = new Date("2026-11-20T06:00:00Z");

const page = (tenant, slug) =>
  `<!doctype html><html><head><meta name="cyris-issue" content="${slug}"><title>${tenant}</title></head>` +
  `<body>${tenant}'s ${slug}</body></html>`;

// A page served to a session carries its vote token in a <meta> (hosted_vote.test.js); the page
// itself is what a test compares.
const withoutVoteToken = (html) => html.replace(/<meta name="cyris-vote-token" content="[^"]+">/, "");

const UPSERT =
  "INSERT INTO hosted_pages (path, body, content_type, updated_at) VALUES (?, ?, ?, ?) " +
  "ON CONFLICT(path) DO UPDATE SET body = excluded.body, content_type = excluded.content_type";

// One issue of `tenant`, its digest page and its raw page, as the run's HostedPages stores them.
async function publish(db, tenant, slug, savedAt) {
  const [date, period] = [slug.slice(0, 10), slug.slice(11)];
  await db.query("INSERT INTO digests (date, period, content, raw_page, saved_at) VALUES (?, ?, ?, ?, ?)", [
    date,
    period,
    "{}",
    1,
    savedAt,
  ]);
  await db.query(UPSERT, [`/${slug}-raw.html`, `<html><head></head><body>raw ${tenant}</body></html>`, HTML, savedAt]);
  await db.query(UPSERT, [`/${slug}.html`, page(tenant, slug), HTML, savedAt]);
}

let bound;
let privateKey;
let ctx;

beforeEach(async () => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(AT);
  // A page served to a session carries its vote token, which workerd's HTMLRewriter puts in <head>.
  vi.stubGlobal("HTMLRewriter", FakeHTMLRewriter);
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
    CYRIS_ACCESS_AUD: AUD,
    CYRIS_HOSTED_OPERATORS: OPERATOR,
    CYRIS_STORE_HOSTED_DOMAIN: DOMAIN,
    CYRIS_HOSTED_MAIL_FROM: SENDER,
  });
  ctx = { pending: [], waitUntil: (promise) => ctx.pending.push(promise) };

  // alpha and bravo each have an issue; charlie is registered with none. ana reads alpha, bea bravo.
  const registry = tenantRegistry(env);
  for (const tenant of ["alpha", "bravo", "charlie"]) await registry.register(tenant);
  for (const [tenant, slug, savedAt] of [
    ["alpha", "2026-11-19-evening", "2026-11-19T20:05:00+00:00"],
    ["alpha", "2026-11-20-morning", "2026-11-20T05:05:00+00:00"],
    ["bravo", "2026-11-20-morning", "2026-11-20T05:10:00+00:00"],
  ]) {
    const db = bound.TENANT_DB.getByName(tenant);
    if (!(await db.query("SELECT name FROM sqlite_master WHERE name = 'hosted_pages'", [])).rows.length) {
      await seedTenant({
        db,
        tenant,
        schemaSql: SCHEMA_SQL,
        hostedSchemaSql: HOSTED_SCHEMA_SQL,
        startingValues: STARTING_VALUES,
        seed: SEED,
        catalog: FEED_CATALOG,
      });
    }
    await publish(db, tenant, slug, savedAt);
  }
  await registry.invite("alpha", "ana@example.com");
  await registry.invite("bravo", "bea@example.com");
  bound.TENANT_DB.gets.length = 0;
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  for (const key of Object.keys(env)) delete env[key];
});

const accessJwt = () =>
  new SignJWT({ common_name: OPERATOR })
    .setProtectedHeader({ alg: "RS256", kid: "k1" })
    .setIssuer(`https://${env.CYRIS_ACCESS_TEAM_DOMAIN}`)
    .setAudience(AUD)
    .setIssuedAt()
    .setExpirationTime(Math.floor(Date.now() / 1000) + 3600)
    .sign(privateKey);

// One request through hosted.js, and whatever it left to waitUntil. A browser's POST carries its
// Origin, so `form` sets it; `session` is a cookie, `operator` an Access JWT, and `text` a body
// sent as text/plain, which a page on any site may post without asking.
async function call(path, { method = "GET", session, operator = false, form, body, text, headers = {} } = {}) {
  const sent = new Headers(headers);
  if (session) sent.set("Cookie", `${SESSION_COOKIE}=${session}`);
  if (operator) sent.set("Cf-Access-Jwt-Assertion", await accessJwt());
  let payload;
  if (form) {
    sent.set("Content-Type", "application/x-www-form-urlencoded");
    sent.set("Origin", sent.get("Origin") ?? ORIGIN);
    payload = new URLSearchParams(form).toString();
  }
  if (body) {
    sent.set("Content-Type", "application/json");
    payload = JSON.stringify(body);
  }
  if (text !== undefined) {
    sent.set("Content-Type", "text/plain");
    payload = text;
  }
  const response = await worker.fetch(new Request(`${ORIGIN}${path}`, { method, headers: sent, body: payload }), env, ctx);
  await Promise.all(ctx.pending.splice(0));
  return response;
}

const linkToken = (mail) => /\/login\/confirm#([A-Za-z0-9_-]{43})/.exec(mail.text)?.[1];
const cookieOf = (response) => new RegExp(`^${SESSION_COOKIE}=([^;]*)`).exec(response.headers.get("Set-Cookie") ?? "")?.[1];

// Asks for a link for `email`, opens it, and answers the session cookie and what it took.
async function signIn(email) {
  const asked = await call("/login", { method: "POST", form: { email } });
  const mail = bound.MAIL.sent.at(-1);
  const token = linkToken(mail);
  const confirmed = await call("/login/confirm", { method: "POST", form: { token } });
  return { asked, mail, token, confirmed, session: cookieOf(confirmed) };
}

const rows = (table) => bound.REGISTRY.getByName("cyris:registry").ctx.storage.sql.exec(`SELECT * FROM ${table}`).toArray();

describe("signing in to a hosted tenant", () => {
  it("mails a link to an address on the list and none to one off it, with the same answer", async () => {
    const listed = await call("/login", { method: "POST", form: { email: "Ana@Example.com" } });
    const unlisted = await call("/login", { method: "POST", form: { email: "stranger@example.com" } });

    expect([listed.status, unlisted.status]).toEqual([200, 200]);
    expect(await listed.text()).toBe(await unlisted.text());
    expect(bound.MAIL.sent).toHaveLength(1);
    const [mail] = bound.MAIL.sent;
    expect(mail).toMatchObject({ to: "ana@example.com", from: SENDER, subject: "Your cyris sign-in link" });
    expect(mail.text).toContain(`https://${DOMAIN}/login/confirm#`);
    expect(mail.text).toContain(`https://${DOMAIN}/login`);
    expect(rows("invites").map((row) => row.email)).toEqual(["ana@example.com"]);
  });

  it("opens the tenant the registry lists the address for, whatever tenant the request names", async () => {
    await call("/login", { method: "POST", form: { email: "ana@example.com", tenant: "bravo" } });
    const token = linkToken(bound.MAIL.sent.at(-1));

    const confirmed = await call("/login/confirm", { method: "POST", form: { token, tenant: "bravo" } });

    expect(confirmed.headers.get("Location")).toBe("/t/alpha/");
    expect(rows("sessions").map((row) => [row.email, row.tenant_id])).toEqual([["ana@example.com", "alpha"]]);
    expect((await call("/t/bravo/", { session: cookieOf(confirmed) })).status).toBe(404);
  });

  it("opens the link into the reader's own latest digest, through a URL with no token", async () => {
    const { token, confirmed, session } = await signIn("ana@example.com");

    expect(confirmed.status).toBe(303);
    expect(confirmed.headers.get("Location")).toBe("/t/alpha/");
    expect(confirmed.headers.get("Location")).not.toContain(token);
    const latest = await call("/t/alpha/", { session });
    expect(latest.status).toBe(200);
    expect(withoutVoteToken(await latest.text())).toBe(page("alpha", "2026-11-20-morning"));
    expect((await call("/t/alpha/2026-11-19-evening", { session })).status).toBe(200);
    expect((await call("/t/alpha/2026-11-20-morning-raw", { session })).status).toBe(200);
  });

  it("sets the session cookie HttpOnly, Secure, SameSite=Lax and host-only, with its expiry", async () => {
    const { confirmed } = await signIn("ana@example.com");
    const cookie = confirmed.headers.get("Set-Cookie");

    expect(cookie).toMatch(new RegExp(`^__Host-reader_session=[A-Za-z0-9_-]{43}; Max-Age=${SESSION_TTL_MS / 1000}; `));
    for (const attribute of ["Path=/", "Secure", "HttpOnly", "SameSite=Lax"]) expect(cookie).toContain(`; ${attribute}`);
    expect(cookie).not.toMatch(/Domain=/i);
  });

  it("keeps no token in the registry, and logs none", async () => {
    const lines = [];
    for (const level of ["log", "warn", "error"]) {
      vi.spyOn(console, level).mockImplementation((...args) => lines.push(args.map(String).join(" ")));
    }

    const { token, session } = await signIn("ana@example.com");
    await call("/t/alpha/", { session });
    await call("/logout", { method: "POST", session, form: {} });

    const [link] = rows("invites");
    expect(link.code).toBe(await tokenHash(token));
    const stored = JSON.stringify([rows("invites"), rows("sessions"), rows("members")]);
    expect(stored).not.toContain(token);
    expect(stored).not.toContain(session);
    expect(lines.join("\n")).not.toContain(token);
    expect(lines.join("\n")).not.toContain(session);
  });

  // Under the response's `Referrer-Policy: no-referrer` a browser posts a form with `Origin: null`
  // (Fetch, "append a request `Origin` header"), which crossSite refuses: the page must declare a
  // policy of its own that sends this origin to itself, and still nothing elsewhere.
  it("lets each of its forms post this Origin, and still sends no referrer to another site", async () => {
    const pages = [
      await call("/login"),
      await call("/login/confirm"),
      await call("/logout"),
    ];

    for (const served of pages) {
      const html = await served.text();
      expect(html).toContain("<form");
      expect(html).toContain('<meta name="referrer" content="same-origin">');
      expect(served.headers.get("Referrer-Policy")).toBe("no-referrer");
    }
  });

  it("asks for a press before it spends the link, and keeps the token out of every URL it serves", async () => {
    const opened = await call("/login/confirm");
    const html = await opened.text();

    expect(opened.status).toBe(200);
    expect(html).toContain('<form id="confirm" method="post" action="/login/confirm" hidden>');
    expect(html).toContain("history.replaceState(null, '', location.pathname)");
    expect(opened.headers.get("Referrer-Policy")).toBe("no-referrer");
    expect(opened.headers.get("Cache-Control")).toBe("no-store");
    // The one script is allowed by its hash; nothing else may run there.
    expect(opened.headers.get("Content-Security-Policy")).toMatch(/script-src 'sha256-[A-Za-z0-9+/]+=*'/);
    expect(rows("sessions")).toEqual([]);
  });

  it("refuses a link opened a second time", async () => {
    const { token } = await signIn("ana@example.com");

    const again = await call("/login/confirm", { method: "POST", form: { token } });

    expect(again.status).toBe(400);
    expect(again.headers.get("Set-Cookie")).toBeNull();
    expect(rows("sessions")).toHaveLength(1);
  });

  it("refuses a link past its 15 minutes", async () => {
    await call("/login", { method: "POST", form: { email: "ana@example.com" } });
    const token = linkToken(bound.MAIL.sent.at(-1));
    vi.setSystemTime(new Date(AT.getTime() + SIGN_IN_TTL_MS + 1000));

    const late = await call("/login/confirm", { method: "POST", form: { token } });

    expect(late.status).toBe(400);
    expect(late.headers.get("Set-Cookie")).toBeNull();
    expect(rows("sessions")).toEqual([]);
  });

  it("sends a session past its 14 days to sign in again, and clears its cookie", async () => {
    const { session } = await signIn("ana@example.com");
    vi.setSystemTime(new Date(AT.getTime() + SESSION_TTL_MS + 1000));

    const expired = await call("/t/alpha/", { session });

    expect(expired.status).toBe(302);
    expect(expired.headers.get("Location")).toBe("/login");
    expect(expired.headers.get("Set-Cookie")).toMatch(new RegExp(`^${SESSION_COOKIE}=; Max-Age=0; `));
  });

  it("ends a session at its next request once it signs out, or once the operator revokes its address", async () => {
    const ana = await signIn("ana@example.com");
    const bea = await signIn("bea@example.com");

    const out = await call("/logout", { method: "POST", session: ana.session, form: {} });
    expect(out.status).toBe(303);
    expect(out.headers.get("Set-Cookie")).toMatch(/Max-Age=0/);
    expect((await call("/t/alpha/", { session: ana.session })).status).toBe(302);

    expect((await call("/t/bravo/", { session: bea.session })).status).toBe(200);
    const revoked = await call("/hosted/tenants/bravo/revoke", { method: "POST", operator: true, body: { email: "bea@example.com" } });
    expect(revoked.status).toBe(200);
    expect(await revoked.json()).toEqual({ email: "bea@example.com", tenant_id: "bravo", sessions: 1 });
    expect((await call("/t/bravo/", { session: bea.session })).status).toBe(302);
  });

  it("refuses a sign-in form posted from another site", async () => {
    for (const headers of [{ Origin: "https://evil.example" }, { Origin: "null" }, { "Sec-Fetch-Site": "cross-site" }]) {
      const response = await call("/login", { method: "POST", form: { email: "ana@example.com" }, headers });
      expect(response.status).toBe(403);
    }
    expect(bound.MAIL.sent).toEqual([]);
  });

  it("mails an address at most once a minute, however often it asks", async () => {
    for (let i = 0; i < 3; i++) await call("/login", { method: "POST", form: { email: "ana@example.com" } });
    vi.setSystemTime(new Date(AT.getTime() + SIGN_IN_GAP_MS + 1000));
    await call("/login", { method: "POST", form: { email: "ana@example.com" } });

    expect(bound.MAIL.sent).toHaveLength(2);
  });
});

describe("the caps on sign-in mail", () => {
  const registry = () => bound.REGISTRY.getByName("cyris:registry");
  // Every part of an answer a client sees: status, headers and body.
  const seen = async (response) => [response.status, [...response.headers], await response.text()];
  const ask = (email) => call("/login", { method: "POST", form: { email } });
  const at = (ms) => vi.setSystemTime(new Date(AT.getTime() + ms));
  const reInvite = (tenant, email) => call(`/hosted/tenants/${tenant}/invite`, { method: "POST", operator: true, body: { email } });
  const mailedTo = (email) => bound.MAIL.sent.filter((mail) => mail.to === email).length;
  // A stranger asks in `email`'s name `times` times, each a gap apart, from `from` ms on.
  async function flood(email, times, from = 0) {
    for (let i = 0; i < times; i++) {
      at(from + i * (SIGN_IN_GAP_MS + 1000));
      await ask(email);
    }
  }
  beforeEach(() => vi.spyOn(console, "warn").mockImplementation(() => {}));

  it("let a reader a stranger asked for five times sign in again within the hour, and be re-invited at once (Q1, Q3)", async () => {
    await flood("ana@example.com", 5);
    expect(mailedTo("ana@example.com")).toBe(SIGN_IN_ADDRESS_CAP);
    // No form field makes an ask an invitation.
    await call("/login", { method: "POST", form: { email: "ana@example.com", invitation: "true" } });
    expect(mailedTo("ana@example.com")).toBe(SIGN_IN_ADDRESS_CAP);

    // The operator re-invites at once, with no revoke, and the link opens alpha.
    const invited = await reInvite("alpha", "ana@example.com");
    expect(invited.status).toBe(200);
    expect((await invited.json()).mail).toMatchObject({ sent: true, tenant: "alpha" });
    const opened = await call("/login/confirm", { method: "POST", form: { token: linkToken(bound.MAIL.sent.at(-1)) } });
    expect(opened.headers.get("Location")).toBe("/t/alpha/");

    // And ana's own ask an hour after the flood's first is mailed.
    const sent = bound.MAIL.sent.length;
    at(SIGN_IN_HOUR_MS + 1000);
    await ask("ana@example.com");
    expect(bound.MAIL.sent).toHaveLength(sent + 1);
    expect(bound.MAIL.sent.at(-1).to).toBe("ana@example.com");
  });

  it("keep one tenant's flood from every other tenant's readers and from the operator's invitations (Q2)", async () => {
    // Six of alpha's addresses asked for five times each, as the review's global lockout did.
    const known = Array.from({ length: 6 }, (_, i) => `known${i}@example.com`);
    for (const email of known) registry().invite("alpha", email);
    for (const email of known) await flood(email, 5);
    expect(bound.MAIL.sent.length).toBeLessThanOrEqual(SIGN_IN_TENANT_CAP);
    const sent = bound.MAIL.sent.length;

    await ask("ana@example.com");
    expect(bound.MAIL.sent).toHaveLength(sent);
    // bravo's reader is mailed, and the operator still invites to alpha.
    await ask("bea@example.com");
    expect(bound.MAIL.sent.at(-1).to).toBe("bea@example.com");
    expect((await (await reInvite("alpha", "ana@example.com")).json()).mail).toMatchObject({ sent: true });
    // A cohort the operator invites at once is mailed whole, and takes none of its tenant's hour:
    // once the gap has passed, each of its readers may still ask.
    const cohort = Array.from({ length: SIGN_IN_TENANT_CAP * 3 }, (_, i) => `cohort${i}@example.com`);
    for (const email of cohort) expect((await (await reInvite("charlie", email)).json()).mail).toMatchObject({ sent: true });
    at(5 * (SIGN_IN_GAP_MS + 1000));
    for (const email of cohort.slice(0, SIGN_IN_TENANT_CAP)) await ask(email);
    expect(cohort.slice(0, SIGN_IN_TENANT_CAP).map(mailedTo)).toEqual(Array(SIGN_IN_TENANT_CAP).fill(2));
  });

  it("answer an ask past any cap as they answer an address off the list, and mail none", async () => {
    const unlisted = async () => seen(await ask("stranger@example.com"));
    const held = async (email) => {
      const before = bound.MAIL.sent.length;
      const answer = await seen(await ask(email));
      expect(bound.MAIL.sent).toHaveLength(before);
      return answer;
    };
    // The address's cap.
    await flood("ana@example.com", SIGN_IN_ADDRESS_CAP);
    at(SIGN_IN_ADDRESS_CAP * (SIGN_IN_GAP_MS + 1000));
    expect(await held("ana@example.com")).toEqual(await unlisted());
    // The tenant's cap, reached by bravo's readers.
    for (let i = 0; i < SIGN_IN_TENANT_CAP; i++) {
      registry().invite("bravo", `b${i}@example.com`);
      await ask(`b${i}@example.com`);
    }
    expect(await held("bea@example.com")).toEqual(await unlisted());
    // The deployment's ceiling, reached by asks of other tenants.
    const sql = registry().ctx.storage.sql;
    for (let i = 0; i < SIGN_IN_DAILY_CEILING; i++) {
      sql.exec("INSERT INTO sign_in_asks (email, tenant_id, asked_at) VALUES (?, ?, ?)", `r${i}@example.com`, `t${i}`, "2026-11-20T06:00:00+00:00");
    }
    registry().invite("charlie", "cy@example.com");
    expect(await held("cy@example.com")).toEqual(await unlisted());

    const lines = console.warn.mock.calls.flat().join("\n");
    expect(lines.split("\n")).toEqual(["address", "tenant", "daily"].map((cap) => `sign-in mail held: the ${cap} cap on asking is reached`));
    expect(lines).not.toMatch(/@/);
  });

  it("leave the registry as it was however many addresses off the list ask", async () => {
    const tables = ["members", "invites", "sessions", "tenants", "sign_in_asks"];
    const before = JSON.stringify(tables.map(rows));

    for (let i = 0; i < SIGN_IN_TENANT_CAP * 6; i++) await ask(`stranger${i}@example.com`);

    expect(JSON.stringify(tables.map(rows))).toBe(before);
    expect(bound.MAIL.sent).toEqual([]);
    await ask("ana@example.com");
    expect(bound.MAIL.sent).toHaveLength(1);
  });
});

describe("a tenant session and the paths of other tenants", () => {
  // Every shape of path a reader could name for bravo, and for a tenant that is not registered.
  const ELSEWHERE = [
    "/t/bravo/",
    "/t/bravo",
    "/t/bravo/2026-11-20-morning",
    "/t/bravo/2026-11-20-morning-raw.html",
    "/t/bravo/index.html",
    "/t/charlie/",
    "/t/delta/",
    "/t/delta",
    "/t/delta/2026-11-20-morning",
  ];

  it("refuse alpha's session every path of another tenant, with one answer whether it exists or not", async () => {
    const { session } = await signIn("ana@example.com");
    bound.TENANT_DB.gets.length = 0;

    const answers = [];
    for (const path of ELSEWHERE) {
      const response = await call(path, { session });
      answers.push([response.status, await response.text()]);
    }

    expect(new Set(answers.map(JSON.stringify))).toEqual(new Set([JSON.stringify([404, '{"error":"not found"}'])]));
    // None of those requests opened any tenant's object.
    expect(bound.TENANT_DB.gets).toEqual([]);
  });

  it("send a request with no session to sign in, whichever tenant it names, before looking any up", async () => {
    const registry = bound.REGISTRY.getByName("cyris:registry");
    const lookups = vi.spyOn(registry, "tenant");

    for (const path of ["/t/alpha/", "/t/bravo/2026-11-20-morning", "/t/delta/"]) {
      const response = await call(path);
      expect(response.status).toBe(302);
      expect(response.headers.get("Location")).toBe("/login");
    }
    // A cookie that is no session's is treated the same, and an operator's JWT opens no page.
    expect((await call("/t/alpha/", { session: "x".repeat(43) })).status).toBe(302);
    expect((await call("/t/alpha/", { operator: true })).status).toBe(302);
    expect(lookups).not.toHaveBeenCalled();
    expect(bound.TENANT_DB.gets).toEqual([]);
  });

  it("serve bravo's reader bravo's page, and never alpha's", async () => {
    const { session } = await signIn("bea@example.com");

    expect(withoutVoteToken(await (await call("/t/bravo/", { session })).text())).toBe(page("bravo", "2026-11-20-morning"));
    expect((await call("/t/alpha/", { session })).status).toBe(404);
    expect((await call("/t/alpha/2026-11-20-morning", { session })).status).toBe(404);
  });
});

describe("the operator's routes and a tenant session", () => {
  const OPERATOR_ROUTES = [
    ["POST", "/hosted/tenants/alpha/register"],
    ["POST", "/hosted/tenants/alpha/seed"],
    ["POST", "/hosted/tenants/alpha/run?period=morning"],
    ["POST", "/hosted/tenants/alpha/poll"],
    ["GET", "/hosted/tenants/alpha/report"],
    ["GET", "/hosted/tenants/alpha/page"],
    ["POST", "/hosted/tenants/alpha/invite"],
    ["POST", "/hosted/tenants/alpha/revoke"],
    ["GET", "/hosted/tenants/alpha/members"],
    ["GET", "/hosted/tenants/bravo/report"],
  ];

  it("never let a tenant session through /hosted/, even its own tenant's routes", async () => {
    const { session } = await signIn("ana@example.com");
    bound.TENANT_DB.gets.length = 0;
    const sent = bound.MAIL.sent.length;

    for (const [method, path] of OPERATOR_ROUTES) {
      const response = await call(path, { method, session, body: method === "POST" ? { email: "ana@example.com" } : undefined });
      expect(response.status, path).toBe(403);
      expect((await response.json()).error).toBe("forbidden");
    }
    expect(bound.TENANT_DB.gets).toEqual([]);
    expect(bound.MAIL.sent).toHaveLength(sent);
    expect(rows("members").map((row) => row.email)).toEqual(["ana@example.com", "bea@example.com"]);
  });

  // The routes the other hosted cards added beside the sign-in: the operator's query route into a
  // tenant's database above all, which a session of that very tenant must not open.
  it("refuse a tenant session the query route into its own tenant, and every route the other cards added", async () => {
    const { session } = await signIn("ana@example.com");
    bound.TENANT_DB.gets.length = 0;
    const query = { sql: "SELECT key, value FROM settings", params: [] };

    const own = await call("/hosted/tenants/alpha/query", { method: "POST", session, body: query });
    expect(own.status).toBe(403);
    expect((await own.json()).error).toBe("forbidden");

    for (const [method, path, body] of [
      ["POST", "/hosted/tenants/bravo/query", query],
      ["POST", "/hosted/tenants/alpha/schedule?times=07:00,19:00&timezone=UTC"],
      ["GET", "/hosted/tenants/alpha/address"],
      ["POST", "/hosted/tenants/alpha/rotate-address"],
      ["POST", "/hosted/tenants/alpha/revoke-address"],
      ["POST", "/hosted/tenants/alpha/newsletter-source?name=Letter&from=letter@news.example"],
    ]) {
      const response = await call(path, { method, session, body });
      expect(response.status, path).toBe(403);
      expect((await response.json()).error, path).toBe("forbidden");
    }
    expect(bound.TENANT_DB.gets).toEqual([]);
    expect(rows("inbound_addresses")).toEqual([]);
    expect(await tenantRegistry(env).tenant("alpha")).toMatchObject({ digest_times: null, timezone: null });

    // The same query with the operator's Access sign-in is answered, so the 403 was the credential's.
    const operator = await call("/hosted/tenants/alpha/query", { method: "POST", operator: true, body: query });
    expect(operator.status).toBe(200);
  });

  it("keep /hosted/ behind the Access JWT alone once Access is off /t/", async () => {
    // With Access only on /hosted/*, a /t/ request reaches the Worker with no JWT: it is served to
    // its session. A /hosted/ request with no JWT is refused, cookie or not; with one, it is answered.
    const { session } = await signIn("ana@example.com");

    expect((await call("/t/alpha/", { session })).status).toBe(200);
    expect((await call("/hosted/tenants/alpha/report")).status).toBe(403);
    expect((await call("/hosted/tenants/alpha/report", { session })).status).toBe(403);
    expect((await call("/hosted/tenants/alpha/report", { operator: true })).status).toBe(200);
    expect((await call("/hosted/tenants/alpha/report", { operator: true, session })).status).toBe(200);
  });

  it("invite an address to one tenant only, mail it its first link, and list it", async () => {
    const invited = await call("/hosted/tenants/charlie/invite", { method: "POST", operator: true, body: { email: "Cy@Example.com" } });

    expect(invited.status).toBe(200);
    expect(await invited.json()).toMatchObject({ email: "cy@example.com", tenant_id: "charlie", mail: { sent: true, tenant: "charlie" } });
    expect(bound.MAIL.sent.at(-1).to).toBe("cy@example.com");
    const opened = await call("/login/confirm", { method: "POST", form: { token: linkToken(bound.MAIL.sent.at(-1)) } });
    expect(opened.headers.get("Location")).toBe("/t/charlie/");

    const elsewhere = await call("/hosted/tenants/alpha/invite", { method: "POST", operator: true, body: { email: "cy@example.com" } });
    expect(elsewhere.status).toBe(409);
    const unregistered = await call("/hosted/tenants/delta/invite", { method: "POST", operator: true, body: { email: "d@example.com" } });
    expect(unregistered.status).toBe(404);
    const noAddress = await call("/hosted/tenants/alpha/invite", { method: "POST", operator: true, body: {} });
    expect(noAddress.status).toBe(400);

    const members = await call("/hosted/tenants/charlie/members", { operator: true });
    expect(await members.json()).toEqual([{ email: "cy@example.com", created_at: "2026-11-20T06:00:00+00:00", sessions: 1 }]);
  });

  it("answer an invite the Worker could not mail with 502, the address still on the list", async () => {
    delete env.CYRIS_HOSTED_MAIL_FROM;

    const invited = await call("/hosted/tenants/charlie/invite", { method: "POST", operator: true, body: { email: "cy@example.com" } });

    expect(invited.status).toBe(502);
    expect((await invited.json()).mail.error).toContain("CYRIS_HOSTED_MAIL_FROM");
    expect(rows("members").map((row) => row.email)).toContain("cy@example.com");
    expect(bound.MAIL.sent).toEqual([]);
  });

  it("refuse an invite or a revoke posted from another site with the operator's sign-in, writing and mailing nothing", async () => {
    // The review's repro: a page elsewhere posts a text/plain form while the operator's browser
    // holds the Access cookie (SameSite=None), so the request arrives signed in.
    const forged = { Origin: "https://evil.example", "Sec-Fetch-Site": "cross-site" };
    const before = JSON.stringify([rows("members"), rows("invites"), rows("sessions")]);

    for (const headers of [forged, { Origin: "https://evil.example" }, { Origin: "null" }, { "Sec-Fetch-Site": "same-site" }]) {
      const text = JSON.stringify({ email: "attacker@evil.example" });
      const invited = await call("/hosted/tenants/alpha/invite", { method: "POST", operator: true, text, headers });
      expect(invited.status).toBe(403);
      const revoked = await call("/hosted/tenants/alpha/revoke", {
        method: "POST",
        operator: true,
        text: JSON.stringify({ email: "ana@example.com" }),
        headers,
      });
      expect(revoked.status).toBe(403);
    }

    expect(JSON.stringify([rows("members"), rows("invites"), rows("sessions")])).toBe(before);
    expect(bound.MAIL.sent).toEqual([]);
    // From the Worker's own origin, as a browser on this site sends it, and as curl sends it, with
    // no Origin and no Sec-Fetch-Site, the operator's invite still goes through.
    const sameSite = { Origin: ORIGIN, "Sec-Fetch-Site": "same-origin" };
    const here = await call("/hosted/tenants/charlie/invite", { method: "POST", operator: true, body: { email: "cy@example.com" }, headers: sameSite });
    expect(here.status).toBe(200);
    const curl = await call("/hosted/tenants/charlie/revoke", { method: "POST", operator: true, body: { email: "cy@example.com" } });
    expect(curl.status).toBe(200);
  });

  it("refuse every operator POST route, posted from another site with the operator's sign-in, changing nothing", async () => {
    // The review's finding: the query route runs any SQL for whoever passes Access, and a page on
    // another site can post it a text/plain body while the operator's browser holds the Access cookie.
    const alpha = bound.TENANT_DB.getByName("alpha");
    const ran = vi.spyOn(alpha, "query");
    bound.TENANT_DB.gets.length = 0;
    const registryRows = () => JSON.stringify(["tenants", "runs", "inbound_addresses", "members"].map((table) => rows(table)));
    const polled = vi.spyOn(globalThis, "fetch");
    const before = registryRows();
    const sql = { sql: "DELETE FROM digests", params: [] };

    for (const headers of [
      { Origin: "https://evil.example", "Sec-Fetch-Site": "cross-site" },
      { Origin: "https://evil.example" },
      { Origin: "null" },
      { "Sec-Fetch-Site": "same-site" },
    ]) {
      for (const path of [
        // The first hosted Worker's routes: a pause, a seed over the tenant's settings and
        // sources, a run that spends the operator's model key, and a poll of every feed.
        "/hosted/tenants/alpha/register?status=paused",
        "/hosted/tenants/alpha/seed",
        "/hosted/tenants/alpha/run?period=morning",
        "/hosted/tenants/alpha/poll",
        "/hosted/tenants/alpha/invite",
        "/hosted/tenants/alpha/revoke",
        "/hosted/tenants/alpha/query",
        "/hosted/tenants/alpha/schedule?times=07:00,19:00&timezone=UTC",
        "/hosted/tenants/alpha/rotate-address",
        "/hosted/tenants/alpha/revoke-address",
        "/hosted/tenants/alpha/newsletter-source?name=Letter&from=letter@news.example",
      ]) {
        const response = await call(path, { method: "POST", operator: true, text: JSON.stringify(sql), headers });
        expect(response.status, `${path} ${JSON.stringify(headers)}`).toBe(403);
      }
    }

    // No SQL ran and no feed was fetched: the tenant's database was not even reached, and its
    // rows are all there.
    expect(ran).not.toHaveBeenCalled();
    expect(polled.mock.calls.map(([url]) => String(url)).filter((url) => !url.endsWith("/cdn-cgi/access/certs"))).toEqual([]);
    polled.mockRestore();
    expect(bound.TENANT_DB.gets).toEqual([]);
    expect(registryRows()).toBe(before);
    ran.mockRestore();
    expect((await alpha.query("SELECT count(*) AS n FROM digests", [])).rows[0].n).toBe(2);
    expect((await alpha.query("SELECT count(*) AS n FROM sources WHERE type = 'newsletter'", [])).rows[0].n).toBe(0);

    // As curl sends them, with no Origin and no Sec-Fetch-Site, and from this site, they are answered.
    const read = { sql: "SELECT count(*) AS n FROM digests", params: [] };
    expect((await call("/hosted/tenants/alpha/query", { method: "POST", operator: true, body: read })).status).toBe(200);
    const here = { Origin: ORIGIN, "Sec-Fetch-Site": "same-origin" };
    const schedule = "/hosted/tenants/alpha/schedule?times=07:00,19:00&timezone=UTC";
    expect((await call(schedule, { method: "POST", operator: true, headers: here })).status).toBe(200);
    expect((await call("/hosted/tenants/alpha/rotate-address", { method: "POST", operator: true })).status).toBe(200);
    expect((await call("/hosted/tenants/alpha/register?status=paused", { method: "POST", operator: true })).status).toBe(200);
    expect(rows("tenants").find((row) => row.id === "alpha").status).toBe("paused");
  });

  it("give the operator a tenant's latest page under /hosted/, sandboxed as /t/ serves it", async () => {
    const response = await call("/hosted/tenants/alpha/page", { operator: true });

    expect(response.status).toBe(200);
    expect(await response.text()).toBe(page("alpha", "2026-11-20-morning"));
    expect(response.headers.get("Content-Security-Policy")).toBe(SITE_POLICY);
  });
});

describe("a tenant's site, as served", () => {
  it("is sandboxed, unindexed, unsniffed and sends no referrer, on every answer", async () => {
    const { session } = await signIn("ana@example.com");

    for (const path of ["/t/alpha/", "/t/alpha/2026-11-20-morning-raw", "/t/alpha/missing", "/t/alpha", "/t/bravo/"]) {
      const response = await call(path, { session });
      // A page served to the session may post its votes to the vote route alone (hosted_vote.test.js).
      const pageServed = response.status === 200 && response.headers.get("Content-Type") === HTML;
      expect(response.headers.get("Content-Security-Policy"), path).toBe(pageServed ? votePolicy(ORIGIN) : SITE_POLICY);
      expect(response.headers.get("X-Robots-Tag"), path).toBe("noindex, nofollow");
      expect(response.headers.get("X-Content-Type-Options"), path).toBe("nosniff");
      expect(response.headers.get("Referrer-Policy"), path).toBe("no-referrer");
      // A page is private and revalidated; any other answer is never stored.
      expect(["private, no-cache", "no-store"], path).toContain(response.headers.get("Cache-Control"));
    }
    expect((await call("/t/alpha/", { session })).headers.get("Cache-Control")).toBe("private, no-cache");
    expect((await call("/t/bravo/", { session })).headers.get("Cache-Control")).toBe("no-store");
    expect((await call("/t/alpha/")).headers.get("Cache-Control")).toBe("no-store");
    expect(SITE_POLICY).toMatch(/^sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox; /);
    expect(SITE_POLICY).not.toContain("allow-same-origin");
    expect(SITE_POLICY).toContain("connect-src 'none'");
  });

  it("serves a stored page only as HTML or SVG, whatever type its row names", async () => {
    const { session } = await signIn("ana@example.com");
    const db = bound.TENANT_DB.getByName("alpha");
    await db.query(UPSERT, ["/evil.js", "alert(document.cookie)", "text/javascript", "2026-11-20T06:00:00+00:00"]);
    await db.query(UPSERT, ["/favicon.svg", "<svg/>", "image/svg+xml", "2026-11-20T06:00:00+00:00"]);

    expect((await call("/t/alpha/evil.js", { session })).status).toBe(404);
    expect((await call("/t/alpha/favicon.svg", { session })).headers.get("Content-Type")).toBe("image/svg+xml");
  });
});

describe("the sign-in mail", () => {
  it("carries the token in the link's fragment, on the hosted domain", () => {
    const mail = signInMail({ to: "ana@example.com", domain: DOMAIN, token: "T".repeat(43) });

    expect(mail.text.split("\n\n")).toEqual([
      "Open this link to sign in to your cyris digest:",
      `https://${DOMAIN}/login/confirm#${"T".repeat(43)}`,
      "The link works once, within 15 minutes. If you did not ask for it, you can ignore this mail.",
      `To sign in again later, ask for a new link at https://${DOMAIN}/login`,
    ]);
  });

  it("is not sent, and says why, when the Worker lacks the binding, the sender or the domain", async () => {
    const issue = vi.fn(async () => ({ tenant: "alpha" }));
    const mail = { send: vi.fn() };

    for (const [deps, missing] of [
      [{ mail: undefined, from: SENDER, domain: DOMAIN }, "MAIL binding"],
      [{ mail, from: "", domain: DOMAIN }, "CYRIS_HOSTED_MAIL_FROM"],
      [{ mail, from: SENDER, domain: undefined }, "CYRIS_STORE_HOSTED_DOMAIN"],
    ]) {
      expect((await createSignIn({ issue, ...deps })("ana@example.com")).error).toContain(missing);
    }
    expect(issue).not.toHaveBeenCalled();
    expect(mail.send).not.toHaveBeenCalled();
  });

  it("names only the binding's error code when a send fails, never the address or the token", async () => {
    const lines = [];
    vi.spyOn(console, "error").mockImplementation((...args) => lines.push(args.join(" ")));
    let sent;
    const mail = {
      send: async (message) => {
        sent = message;
        throw Object.assign(new Error(`refused ${message.to}`), { code: "E_RECIPIENT_NOT_ALLOWED" });
      },
    };

    const answer = await createSignIn({ issue: async () => ({ tenant: "alpha" }), mail, from: SENDER, domain: DOMAIN })("ana@example.com");

    expect(answer).toEqual({ error: "the sign-in mail was not sent: E_RECIPIENT_NOT_ALLOWED" });
    expect(lines).toEqual(["sign-in mail not sent: E_RECIPIENT_NOT_ALLOWED"]);
    expect(lines.join("")).not.toContain(linkToken(sent));
  });
});
