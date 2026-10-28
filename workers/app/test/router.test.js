import { describe, it, expect, vi } from "vitest";
import {
  generateKeyPair,
  exportJWK,
  SignJWT,
  createLocalJWKSet,
  createRemoteJWKSet,
  customFetch,
} from "jose";
import { handleRequest, returnPath } from "../src/router.js";
import { accessKeys } from "../src/access_keys.js";
import routes from "./routes.json" with { type: "json" };

const HOST = "cyris-app.x.workers.dev";
const ACCESS_HOST = "digest.example.test";
const VOTE_BODY = { url: "https://e.x/a", vote: "up", digest_date: "2026-09-01" };

function makeDeps({
  fetchStatus = 200,
  fetchBody = "<html>",
  fetchThrow = false,
  fetchHeaders = {},
  scale = 1,
  containerResponse = () => new Response("container", { status: 200 }),
  timeZone = "Asia/Taipei",
  now = new Date("2026-10-20T02:30:00Z"),
  accessKeys = ACCESS_KEYS,
} = {}) {
  const wrappedContainer = async (req) => {
    wrappedContainer.calls.push(req);
    return containerResponse();
  };
  wrappedContainer.calls = [];

  // A run's period alone, or [period, date] when it names a date.
  const startRun = async (period, date) => {
    startRun.calls.push(date === undefined ? period : [period, date]);
    return { started: "run", at: "t" };
  };
  startRun.calls = [];

  const probeGemini = async (model) => {
    probeGemini.calls.push(model);
    return {
      status: 200,
      body: { ok: true, provider: "gemini", model, latency_ms: 1 },
    };
  };
  probeGemini.calls = [];

  const fetchImpl = async (input, init) => {
    fetchImpl.calls.push({ input, init });
    if (fetchThrow) throw new Error("upstream");
    const body = typeof fetchBody === "string" ? fetchBody : JSON.stringify(fetchBody);
    return new Response(body, {
      status: fetchStatus,
      headers: { "Content-Type": "application/json", ...fetchHeaders },
    });
  };
  fetchImpl.calls = [];

  const typeScale = {
    reads: 0,
    forgets: 0,
    async current() {
      typeScale.reads += 1;
      return scale;
    },
    forget() {
      typeScale.forgets += 1;
    },
  };

  const theme = {
    reads: 0,
    async current() {
      theme.reads += 1;
      return "default";
    },
    forget() {
      theme.forgets += 1;
    },
  };
  theme.forgets = 0;

  // What HTMLRewriter does in workerd, which Node lacks: `html` goes last in <head>.
  const injectStyle = (response, html) => {
    injectStyle.calls.push(html);
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
  injectStyle.calls = [];

  const runTimeZone = async () => {
    runTimeZone.calls += 1;
    return timeZone;
  };
  runTimeZone.calls = 0;

  return {
    accessKeys,
    container: wrappedContainer,
    startRun,
    runTimeZone,
    now: () => now,
    probeGemini,
    fetchImpl,
    typeScale,
    theme,
    injectStyle,
  };
}

function env(extra = {}) {
  return {
    CYRIS_ACCESS_TEAM_DOMAIN: TEAM,
    CYRIS_ACCESS_AUD: AUD,
    DIGEST_ORIGIN: "https://p.pages.dev",
    CYRIS_PROMOTE_WORKER_URL: "https://promote.example",
    CYRIS_PROMOTE_TOKEN: "promote-tok",
    ...extra,
  };
}

function request(method, path, { host = HOST, headers = {}, body, jwt } = {}) {
  const h = new Headers(headers);
  if (jwt) h.set("Cf-Access-Jwt-Assertion", jwt);
  const init = { method, headers: h };
  if (body instanceof FormData) {
    init.body = body;
  } else if (body !== undefined) {
    init.body = typeof body === "string" ? body : JSON.stringify(body);
    if (!h.has("Content-Type")) h.set("Content-Type", "application/json");
  }
  return new Request(`https://${host}${path}`, init);
}

const TEAM = "team.example";
const AUD = "AUD1";
const { privateKey, publicKey } = await generateKeyPair("RS256");
const JWK = { ...(await exportJWK(publicKey)), kid: "k1", alg: "RS256", use: "sig" };
const ACCESS_KEYS = { keySet: createLocalJWKSet({ keys: [JWK] }), issuer: `https://${TEAM}` };

async function accessJwt({
  key = privateKey,
  iss = `https://${TEAM}`,
  aud = AUD,
  exp = Math.floor(Date.now() / 1000) + 3600,
  claims = {},
} = {}) {
  return new SignJWT({ ...claims })
    .setProtectedHeader({ alg: "RS256", kid: "k1" })
    .setIssuer(iss)
    .setAudience(aud)
    .setIssuedAt()
    .setExpirationTime(exp)
    .sign(key);
}

describe("UnauthenticatedWriteSurfaceRejected", () => {
  it("POST /api/settings with no JWT does not invoke the container", async () => {
    const deps = makeDeps();
    const resp = await handleRequest(request("POST", "/api/settings"), env(), deps);
    expect(resp.status).toBe(403);
    expect(deps.container.calls).toHaveLength(0);
  });

  it("POST /api/articles/accept with no JWT does not invoke the container", async () => {
    const deps = makeDeps();
    const resp = await handleRequest(request("POST", "/api/articles/accept"), env(), deps);
    expect(resp.status).toBe(403);
    expect(deps.container.calls).toHaveLength(0);
  });

  it("DELETE /api/sources/demo with no JWT does not invoke the container", async () => {
    const deps = makeDeps();
    const resp = await handleRequest(request("DELETE", "/api/sources/demo"), env(), deps);
    expect(resp.status).toBe(403);
    expect(deps.container.calls).toHaveLength(0);
  });

  it("POST /api/settings/schedule with no JWT does not invoke the container", async () => {
    const deps = makeDeps();
    const resp = await handleRequest(request("POST", "/api/settings/schedule"), env(), deps);
    expect(resp.status).toBe(403);
    expect(deps.container.calls).toHaveLength(0);
  });

  it("POST /api/sources with no JWT does not invoke the container", async () => {
    const deps = makeDeps();
    const resp = await handleRequest(request("POST", "/api/sources"), env(), deps);
    expect(resp.status).toBe(403);
    expect(deps.container.calls).toHaveLength(0);
  });

  it("POST /api/articles/reject with no JWT is 403", async () => {
    const resp = await handleRequest(request("POST", "/api/articles/reject"), env(), makeDeps());
    expect(resp.status).toBe(403);
  });

  it("POST /api/articles/undo with no JWT is 403", async () => {
    const resp = await handleRequest(request("POST", "/api/articles/undo"), env(), makeDeps());
    expect(resp.status).toBe(403);
  });

  it("POST /run with no JWT does not start a run", async () => {
    const deps = makeDeps();
    const resp = await handleRequest(request("POST", "/run"), env(), deps);
    expect(resp.status).toBe(403);
    expect(deps.startRun.calls).toHaveLength(0);
  });

  it("GET /settings with Accept text/html and no JWT is JSON 403", async () => {
    const resp = await handleRequest(
      request("GET", "/settings", { headers: { Accept: "text/html" } }),
      env(),
      makeDeps(),
    );
    expect(resp.status).toBe(403);
    expect(resp.headers.get("Content-Type")).toBe("application/json");
  });

  it("GET /labels with a valid JWT reaches the container, not Pages", async () => {
    const deps = makeDeps();
    const resp = await handleRequest(
      request("GET", "/labels", { jwt: await accessJwt() }),
      env(),
      deps,
    );
    expect(resp.status).toBe(200);
    expect(deps.container.calls).toHaveLength(1);
    expect(deps.fetchImpl.calls).toHaveLength(0);
  });

  it("GET /onboarding with no JWT does not reach the container", async () => {
    const deps = makeDeps();
    const resp = await handleRequest(request("GET", "/onboarding"), env(), deps);
    expect(resp.status).toBe(403);
    expect(deps.container.calls).toHaveLength(0);
    expect(deps.fetchImpl.calls).toHaveLength(0);
  });

  it("GET /onboarding with Accept text/html and no JWT is JSON 403", async () => {
    const resp = await handleRequest(
      request("GET", "/onboarding", { headers: { Accept: "text/html" } }),
      env(),
      makeDeps(),
    );
    expect(resp.status).toBe(403);
    expect(resp.headers.get("Content-Type")).toBe("application/json");
  });

  it("GET /onboarding with a valid JWT reaches the container, not Pages", async () => {
    const deps = makeDeps();
    const resp = await handleRequest(
      request("GET", "/onboarding", { jwt: await accessJwt() }),
      env(),
      deps,
    );
    expect(resp.status).toBe(200);
    expect(deps.container.calls).toHaveLength(1);
    expect(deps.fetchImpl.calls).toHaveLength(0);
  });

  it("GET /static/settings.js with no JWT is 403", async () => {
    const resp = await handleRequest(request("GET", "/static/settings.js"), env(), makeDeps());
    expect(resp.status).toBe(403);
  });

  it("GET /api/zzz-undefined with no JWT is 403", async () => {
    const resp = await handleRequest(request("GET", "/api/zzz-undefined"), env(), makeDeps());
    expect(resp.status).toBe(403);
  });

  it("GET /settings with a junk cookie is 403", async () => {
    const resp = await handleRequest(
      request("GET", "/settings", { jwt: "deadbeef" }),
      env(),
      makeDeps(),
    );
    expect(resp.status).toBe(403);
  });

  it("GET /api/settings with a valid JWT reaches the container", async () => {
    const deps = makeDeps();
    const jwt = await accessJwt();
    await handleRequest(request("GET", "/api/settings", { jwt }), env(), deps);
    expect(deps.container.calls).toHaveLength(1);
  });

  it("every routes.json row answers the status the table names without a JWT", async () => {
    for (const row of routes) {
      const deps = makeDeps();
      const resp = await handleRequest(request(row.method, row.path), env(), deps);
      expect(resp.status, `${row.method} ${row.path}`).toBe(row.unauth_status);
      expect(deps.container.calls, row.path).toHaveLength(0);
      expect(deps.startRun.calls, row.path).toHaveLength(0);
      expect(deps.probeGemini.calls, row.path).toHaveLength(0);
      if (row.kind !== "public") expect(deps.fetchImpl.calls, row.path).toHaveLength(0);
    }
  });
});

describe("ManualRunPeriod", () => {
  it("POST /run?period=evening starts a run for that period", async () => {
    const deps = makeDeps();
    const resp = await handleRequest(
      request("POST", "/run?period=evening", { jwt: await accessJwt() }),
      env(),
      deps,
    );

    expect(resp.status).toBe(200);
    expect(deps.startRun.calls).toEqual(["evening"]);
  });

  it("POST /run with no period leaves the schedule to decide", async () => {
    const deps = makeDeps();
    const resp = await handleRequest(
      request("POST", "/run", { jwt: await accessJwt() }),
      env(),
      deps,
    );

    expect(resp.status).toBe(200);
    expect(deps.startRun.calls).toEqual([undefined]);
  });

  // 10:30 on 2026-10-20 in Taipei; 02:30 UTC, the same day there.
  const run = async (query, options) => {
    const deps = makeDeps(options);
    const resp = await handleRequest(
      request("POST", `/run?${query}`, { jwt: await accessJwt() }),
      env(),
      deps,
    );
    return { resp, deps };
  };

  it("passes a named date beside its period", async () => {
    const { resp, deps } = await run("period=evening&date=2026-10-19");

    expect(resp.status).toBe(200);
    expect(deps.startRun.calls).toEqual([["evening", "2026-10-19"]]);
  });

  it("takes today in the deployment's time zone, not UTC's", async () => {
    // 23:30 UTC on the 19th is already the 20th in Taipei.
    const late = new Date("2026-10-19T23:30:00Z");
    const today = await run("period=morning&date=2026-10-20", { now: late });
    const before = await run("period=morning&date=2026-10-18", { now: late });

    expect(today.deps.startRun.calls).toEqual([["morning", "2026-10-20"]]);
    expect(before.resp.status).toBe(400);
  });

  it.each(["2026-10-21", "2026-10-18", "2025-10-20"])(
    "refuses %s, neither today nor yesterday",
    async (date) => {
      const { resp, deps } = await run(`period=evening&date=${date}`);

      expect(resp.status).toBe(400);
      expect((await resp.json()).error).toContain("today or yesterday in Asia/Taipei");
      expect(deps.startRun.calls).toHaveLength(0);
    },
  );

  it.each(["2026-10-32", "2026-1-9", "20261019", "2026-10-19T00:00", ""])(
    "refuses %j, not a real YYYY-MM-DD",
    async (date) => {
      const { resp, deps } = await run(`period=evening&date=${date}`);

      expect(resp.status).toBe(400);
      expect((await resp.json()).error).toContain("real YYYY-MM-DD");
      expect(deps.startRun.calls).toHaveLength(0);
    },
  );

  it("refuses a date it cannot check when the time zone cannot be read", async () => {
    const old = await run("period=evening&date=2020-01-01", { timeZone: null });
    const bad = await run("period=evening&date=2020-13-01", { timeZone: null });

    expect(old.resp.status).toBe(503);
    expect((await old.resp.json()).error).toContain("time zone");
    expect(old.deps.startRun.calls).toHaveLength(0);
    expect(bad.resp.status).toBe(400);
  });

  it("refuses a date with no period: the tick dates its own issue", async () => {
    const { resp, deps } = await run("date=2026-10-20");

    expect(resp.status).toBe(400);
    expect(deps.startRun.calls).toHaveLength(0);
  });

  it("reads no time zone for a run that names no date", async () => {
    const { deps } = await run("period=morning");

    expect(deps.runTimeZone.calls).toBe(0);
    expect(deps.startRun.calls).toEqual(["morning"]);
  });

  it("rejects an unknown period before starting a run", async () => {
    const deps = makeDeps();
    const resp = await handleRequest(
      request("POST", "/run?period=noon", { jwt: await accessJwt() }),
      env(),
      deps,
    );

    expect(resp.status).toBe(400);
    expect(await resp.json()).toHaveProperty("error");
    expect(deps.startRun.calls).toHaveLength(0);
  });
});

describe("GeminiProbeIsWorkerLocal", () => {
  it("POST /api/diagnostics/gemini invokes the Worker probe after JWT authentication", async () => {
    const deps = makeDeps();
    const resp = await handleRequest(
      request("POST", "/api/diagnostics/gemini?model=gemini-3.7-flash", {
        jwt: await accessJwt(),
      }),
      env(),
      deps,
    );

    expect(resp.status).toBe(200);
    expect(await resp.json()).toMatchObject({
      ok: true,
      provider: "gemini",
      model: "gemini-3.7-flash",
    });
    expect(deps.probeGemini.calls).toEqual(["gemini-3.7-flash"]);
    expect(deps.container.calls).toHaveLength(0);
  });

  it("rejects an invalid model before probing Gemini", async () => {
    const deps = makeDeps();
    const resp = await handleRequest(
      request("POST", "/api/diagnostics/gemini?model=not/a-model", {
        jwt: await accessJwt(),
      }),
      env(),
      deps,
    );

    expect(resp.status).toBe(400);
    expect(deps.probeGemini.calls).toHaveLength(0);
  });
});

describe("VotePostRequiresJwtOffAccessHost", () => {
  it("POST /api/vote with no JWT does not call promote", async () => {
    const deps = makeDeps();
    const resp = await handleRequest(
      request("POST", "/api/vote", { body: VOTE_BODY }),
      env(),
      deps,
    );
    expect(resp.status).toBe(403);
    expect((await resp.json()).error).toBe("forbidden");
    expect(deps.fetchImpl.calls).toHaveLength(0);
  });

  it("POST /api/vote with a valid JWT forwards the promote bearer", async () => {
    const deps = makeDeps({ fetchStatus: 201, fetchBody: { ok: true } });
    const jwt = await accessJwt();
    const resp = await handleRequest(
      request("POST", "/api/vote", { body: { ...VOTE_BODY, vote: "up" }, jwt }),
      env(),
      deps,
    );
    expect(resp.status).toBe(201);
    expect(await resp.json()).toEqual({ ok: true });
    const call = deps.fetchImpl.calls[0];
    expect(call.init.headers.Authorization).toBe("Bearer promote-tok");
  });

  it("POST /api/vote with a valid JWT but no promote URL is 503", async () => {
    const jwt = await accessJwt();
    const resp = await handleRequest(
      request("POST", "/api/vote", { body: VOTE_BODY, jwt }),
      env({ CYRIS_PROMOTE_WORKER_URL: "" }),
      makeDeps(),
    );
    expect(resp.status).toBe(503);
  });

  it("PUT /api/vote with no JWT on a non-Access host is 403", async () => {
    const resp = await handleRequest(request("PUT", "/api/vote"), env(), makeDeps());
    expect(resp.status).toBe(403);
  });
});

describe("VoteProbeReflectsJwtOffAccessHost", () => {
  it("GET /api/vote with no JWT is JSON 403", async () => {
    const resp = await handleRequest(request("GET", "/api/vote"), env(), makeDeps());
    expect(resp.status).toBe(403);
    expect(resp.headers.get("Content-Type")).toBe("application/json");
    expect((await resp.json()).error).toBe("forbidden");
  });

  it("GET /api/vote with a valid JWT is authorized", async () => {
    const jwt = await accessJwt();
    const resp = await handleRequest(request("GET", "/api/vote", { jwt }), env(), makeDeps());
    expect(resp.status).toBe(200);
    expect(await resp.json()).toEqual({ authorized: true });
  });

  it("HEAD /api/vote with no JWT is 403", async () => {
    const resp = await handleRequest(request("HEAD", "/api/vote"), env(), makeDeps());
    expect(resp.status).toBe(403);
  });

  it("GET /api/vote with Accept text/html still returns JSON", async () => {
    const resp = await handleRequest(
      request("GET", "/api/vote", { headers: { Accept: "text/html" } }),
      env(),
      makeDeps(),
    );
    expect(resp.status).toBe(403);
    expect(resp.headers.get("Content-Type")).toBe("application/json");
  });
});

describe("LoginReturnsToThePage", () => {
  const origin = `https://${HOST}`;

  it("returns to a page on this origin, query included", () => {
    for (const path of ["/settings", "/2026-10-01-morning-raw.html", "/settings?x=1"]) {
      expect(returnPath(path, origin), path).toBe(path);
    }
  });

  it("returns to the archive with no next", () => {
    expect(returnPath(undefined, origin)).toBe("/");
  });

  it("never sends the reader off this origin", () => {
    const away = [
      "https://evil.example/",
      "//evil.example/",
      "//evil.example/settings",
      "/\\evil.example/",
      "/\t/evil.example/",
      "/.//evil.example/",
      "/..//evil.example/",
      "javascript:alert(1)",
      "evil.example",
      "",
      "/login",
      "/login?next=https://evil.example/",
    ];
    for (const next of away) {
      expect(returnPath(next, origin), JSON.stringify(next)).toBe("/");
    }
  });
});

describe("DigestOriginRequired", () => {
  it("GET / without DIGEST_ORIGIN is 503 and does not fetch", async () => {
    const deps = makeDeps();
    const { DIGEST_ORIGIN: _, ...rest } = env();
    const resp = await handleRequest(request("GET", "/"), rest, deps);
    expect(resp.status).toBe(503);
    expect(resp.headers.get("Content-Type")).toBe("text/plain");
    expect(await resp.text()).toContain("DIGEST_ORIGIN");
    expect(deps.fetchImpl.calls).toHaveLength(0);
  });

  it("GET / with empty DIGEST_ORIGIN is 503", async () => {
    const resp = await handleRequest(
      request("GET", "/"),
      env({ DIGEST_ORIGIN: "" }),
      makeDeps(),
    );
    expect(resp.status).toBe(503);
  });

  it("GET /index.html proxies to DIGEST_ORIGIN", async () => {
    const deps = makeDeps({ fetchStatus: 200, fetchBody: "<html>" });
    const resp = await handleRequest(request("GET", "/index.html"), env(), deps);
    expect(resp.status).toBe(200);
    const call = deps.fetchImpl.calls[0];
    const url = typeof call.input === "string" ? call.input : call.input.url;
    expect(url).toBe("https://p.pages.dev/index.html");
  });

  it("the Pages project name is an origin on its own", async () => {
    const deps = makeDeps({ fetchStatus: 200, fetchBody: "<html>" });
    const { DIGEST_ORIGIN: _, ...rest } = env();
    const resp = await handleRequest(
      request("GET", "/index.html"),
      { ...rest, CYRIS_PROMOTE_PAGES_PROJECT: "cyris-digest" },
      deps,
    );
    expect(resp.status).toBe(200);
    const call = deps.fetchImpl.calls[0];
    const url = typeof call.input === "string" ? call.input : call.input.url;
    expect(url).toBe("https://cyris-digest.pages.dev/index.html");
  });

  it("DIGEST_ORIGIN wins, because a custom domain is not a subdomain", async () => {
    const deps = makeDeps({ fetchStatus: 200, fetchBody: "<html>" });
    const resp = await handleRequest(
      request("GET", "/index.html"),
      env({ CYRIS_PROMOTE_PAGES_PROJECT: "cyris-digest" }),
      deps,
    );
    expect(resp.status).toBe(200);
    const call = deps.fetchImpl.calls[0];
    const url = typeof call.input === "string" ? call.input : call.input.url;
    expect(url).toBe("https://p.pages.dev/index.html");
  });

  it("neither set is still 503, and names both", async () => {
    const deps = makeDeps();
    const { DIGEST_ORIGIN: _, ...rest } = env();
    const resp = await handleRequest(request("GET", "/"), rest, deps);
    expect(resp.status).toBe(503);
    const body = await resp.text();
    expect(body).toContain("CYRIS_PROMOTE_PAGES_PROJECT");
    expect(body).toContain("DIGEST_ORIGIN");
    expect(deps.fetchImpl.calls).toHaveLength(0);
  });
});

const PAGE = "<html><head><title>x</title></head><body></body></html>";
const PAGE_HEADERS = {
  "Content-Type": "text/html; charset=utf-8",
  ETag: '"abc"',
  "Last-Modified": "Tue, 01 Sep 2026 00:00:00 GMT",
};
const STYLE_1125 = "<style>html:root{--type-scale:1.125}</style>";

const pageDeps = (scale, headers = PAGE_HEADERS) =>
  makeDeps({ scale, fetchBody: PAGE, fetchHeaders: headers });

const containerAnswering = (contentType, status = 200) => () =>
  new Response(contentType === "text/html" ? PAGE : "{}", {
    status,
    headers: { "Content-Type": contentType },
  });

const upstreamRequest = (deps) => deps.fetchImpl.calls[0].input;

describe("HtmlResponsesCarryTheScale", () => {
  it("puts the size last in the head of a proxied page and drops its validators", async () => {
    const deps = pageDeps(1.125);

    const resp = await handleRequest(request("GET", "/2026-08-30-evening.html"), env(), deps);

    expect(resp.status).toBe(200);
    expect(await resp.text()).toContain(`${STYLE_1125}</head>`);
    expect(resp.headers.get("ETag")).toBeNull();
    expect(resp.headers.get("Last-Modified")).toBeNull();
    expect(deps.injectStyle.calls).toEqual([STYLE_1125]);
  });

  it("passes a page through untouched at size 1", async () => {
    const deps = pageDeps(1);

    const resp = await handleRequest(request("GET", "/2026-08-30-evening.html"), env(), deps);

    expect(deps.injectStyle.calls).toEqual([]);
    expect(resp.headers.get("ETag")).toBe('"abc"');
    expect(await resp.text()).toBe(PAGE);
  });

  it("leaves what is not HTML alone", async () => {
    const deps = pageDeps(1.125, { "Content-Type": "image/png", ETag: '"abc"' });

    const resp = await handleRequest(request("GET", "/logo.png"), env(), deps);

    expect(deps.injectStyle.calls).toEqual([]);
    expect(resp.headers.get("ETag")).toBe('"abc"');
  });

  it("drops the validators of a HEAD for a page too", async () => {
    const deps = pageDeps(1.125);

    const resp = await handleRequest(request("HEAD", "/"), env(), deps);

    expect(resp.status).toBe(200);
    expect(resp.headers.get("ETag")).toBeNull();
  });

  it("sizes /settings as the container serves it", async () => {
    const deps = makeDeps({ scale: 0.875, containerResponse: containerAnswering("text/html") });
    const jwt = await accessJwt();

    const resp = await handleRequest(request("GET", "/settings", { jwt }), env(), deps);

    expect(await resp.text()).toContain("<style>html:root{--type-scale:0.875}</style></head>");
    expect(deps.injectStyle.calls).toHaveLength(1);
  });

  it("leaves the container's JSON alone", async () => {
    const deps = makeDeps({ scale: 1.125, containerResponse: containerAnswering("application/json") });
    const jwt = await accessJwt();

    await handleRequest(request("GET", "/api/settings", { jwt }), env(), deps);

    expect(deps.injectStyle.calls).toEqual([]);
    expect(deps.typeScale.reads).toBe(0);
  });

  it("puts no token in a sized page", async () => {
    const deps = pageDeps(1.125);

    const resp = await handleRequest(
      request("GET", "/2026-08-30-evening.html"),
      env({ CLOUDFLARE_API_TOKEN: "tok-SECRET" }),
      deps,
    );

    expect(await resp.text()).not.toContain("tok-SECRET");
    expect([...resp.headers].filter(([, value]) => value.includes("tok-SECRET"))).toEqual([]);
  });
});

describe("ScaledHtmlDropsValidators", () => {
  const conditional = {
    "If-None-Match": '"abc"',
    "If-Modified-Since": "Tue, 01 Sep 2026 00:00:00 GMT",
    Accept: "text/html",
  };

  it("asks Pages without validators at another size", async () => {
    const deps = pageDeps(1.125);

    await handleRequest(request("GET", "/", { headers: conditional }), env(), deps);

    const upstream = upstreamRequest(deps);
    expect(upstream.headers.get("If-None-Match")).toBeNull();
    expect(upstream.headers.get("If-Modified-Since")).toBeNull();
    expect(upstream.headers.get("Accept")).toBe("text/html");
  });

  it("forwards them at size 1", async () => {
    const deps = pageDeps(1);

    await handleRequest(request("GET", "/", { headers: conditional }), env(), deps);

    expect(upstreamRequest(deps).headers.get("If-None-Match")).toBe('"abc"');
  });

  it("sends Pages no token", async () => {
    const deps = pageDeps(1.125);

    await handleRequest(
      request("GET", "/", { headers: conditional }),
      env({ CLOUDFLARE_API_TOKEN: "tok-SECRET" }),
      deps,
    );

    const upstream = upstreamRequest(deps);
    expect([...upstream.headers].filter(([, value]) => value.includes("tok-SECRET"))).toEqual([]);
    expect(upstream.headers.get("Authorization")).toBeNull();
  });
});

describe("SettingsSaveForgetsTheScale", () => {
  const save = async (deps, path, jwt) =>
    handleRequest(request("POST", path, { jwt, body: { values: {} } }), env(), deps);

  it("forgets the size when a values save lands", async () => {
    const deps = makeDeps({ containerResponse: containerAnswering("application/json") });

    await save(deps, "/api/settings/values", await accessJwt());

    expect(deps.typeScale.forgets).toBe(1);
  });

  it("keeps it when the save is refused", async () => {
    const deps = makeDeps({ containerResponse: containerAnswering("application/json", 400) });

    await save(deps, "/api/settings/values", await accessJwt());

    expect(deps.typeScale.forgets).toBe(0);
  });

  it("keeps it for a request that never reaches the container", async () => {
    const deps = makeDeps();

    const resp = await save(deps, "/api/settings/values", undefined);

    expect(resp.status).toBe(403);
    expect(deps.typeScale.forgets).toBe(0);
    expect(deps.container.calls).toHaveLength(0);
  });

  it("keeps it for another settings route", async () => {
    const deps = makeDeps({ containerResponse: containerAnswering("application/json") });

    await save(deps, "/api/settings", await accessJwt());

    expect(deps.typeScale.forgets).toBe(0);
  });
});

describe("PrivateArchiveRefusesAnonymousReaders", () => {
  const PRIVATE = { CYRIS_PRIVATE_ARCHIVE: "true" };
  const anonymous = async (method, path, extra = PRIVATE, opts = {}) => {
    const deps = makeDeps();
    const resp = await handleRequest(request(method, path, opts), env(extra), deps);
    return { resp, deps };
  };

  it("refuses the archive root with 403 without reading the archive", async () => {
    const { resp, deps } = await anonymous("GET", "/");

    expect(resp.status).toBe(403);
    expect(resp.headers.get("Set-Cookie")).toBeNull();
    expect(deps.typeScale.reads).toBe(0);
    expect(deps.fetchImpl.calls).toHaveLength(0);
  });

  it("refuses every public route", async () => {
    for (const row of routes.filter((r) => r.kind === "public")) {
      const { resp, deps } = await anonymous(row.method, row.path);

      expect(resp.status, row.path).toBe(403);
      expect(deps.fetchImpl.calls, row.path).toHaveLength(0);
    }
  });

  it("refuses a value that is not a JWT", async () => {
    const { resp } = await anonymous("GET", "/", PRIVATE, { jwt: "deadbeef" });

    expect(resp.status).toBe(403);
  });

  it("does not let Access stand in for the JWT", async () => {
    const { resp } = await anonymous(
      "GET",
      "/",
      PRIVATE,
      { host: ACCESS_HOST },
    );

    expect(resp.status).toBe(403);
  });

  it("refuses HEAD and POST too", async () => {
    for (const method of ["HEAD", "POST"]) {
      const { resp } = await anonymous(method, "/");

      expect(resp.status, method).toBe(403);
    }
  });

  it("refuses with 403, not 503, when no archive origin is named", async () => {
    const { resp } = await anonymous("GET", "/", { ...PRIVATE, DIGEST_ORIGIN: "" });

    expect(resp.status).toBe(403);
  });

  it("reads a boolean true as on", async () => {
    const { resp } = await anonymous("GET", "/", { CYRIS_PRIVATE_ARCHIVE: true });

    expect(resp.status).toBe(403);
  });

  it("leaves every other route's answer unchanged", async () => {
    for (const row of routes.filter((r) => r.kind !== "public")) {
      const deps = makeDeps();
      const resp = await handleRequest(request(row.method, row.path), env(PRIVATE), deps);

      expect(resp.status, `${row.method} ${row.path}`).toBe(row.unauth_status);
      expect(deps.container.calls, row.path).toHaveLength(0);
      expect(deps.startRun.calls, row.path).toHaveLength(0);
    }
  });
});

describe("PrivateArchiveServesSignedInReaders", () => {
  const PRIVATE = { CYRIS_PRIVATE_ARCHIVE: "true" };

  it("serves an archive page through the proxy", async () => {
    const deps = makeDeps();
    const resp = await handleRequest(
      request("GET", "/2026-08-30-evening.html", { jwt: await accessJwt() }),
      env(PRIVATE),
      deps,
    );

    expect(resp.status).toBe(200);
    expect(deps.fetchImpl.calls).toHaveLength(1);
    expect(deps.fetchImpl.calls[0].input.url).toBe("https://p.pages.dev/2026-08-30-evening.html");
    expect(deps.typeScale.reads).toBe(1);
  });

  it("sizes an HTML page as the public archive does", async () => {
    const deps = makeDeps({
      scale: 1.125,
      fetchHeaders: { "Content-Type": "text/html", ETag: '"v1"' },
      fetchBody: "<html><head></head><body></body></html>",
    });
    const resp = await handleRequest(
      request("GET", "/", { jwt: await accessJwt() }),
      env(PRIVATE),
      deps,
    );

    expect(await resp.text()).toContain("--type-scale:1.125");
    expect(resp.headers.get("ETag")).toBeNull();
  });

  it("answers 503 when no archive origin is named", async () => {
    const resp = await handleRequest(
      request("GET", "/", { jwt: await accessJwt() }),
      env({ ...PRIVATE, DIGEST_ORIGIN: "" }),
      makeDeps(),
    );

    expect(resp.status).toBe(503);
  });

  it("passes an upstream 404 through", async () => {
    const resp = await handleRequest(
      request("GET", "/", { jwt: await accessJwt() }),
      env(PRIVATE),
      makeDeps({ fetchStatus: 404 }),
    );

    expect(resp.status).toBe(404);
  });

  it("keeps the reader's Access credentials from the archive origin", async () => {
    const deps = makeDeps();
    await handleRequest(
      request("GET", "/", { jwt: await accessJwt(), headers: { Cookie: "CF_Authorization=tok" } }),
      env(PRIVATE),
      deps,
    );

    const sent = upstreamRequest(deps).headers;
    expect(sent.get("Cf-Access-Jwt-Assertion")).toBeNull();
    expect(sent.get("Cookie")).toBeNull();
  });
});

describe("MalformedTeamDomainFailsClosed", () => {
  const NO_NETWORK = () => {
    throw new Error("the team domain's keys must not be fetched");
  };

  // accessKeys trims, so a padded domain still names its team; an inner space names no host.
  it("takes a padded team domain as the trimmed one", async () => {
    const domain = "  padded-ok.example";
    const certs = vi.fn(async () => Response.json({ keys: [JWK] }));
    const deps = makeDeps({ accessKeys: accessKeys(domain, { fetchImpl: certs }) });
    const resp = await handleRequest(
      request("GET", "/settings", { jwt: await accessJwt({ iss: "https://padded-ok.example" }) }),
      env({ CYRIS_ACCESS_TEAM_DOMAIN: domain }),
      deps,
    );

    expect(resp.status).toBe(200);
    expect(certs).toHaveBeenCalled();
    expect(deps.container.calls).toHaveLength(1);
  });

  it("answers a protected path with a JSON 403 when the team domain names no host", async () => {
    const domain = "inner-case .example";
    const certs = vi.fn(NO_NETWORK);
    const resp = await handleRequest(
      request("GET", "/settings", { jwt: await accessJwt({ iss: "https://inner-case .example" }) }),
      env({ CYRIS_ACCESS_TEAM_DOMAIN: domain }),
      makeDeps({ accessKeys: accessKeys(domain, { fetchImpl: certs }) }),
    );

    expect(resp.status).toBe(403);
    expect(resp.headers.get("Content-Type")).toBe("application/json");
    expect(certs).not.toHaveBeenCalled();
  });

  it.each(["  padded-case.example", "inner-case .example"])(
    "still serves the public archive when the team domain is %j",
    async (domain) => {
      const certs = vi.fn(NO_NETWORK);
      const resp = await handleRequest(
        request("GET", "/"),
        env({ CYRIS_ACCESS_TEAM_DOMAIN: domain }),
        makeDeps({ accessKeys: accessKeys(domain, { fetchImpl: certs }) }),
      );

      expect(resp.status).toBe(200);
      expect(certs).not.toHaveBeenCalled();
    },
  );
});

describe("PublicArchiveUnchangedWhenFlagIsOff", () => {
  it("serves the archive when the switch is the string false", async () => {
    const deps = makeDeps();
    const resp = await handleRequest(
      request("GET", "/"),
      env({ CYRIS_PRIVATE_ARCHIVE: "false" }),
      deps,
    );

    expect(resp.status).toBe(200);
    expect(deps.fetchImpl.calls).toHaveLength(1);
    expect(deps.fetchImpl.calls[0].input.url).toBe("https://p.pages.dev/");
  });

  it("serves the archive when the switch is empty", async () => {
    const resp = await handleRequest(
      request("GET", "/2026-08-30-evening.html"),
      env({ CYRIS_PRIVATE_ARCHIVE: "" }),
      makeDeps(),
    );

    expect(resp.status).toBe(200);
  });
});

describe("InterestPageGate", () => {
  const anonymous = async (method, path, extra = {}, opts = {}) => {
    const deps = makeDeps();
    const resp = await handleRequest(request(method, path, opts), env(extra), deps);
    return { resp, deps };
  };

  it("refuses an anonymous reader without reaching the container or Pages", async () => {
    const { resp, deps } = await anonymous("GET", "/interest", {}, { headers: { Accept: "text/html" } });

    expect(resp.status).toBe(403);
    expect(resp.headers.get("Content-Type")).toBe("application/json");
    expect(deps.fetchImpl.calls).toHaveLength(0);
    expect(deps.container.calls).toHaveLength(0);
    expect(resp.headers.get("Set-Cookie")).toBeNull();
  });

  it("refuses every spelling of the page, including the encoded one", async () => {
    for (const [method, path] of [
      ["GET", "/interest"],
      ["GET", "/interest/"],
      ["GET", "/interest.html"],
      ["HEAD", "/interest.html"],
      ["GET", "/%69nterest"],
      ["GET", "/%69nterest.html"],
      ["GET", "/%E0%A4%A"],
    ]) {
      const { resp, deps } = await anonymous(method, path);

      expect(resp.status, `${method} ${path}`).toBe(403);
      expect(deps.fetchImpl.calls, path).toHaveLength(0);
      expect(deps.container.calls, path).toHaveLength(0);
    }
  });

  it("refuses the page on a workers.dev hostname too", async () => {
    const { resp } = await anonymous("GET", "/interest", {}, { host: "cyris-app.example.workers.dev" });

    expect(resp.status).toBe(403);
  });

  it("serves the page from the container to a signed-in reader", async () => {
    const deps = makeDeps();
    const resp = await handleRequest(
      request("GET", "/interest", { jwt: await accessJwt() }),
      env(),
      deps,
    );

    expect(resp.status).toBe(200);
    expect(deps.container.calls).toHaveLength(1);
    expect(deps.fetchImpl.calls).toHaveLength(0);
  });

  it("leaves the archive root and its pages public when the flag is off", async () => {
    for (const path of ["/", "/2026-08-30-evening.html"]) {
      const { resp } = await anonymous("GET", path);

      expect(resp.status, path).toBe(200);
    }
  });

  it("does not gate paths that only start like the page", async () => {
    for (const path of ["/interestx.html", "/interest.html.bak"]) {
      const { resp, deps } = await anonymous("GET", path);

      expect(resp.status, path).toBe(200);
      expect(deps.fetchImpl.calls, path).toHaveLength(1);
    }
  });
});

describe("AccessJwtCheck", () => {
  const GET_SETTINGS = (jwt, extra = {}) => request("GET", "/settings", { jwt, ...extra });
  const unsignedJwt = () => {
    const b64 = (o) => Buffer.from(JSON.stringify(o)).toString("base64url");
    return `${b64({ alg: "none" })}.${b64({ iss: `https://${TEAM}`, aud: AUD, exp: Math.floor(Date.now() / 1000) + 3600 })}.`;
  };

  it("admits a valid JWT whose audience list holds the AUD", async () => {
    const deps = makeDeps();
    const jwt = await accessJwt({ aud: [AUD, "other"] });
    const resp = await handleRequest(GET_SETTINGS(jwt), env(), deps);
    expect(resp.status).toBe(200);
    expect(deps.container.calls).toHaveLength(1);
  });

  it("admits a service-token style JWT with no subject", async () => {
    const deps = makeDeps();
    const jwt = await accessJwt({ claims: { common_name: "id.access", sub: "" } });
    await handleRequest(GET_SETTINGS(jwt), env(), deps);
    expect(deps.container.calls).toHaveLength(1);
  });

  it("refuses an audience that is not the AUD", async () => {
    const deps = makeDeps();
    const resp = await handleRequest(GET_SETTINGS(await accessJwt({ aud: ["other"] })), env(), deps);
    expect(resp.status).toBe(403);
    expect(deps.container.calls).toHaveLength(0);
  });

  it("refuses an issuer other than the team domain", async () => {
    const deps = makeDeps();
    const resp = await handleRequest(
      GET_SETTINGS(await accessJwt({ iss: "https://evil.example" })),
      env(),
      deps,
    );
    expect(resp.status).toBe(403);
    expect(deps.container.calls).toHaveLength(0);
  });

  it("refuses a token signed by another key under the same kid", async () => {
    const other = await generateKeyPair("RS256");
    const resp = await handleRequest(
      GET_SETTINGS(await accessJwt({ key: other.privateKey })),
      env(),
      makeDeps(),
    );
    expect(resp.status).toBe(403);
  });

  it("refuses an expired token", async () => {
    const resp = await handleRequest(
      GET_SETTINGS(await accessJwt({ exp: Math.floor(Date.now() / 1000) - 60 })),
      env(),
      makeDeps(),
    );
    expect(resp.status).toBe(403);
  });

  it("refuses an unsigned token with alg none", async () => {
    const resp = await handleRequest(GET_SETTINGS(unsignedJwt()), env(), makeDeps());
    expect(resp.status).toBe(403);
  });

  it("reads the header only, never the CF_Authorization cookie", async () => {
    const jwt = await accessJwt();
    const req = new Request(`https://${HOST}/settings`, { headers: { Cookie: `CF_Authorization=${jwt}` } });
    const resp = await handleRequest(req, env(), makeDeps());
    expect(resp.status).toBe(403);
  });

  it("keeps a malformed header out of the response and the console", async () => {
    const spies = ["log", "warn", "error"].map((m) => vi.spyOn(console, m).mockImplementation(() => {}));
    try {
      const resp = await handleRequest(GET_SETTINGS("not-a-jwt"), env(), makeDeps());
      const text = await resp.text();
      expect(resp.status).toBe(403);
      expect(text).not.toContain("not-a-jwt");
      for (const spy of spies) {
        for (const call of spy.mock.calls) expect(JSON.stringify(call)).not.toContain("not-a-jwt");
      }
    } finally {
      spies.forEach((spy) => spy.mockRestore());
    }
  });

  it("logs the code of a certs fetch that fails, and never the token", async () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      const keySet = createRemoteJWKSet(new URL(`https://${TEAM}/cdn-cgi/access/certs`), {
        [customFetch]: async () => new Response("unavailable", { status: 503 }),
      });
      const jwt = await accessJwt();
      const resp = await handleRequest(
        GET_SETTINGS(jwt),
        env(),
        makeDeps({ accessKeys: { keySet, issuer: `https://${TEAM}` } }),
      );
      expect(resp.status).toBe(403);
      expect(spy.mock.calls).toEqual([["Access JWT check failed:", "ERR_JOSE_GENERIC"]]);
      expect(JSON.stringify(spy.mock.calls)).not.toContain(jwt);
    } finally {
      spy.mockRestore();
    }
  });

  it("logs nothing for a token that is expired, forged, unsigned or for another app", async () => {
    const other = await generateKeyPair("RS256");
    const tokens = [
      await accessJwt({ exp: Math.floor(Date.now() / 1000) - 60 }),
      await accessJwt({ key: other.privateKey }),
      unsignedJwt(),
      await accessJwt({ aud: ["other"] }),
      "not-a-jwt",
    ];
    const spies = ["log", "warn", "error"].map((m) => vi.spyOn(console, m).mockImplementation(() => {}));
    try {
      for (const jwt of tokens) {
        const resp = await handleRequest(GET_SETTINGS(jwt), env(), makeDeps());
        expect(resp.status).toBe(403);
      }
      for (const spy of spies) expect(spy).not.toHaveBeenCalled();
    } finally {
      spies.forEach((spy) => spy.mockRestore());
    }
  });

  it("names CYRIS_ACCESS_TEAM_DOMAIN when it is unset", async () => {
    const jwt = await accessJwt();
    const resp = await handleRequest(
      GET_SETTINGS(jwt),
      env({ CYRIS_ACCESS_TEAM_DOMAIN: "" }),
      makeDeps(),
    );
    expect(resp.status).toBe(403);
    expect((await resp.json()).detail).toContain("CYRIS_ACCESS_TEAM_DOMAIN");
  });

  it("names CYRIS_ACCESS_AUD alone when only it is unset", async () => {
    const jwt = await accessJwt();
    const resp = await handleRequest(GET_SETTINGS(jwt), env({ CYRIS_ACCESS_AUD: "" }), makeDeps());
    const detail = (await resp.json()).detail;
    expect(resp.status).toBe(403);
    expect(detail).toContain("CYRIS_ACCESS_AUD");
    expect(detail).not.toContain("CYRIS_ACCESS_TEAM_DOMAIN");
  });

  it("refuses when the key set is missing", async () => {
    const jwt = await accessJwt();
    const resp = await handleRequest(GET_SETTINGS(jwt), env(), makeDeps({ accessKeys: null }));
    expect(resp.status).toBe(403);
  });

  it("refuses, not 500, when the key set rejects", async () => {
    const jwt = await accessJwt();
    const deps = makeDeps({
      accessKeys: {
        keySet: async () => {
          throw new Error("certs unreachable");
        },
        issuer: `https://${TEAM}`,
      },
    });
    const resp = await handleRequest(GET_SETTINGS(jwt), env(), deps);
    expect(resp.status).toBe(403);
  });
});

