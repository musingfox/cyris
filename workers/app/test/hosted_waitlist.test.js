import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";
import { env } from "cloudflare:workers";
import { generateKeyPair, exportJWK, SignJWT, createLocalJWKSet } from "jose";
import worker from "../src/hosted.js";
import { PRIVATE_HEADERS } from "../src/hosted_pages.js";
import { handleHostedRequest } from "../src/hosted_router.js";
import {
  MAX_WAITLIST_BODY_BYTES,
  MAX_WAITLIST_NOTE_CHARS,
  TURNSTILE_SITEVERIFY_URL,
  WAITLIST_DAILY_CAP,
  WAITLIST_IP_HOURLY_CAP,
  WAITLIST_MAX_ENTRIES,
  WAITLIST_RATE_DAY_MS,
  WAITLIST_RATE_HOUR_MS,
  WAITLIST_TURNSTILE_ACTION,
  handleWaitlist,
  normalizeNote,
  parseAllowedOrigins,
} from "../src/hosted_waitlist.js";
import { namespace, registryNamespace, registryObject } from "./sql_storage.js";

const AT = new Date("2026-11-25T12:00:00Z");
const later = (ms) => new Date(AT.getTime() + ms);
const ORIGIN = "https://cyris.musingfox.com";
const WORKER_ORIGIN = "https://readcyris.com";
const SECRET = "turnstile-test-secret";
const BASE_ENV = {
  CYRIS_WAITLIST_ORIGINS: ORIGIN,
  CYRIS_WAITLIST_TURNSTILE_SECRET: SECRET,
};

const TEAM = "team.example";
const AUD = "AUD-HOSTED";
const { privateKey, publicKey } = await generateKeyPair("RS256");
const JWK = { ...(await exportJWK(publicKey)), kid: "k1", alg: "RS256", use: "sig" };
const ACCESS_KEYS = { keySet: createLocalJWKSet({ keys: [JWK] }), issuer: `https://${TEAM}` };
const OPERATOR = "operator-token.access";
const OP_ENV = { CYRIS_ACCESS_TEAM_DOMAIN: TEAM, CYRIS_ACCESS_AUD: AUD, CYRIS_HOSTED_OPERATORS: OPERATOR };

const accessJwt = (claims = { common_name: OPERATOR }) =>
  new SignJWT(claims)
    .setProtectedHeader({ alg: "RS256", kid: "k1" })
    .setIssuer(`https://${TEAM}`)
    .setAudience(AUD)
    .setIssuedAt()
    .setExpirationTime(Math.floor(Date.now() / 1000) + 3600)
    .sign(privateKey);

describe("TenantRegistry waitlist storage", () => {
  it("stores normalized email, optional note, language, and updates repeats without losing created_at", () => {
    const registry = registryObject();

    const first = registry.joinWaitlist(
      { email: "  Alice@Example.COM ", note: "  HN & blogs\r\n ", language: "zh-Hant" },
      { now: AT },
    );
    expect(first.created).toBe(true);
    expect(first.entry).toEqual({
      email: "alice@example.com",
      note: "HN & blogs",
      language: "zh-Hant",
      created_at: "2026-11-25T12:00:00+00:00",
      updated_at: "2026-11-25T12:00:00+00:00",
    });

    // Empty repeat preserves the earlier note while updating language and updated_at.
    const second = registry.joinWaitlist(
      { email: "alice@example.com", note: "", language: "en" },
      { now: later(60_000) },
    );
    expect(second.created).toBe(false);
    expect(second.entry).toEqual({
      email: "alice@example.com",
      note: "HN & blogs",
      language: "en",
      created_at: "2026-11-25T12:00:00+00:00",
      updated_at: "2026-11-25T12:01:00+00:00",
    });

    // Non-empty note replaces the earlier note.
    const third = registry.joinWaitlist(
      { email: "alice@example.com", note: "Updated note", language: "zh-Hant" },
      { now: later(120_000) },
    );
    expect(third.entry.note).toBe("Updated note");

    // Second user arrives later; entries are ordered FIFO by created_at.
    registry.joinWaitlist({ email: "bob@example.com", note: "", language: "unknown" }, { now: later(180_000) });
    expect(registry.waitlistEntries()).toEqual({
      count: 2,
      entries: [
        {
          email: "alice@example.com",
          note: "Updated note",
          language: "zh-Hant",
          created_at: "2026-11-25T12:00:00+00:00",
          updated_at: "2026-11-25T12:02:00+00:00",
        },
        {
          email: "bob@example.com",
          note: "",
          language: "en",
          created_at: "2026-11-25T12:03:00+00:00",
          updated_at: "2026-11-25T12:03:00+00:00",
        },
      ],
    });
  });

  it("enforces WAITLIST_MAX_ENTRIES for new emails while allowing existing ones to update", () => {
    const registry = registryObject();
    registry.joinWaitlist({ email: "one@example.com" }, { now: AT, maxEntries: 2 });
    registry.joinWaitlist({ email: "two@example.com" }, { now: later(1000), maxEntries: 2 });

    expect(registry.joinWaitlist({ email: "three@example.com" }, { now: later(2000), maxEntries: 2 })).toEqual({
      refused: "waitlist is full",
      code: "waitlist_full",
      status: 503,
    });
    expect(registry.joinWaitlist({ email: "one@example.com", note: "ok" }, { now: later(3000), maxEntries: 2 }).entry.note).toBe("ok");
  });

  it("removes entries and refuses missing or malformed addresses", () => {
    const registry = registryObject();
    registry.joinWaitlist({ email: "alice@example.com" }, { now: AT });

    expect(registry.removeWaitlist("not-an-email")).toMatchObject({ status: 400 });
    expect(registry.removeWaitlist("bob@example.com")).toMatchObject({ status: 404 });
    expect(registry.removeWaitlist("ALICE@example.com")).toEqual({ removed: { email: "alice@example.com" } });
    expect(registry.waitlistEntries()).toEqual({ count: 0, entries: [] });
  });

  it("enforces per-IP hourly and global daily caps and prunes expired attempts", () => {
    const registry = registryObject();
    for (let i = 0; i < WAITLIST_IP_HOURLY_CAP; i += 1) {
      expect(registry.recordWaitlistAttempt("ip-1", { now: later(i * 1000) })).toEqual({ ok: true });
    }
    expect(registry.recordWaitlistAttempt("ip-1", { now: later(10_000) })).toMatchObject({ capped: "ip" });
    // Another IP succeeds in the same hour, and ip-1 recovers once the hour passes.
    expect(registry.recordWaitlistAttempt("ip-2", { now: later(10_000) })).toEqual({ ok: true });
    expect(registry.recordWaitlistAttempt("ip-1", { now: later(WAITLIST_RATE_HOUR_MS + 60_000) })).toEqual({ ok: true });

    // Global daily cap blocks any IP until 24h expires and old rows are pruned.
    const smallCapRegistry = registryObject();
    expect(smallCapRegistry.recordWaitlistAttempt("ip-a", { now: AT, dailyCap: 2 })).toEqual({ ok: true });
    expect(smallCapRegistry.recordWaitlistAttempt("ip-b", { now: later(1000), dailyCap: 2 })).toEqual({ ok: true });
    expect(smallCapRegistry.recordWaitlistAttempt("ip-c", { now: later(2000), dailyCap: 2 })).toMatchObject({ capped: "daily" });
    expect(smallCapRegistry.recordWaitlistAttempt("ip-c", { now: later(WAITLIST_RATE_DAY_MS + 3000), dailyCap: 2 })).toEqual({ ok: true });
  });

  it("includes waitlist in exportDump", async () => {
    const registry = registryObject();
    registry.joinWaitlist({ email: "alice@example.com", note: "hello" }, { now: AT });
    const dump = await registry.exportDump();
    const text = await new Response(new Blob([dump]).stream().pipeThrough(new DecompressionStream("gzip"))).text();

    expect(text).toContain("CREATE TABLE waitlist");
    expect(text).toContain("alice@example.com");
  });
});