describe("AccessGate", () => {
  const forbiddenShape = async (resp) => {
    expect(resp.status).toBe(403);
    expect(resp.headers.get("Content-Type")).toBe("application/json");
    expect(resp.headers.get("Set-Cookie")).toBeNull();
    const body = await resp.json();
    expect(body.error).toBe("forbidden");
    expect(typeof body.detail).toBe("string");
  };

  it("refuses the same way on a workers.dev host and on a custom domain", async () => {
    for (const host of [HOST, "cyris.example.com"]) {
      const deps = makeDeps();
      await forbiddenShape(await handleRequest(request("GET", "/settings", { host }), env(), deps));
      expect(deps.container.calls, host).toHaveLength(0);
    }
  });

  it("refuses GET, HEAD, POST and PUT on /api/vote with one body shape", async () => {
    for (const method of ["GET", "HEAD", "POST", "PUT"]) {
      const deps = makeDeps();
      const body = method === "GET" || method === "HEAD" ? undefined : VOTE_BODY;
      const resp = await handleRequest(request(method, "/api/vote", { body }), env(), deps);
      await forbiddenShape(resp);
      expect(deps.fetchImpl.calls, method).toHaveLength(0);
    }
  });

  it("serves the vote API, run, probe and container once a valid JWT is present", async () => {
    const jwt = await accessJwt();
    const deps = makeDeps({ fetchStatus: 200, fetchBody: { ok: true } });

    const probe = await handleRequest(request("GET", "/api/vote", { jwt }), env(), deps);
    expect(probe.status).toBe(200);
    expect(await probe.json()).toEqual({ authorized: true });

    const put = await handleRequest(request("PUT", "/api/vote", { jwt }), env(), deps);
    expect(put.status).toBe(405);

    const post = await handleRequest(request("POST", "/api/vote", { jwt, body: VOTE_BODY }), env(), deps);
    expect(post.status).toBe(200);
    expect(deps.fetchImpl.calls).toHaveLength(1);
    expect(deps.fetchImpl.calls[0].init.headers.Authorization).toBe("Bearer promote-tok");

    const run = await handleRequest(request("POST", "/run?period=noon", { jwt }), env(), deps);
    expect(run.status).toBe(400);
    expect(deps.startRun.calls).toHaveLength(0);

    const probeGemini = await handleRequest(
      request("POST", "/api/diagnostics/gemini?model=bad%20model", { jwt }),
      env(),
      deps,
    );
    expect(probeGemini.status).toBe(400);
    expect(deps.probeGemini.calls).toHaveLength(0);
  });
});