describe("handleWaitlist validation, CORS, rate limit, and Turnstile", () => {
  it("parses allowed origins and normalizes notes cleanly", () => {
    const parsed = parseAllowedOrigins(" https://cyris.musingfox.com , invalid , ftp://bad.example , https://a.example:8443/path ");
    expect([...parsed.origins]).toEqual(["https://cyris.musingfox.com"]);
    expect([...parsed.hostnames]).toEqual(["cyris.musingfox.com"]);

    expect(normalizeNote("  hi\u0007\nthere \r\n")).toEqual({ note: "hi\nthere" });
    expect(normalizeNote("x".repeat(MAX_WAITLIST_NOTE_CHARS + 1))).toMatchObject({ code: "note_too_long" });
  });

  const makePost = (body, { origin = ORIGIN, ip = "203.0.113.10" } = {}) =>
    new Request(`${WORKER_ORIGIN}/waitlist`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(origin !== null ? { Origin: origin } : {}),
        ...(ip !== null ? { "CF-Connecting-IP": ip } : {}),
      },
      body: typeof body === "string" ? body : JSON.stringify(body),
    });

  it("fails closed 503 when Turnstile secret or origins are missing", async () => {
    const response = await handleWaitlist(makePost({ email: "a@example.com", "cf-turnstile-response": "tok" }), {}, {
      recordAttempt: async () => ({ ok: true }),
      join: async () => ({ created: true }),
    });
    expect(response.status).toBe(503);
    expect(await response.json()).toMatchObject({ code: "waitlist_not_configured" });
  });

  it("handles CORS preflight OPTIONS and refuses disallowed origins", async () => {
    const ok = await handleWaitlist(
      new Request(`${WORKER_ORIGIN}/waitlist`, { method: "OPTIONS", headers: { Origin: ORIGIN } }),
      BASE_ENV,
      {},
    );
    expect(ok.status).toBe(204);
    expect(ok.headers.get("Access-Control-Allow-Origin")).toBe(ORIGIN);
    expect(ok.headers.get("Access-Control-Allow-Methods")).toBe("POST, OPTIONS");

    const bad = await handleWaitlist(
      new Request(`${WORKER_ORIGIN}/waitlist`, { method: "OPTIONS", headers: { Origin: "https://evil.example" } }),
      BASE_ENV,
      {},
    );
    expect(bad.status).toBe(403);
    expect(bad.headers.get("Access-Control-Allow-Origin")).toBeNull();
  });

  it("refuses non-POST methods, missing/bad origins, oversized bodies, and invalid fields", async () => {
    const deps = { recordAttempt: async () => ({ ok: true }), join: async () => ({ created: true }) };
    expect((await handleWaitlist(new Request(`${WORKER_ORIGIN}/waitlist`, { method: "GET" }), BASE_ENV, deps)).status).toBe(405);
    expect((await handleWaitlist(makePost({ email: "a@example.com", "cf-turnstile-response": "t" }, { origin: null }), BASE_ENV, deps)).status).toBe(403);
    expect((await handleWaitlist(makePost({ email: "a@example.com", "cf-turnstile-response": "t" }, { origin: "https://evil.example" }), BASE_ENV, deps)).status).toBe(403);
    expect((await handleWaitlist(makePost("x".repeat(MAX_WAITLIST_BODY_BYTES + 1)), BASE_ENV, deps)).status).toBe(413);
    expect((await handleWaitlist(makePost("{bad"), BASE_ENV, deps)).status).toBe(400);
    expect((await handleWaitlist(makePost({ email: "bad", "cf-turnstile-response": "tok" }), BASE_ENV, deps)).status).toBe(400);
    expect((await handleWaitlist(makePost({ email: "a@example.com", note: "x".repeat(501), "cf-turnstile-response": "tok" }), BASE_ENV, deps)).status).toBe(400);
    expect((await handleWaitlist(makePost({ email: "a@example.com" }), BASE_ENV, deps)).status).toBe(400);
  });

  it("checks rate limit before calling Turnstile Siteverify", async () => {
    const fetchTurnstile = vi.fn();
    const response = await handleWaitlist(
      makePost({ email: "a@example.com", "cf-turnstile-response": "tok" }),
      BASE_ENV,
      {
        recordAttempt: async () => ({ refused: "too many", capped: "ip" }),
        join: async () => {
          throw new Error("should not reach join");
        },
        fetchTurnstile,
      },
    );
    expect(response.status).toBe(429);
    expect(await response.json()).toMatchObject({ code: "rate_limited" });
    expect(fetchTurnstile).not.toHaveBeenCalled();
  });

  it("verifies Turnstile action and hostname and rejects mismatches or siteverify errors", async () => {
    const join = vi.fn(async () => ({ created: true }));
    const callWithVerify = (verifyResponse) =>
      handleWaitlist(
        makePost({ email: "Reader@Example.com", note: "  interested ", language: "zh-Hant", "cf-turnstile-response": "tok-123" }),
        BASE_ENV,
        {
          recordAttempt: async () => ({ ok: true }),
          join,
          fetchTurnstile: verifyResponse,
        },
      );

    // Wrong hostname
    expect(
      (await callWithVerify(async () => Response.json({ success: true, action: WAITLIST_TURNSTILE_ACTION, hostname: "evil.example" }))).status,
    ).toBe(403);
    // Wrong action
    expect(
      (await callWithVerify(async () => Response.json({ success: true, action: "login", hostname: "cyris.musingfox.com" }))).status,
    ).toBe(403);
    // Network error fails closed
    expect(
      (await callWithVerify(async () => {
        throw new Error("network down");
      })).status,
    ).toBe(403);
    expect(join).not.toHaveBeenCalled();

    // Valid verification stores entry and returns 200 { ok: true }
    let captured;
    const ok = await callWithVerify(async (url, init) => {
      captured = { url, params: Object.fromEntries(new URLSearchParams(init.body)) };
      return Response.json({ success: true, action: WAITLIST_TURNSTILE_ACTION, hostname: "cyris.musingfox.com" });
    });
    expect(ok.status).toBe(200);
    expect(ok.headers.get("Access-Control-Allow-Origin")).toBe(ORIGIN);
    expect(ok.headers.get("X-Robots-Tag")).toBe(PRIVATE_HEADERS["X-Robots-Tag"]);
    expect(await ok.json()).toEqual({ ok: true });
    expect(captured.url).toBe(TURNSTILE_SITEVERIFY_URL);
    expect(captured.params).toEqual({
      secret: SECRET,
      response: "tok-123",
      remoteip: "203.0.113.10",
    });
    expect(join).toHaveBeenCalledWith({
      email: "reader@example.com",
      note: "interested",
      language: "zh-Hant",
    });
  });

  it("has matching defaults for caps", () => {
    expect(WAITLIST_MAX_ENTRIES).toBe(5000);
    expect(WAITLIST_IP_HOURLY_CAP).toBe(5);
    expect(WAITLIST_DAILY_CAP).toBe(500);
  });
});

describe("hosted router + Worker end-to-end waitlist routes", () => {
  beforeEach(() => {
    Object.assign(env, {
      REGISTRY: registryNamespace(),
      TENANT_DB: namespace(),
      ...BASE_ENV,
      ...OP_ENV,
    });
  });

  afterEach(() => {
    for (const key of Object.keys(env)) delete env[key];
    vi.restoreAllMocks();
  });

  it("routes POST /waitlist into TenantRegistry without Access or session", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      Response.json({ success: true, action: WAITLIST_TURNSTILE_ACTION, hostname: "cyris.musingfox.com" }),
    );
    const response = await worker.fetch(
      new Request(`${WORKER_ORIGIN}/waitlist`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Origin: ORIGIN,
          "CF-Connecting-IP": "198.51.100.4",
        },
        body: JSON.stringify({
          email: "Waiter@Example.com",
          note: "tech newsletters",
          language: "zh-Hant",
          "cf-turnstile-response": "valid-token",
        }),
      }),
      env,
      { waitUntil: () => {} },
    );

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ ok: true });
  });

  it("requires Access JWT + named operator for /hosted/waitlist and /hosted/waitlist/remove", async () => {
    const registry = registryObject();
    registry.joinWaitlist({ email: "alice@example.com", note: "rss", language: "en" }, { now: AT });
    const deps = {
      accessKeys: ACCESS_KEYS,
      waitlistEntries: () => registry.waitlistEntries(),
      removeWaitlist: (email) => registry.removeWaitlist(email),
    };

    // Unauthenticated requests are refused 403
    expect(
      (await handleHostedRequest(new Request(`${WORKER_ORIGIN}/hosted/waitlist`), OP_ENV, deps)).status,
    ).toBe(403);
    expect(
      (
        await handleHostedRequest(
          new Request(`${WORKER_ORIGIN}/hosted/waitlist/remove`, {
            method: "POST",
            body: JSON.stringify({ email: "alice@example.com" }),
          }),
          OP_ENV,
          deps,
        )
      ).status,
    ).toBe(403);

    const jwt = await accessJwt();
    const list = await handleHostedRequest(
      new Request(`${WORKER_ORIGIN}/hosted/waitlist`, { headers: { "Cf-Access-Jwt-Assertion": jwt } }),
      OP_ENV,
      deps,
    );
    expect(list.status).toBe(200);
    expect(await list.json()).toMatchObject({
      count: 1,
      entries: [{ email: "alice@example.com", note: "rss", language: "en" }],
    });

    // Cross-site POST is refused 403 even with Access JWT
    const cross = await handleHostedRequest(
      new Request(`${WORKER_ORIGIN}/hosted/waitlist/remove`, {
        method: "POST",
        headers: { "Cf-Access-Jwt-Assertion": jwt, Origin: "https://evil.example" },
        body: JSON.stringify({ email: "alice@example.com" }),
      }),
      OP_ENV,
      deps,
    );
    expect(cross.status).toBe(403);

    const removed = await handleHostedRequest(
      new Request(`${WORKER_ORIGIN}/hosted/waitlist/remove`, {
        method: "POST",
        headers: { "Cf-Access-Jwt-Assertion": jwt, "Content-Type": "application/json" },
        body: JSON.stringify({ email: "alice@example.com" }),
      }),
      OP_ENV,
      deps,
    );
    expect(removed.status).toBe(200);
    expect(await removed.json()).toEqual({ removed: { email: "alice@example.com" } });

    const missing = await handleHostedRequest(
      new Request(`${WORKER_ORIGIN}/hosted/waitlist/remove`, {
        method: "POST",
        headers: { "Cf-Access-Jwt-Assertion": jwt, "Content-Type": "application/json" },
        body: JSON.stringify({ email: "alice@example.com" }),
      }),
      OP_ENV,
      deps,
    );
    expect(missing.status).toBe(404);
  });
});