describe("LoginRedirect", () => {
  const signedIn = async (method, path) =>
    handleRequest(request(method, path, { jwt: await accessJwt() }), env(), makeDeps());

  it("GET /login sends a signed-in reader to the page they came from", async () => {
    const resp = await signedIn("GET", "/login?next=%2F2026-10-17.html");
    expect(resp.status).toBe(302);
    expect(resp.headers.get("Location")).toBe("/2026-10-17.html");
  });

  it("POST /login sends a signed-in reader to the page they came from", async () => {
    const resp = await signedIn("POST", "/login?next=%2Fsettings");
    expect(resp.status).toBe(302);
    expect(resp.headers.get("Location")).toBe("/settings");
  });

  it("falls back to / for an off-origin next", async () => {
    for (const next of ["//evil.example", "https://evil.example"]) {
      const resp = await signedIn("GET", "/login?next=" + encodeURIComponent(next));
      expect(resp.headers.get("Location"), next).toBe("/");
    }
  });

  it("sends / with no next, sets no cookie and reaches no container", async () => {
    const deps = makeDeps();
    const resp = await handleRequest(
      request("GET", "/login", { jwt: await accessJwt() }),
      env(),
      deps,
    );
    expect(resp.status).toBe(302);
    expect(resp.headers.get("Location")).toBe("/");
    expect(resp.headers.get("Set-Cookie")).toBeNull();
    expect(deps.container.calls).toHaveLength(0);
  });
});
