import { describe, it, expect } from "vitest";
import { handleRequest } from "../src/router.js";
import routes from "./routes.json" with { type: "json" };

const TOKEN = "abcdefghijklmnopqrstuvwxyz123456";
const HOST = "cyris-app.x.workers.dev";
const ACCESS_HOST = "digest.example.test";
const VOTE_BODY = { url: "https://e.x/a", vote: "up", digest_date: "2026-09-01" };

async function sha256(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function makeDeps({
  fetchStatus = 200,
  fetchBody = "<html>",
  fetchThrow = false,
  fetchHeaders = {},
  scale = 1,
  containerResponse = () => new Response("container", { status: 200 }),
} = {}) {
  const wrappedContainer = async (req) => {
    wrappedContainer.calls.push(req);
    return containerResponse();
  };
  wrappedContainer.calls = [];

  const startRun = async () => {
    startRun.calls.push(true);
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

  return {
    container: wrappedContainer,
    startRun,
    probeGemini,
    fetchImpl,
    typeScale,
    injectStyle,
  };
}

function env(extra = {}) {
  return {
    CYRIS_UI_TOKEN: TOKEN,
    DIGEST_ORIGIN: "https://p.pages.dev",
    CYRIS_PROMOTE_WORKER_URL: "https://promote.example",
    CYRIS_PROMOTE_TOKEN: "promote-tok",
    ...extra,
  };
}

function request(method, path, { host = HOST, headers = {}, body, cookie } = {}) {
  const h = new Headers(headers);
  if (cookie) h.set("Cookie", `cyris_session=${cookie}`);
  const init = { method, headers: h };
  if (body instanceof FormData) {
    init.body = body;
  } else if (body !== undefined) {
    init.body = typeof body === "string" ? body : JSON.stringify(body);
    if (!h.has("Content-Type")) h.set("Content-Type", "application/json");
  }
  return new Request(`https://${host}${path}`, init);
}

async function sessionCookie() {
  return sha256(TOKEN);
}

describe("UnauthenticatedWriteSurfaceRejected", () => {
  it("POST /api/settings with no cookie does not invoke the container", async () => {
    const deps = makeDeps();
    const resp = await handleRequest(request("POST", "/api/settings"), env(), deps);
    expect(resp.status).toBe(401);
    expect(deps.container.calls).toHaveLength(0);
  });

  it("POST /api/articles/accept with no cookie does not invoke the container", async () => {
    const deps = makeDeps();
    const resp = await handleRequest(request("POST", "/api/articles/accept"), env(), deps);
    expect(resp.status).toBe(401);
    expect(deps.container.calls).toHaveLength(0);
  });

  it("DELETE /api/sources/demo with no cookie does not invoke the container", async () => {
    const deps = makeDeps();
    const resp = await handleRequest(request("DELETE", "/api/sources/demo"), env(), deps);
    expect(resp.status).toBe(401);
    expect(deps.container.calls).toHaveLength(0);
  });

  it("POST /api/settings/schedule with no cookie does not invoke the container", async () => {
    const deps = makeDeps();
    const resp = await handleRequest(request("POST", "/api/settings/schedule"), env(), deps);
    expect(resp.status).toBe(401);
    expect(deps.container.calls).toHaveLength(0);
  });

  it("POST /api/sources with no cookie does not invoke the container", async () => {
    const deps = makeDeps();
    const resp = await handleRequest(request("POST", "/api/sources"), env(), deps);
    expect(resp.status).toBe(401);
    expect(deps.container.calls).toHaveLength(0);
  });

  it("POST /api/articles/reject with no cookie is 401", async () => {
    const resp = await handleRequest(request("POST", "/api/articles/reject"), env(), makeDeps());
    expect(resp.status).toBe(401);
  });

  it("POST /api/articles/undo with no cookie is 401", async () => {
    const resp = await handleRequest(request("POST", "/api/articles/undo"), env(), makeDeps());
    expect(resp.status).toBe(401);
  });

  it("POST /run with no cookie does not start a run", async () => {
    const deps = makeDeps();
    const resp = await handleRequest(request("POST", "/run"), env(), deps);
    expect(resp.status).toBe(401);
    expect(deps.startRun.calls).toHaveLength(0);
  });

  it("GET /settings with Accept text/html and no cookie is HTML 401", async () => {
    const resp = await handleRequest(
      request("GET", "/settings", { headers: { Accept: "text/html" } }),
      env(),
      makeDeps(),
    );
    expect(resp.status).toBe(401);
    expect(resp.headers.get("Content-Type")).toBe("text/html; charset=utf-8");
  });

  it("GET /static/settings.js with no cookie is 401", async () => {
    const resp = await handleRequest(request("GET", "/static/settings.js"), env(), makeDeps());
    expect(resp.status).toBe(401);
  });

  it("GET /api/zzz-undefined with no cookie is 401", async () => {
    const resp = await handleRequest(request("GET", "/api/zzz-undefined"), env(), makeDeps());
    expect(resp.status).toBe(401);
  });

  it("GET /settings with a junk cookie is 401", async () => {
    const resp = await handleRequest(
      request("GET", "/settings", { cookie: "deadbeef" }),
      env(),
      makeDeps(),
    );
    expect(resp.status).toBe(401);
  });

  it("GET /settings with a well-formed cookie but no token is 401", async () => {
    const cookie = await sessionCookie();
    const resp = await handleRequest(
      request("GET", "/settings", { cookie }),
      env({ CYRIS_UI_TOKEN: "" }),
      makeDeps(),
    );
    expect(resp.status).toBe(401);
  });

  it("GET /api/settings with a valid cookie reaches the container", async () => {
    const deps = makeDeps();
    const cookie = await sessionCookie();
    await handleRequest(request("GET", "/api/settings", { cookie }), env(), deps);
    expect(deps.container.calls).toHaveLength(1);
  });

  it("every routes.json row matches unauth_status on a non-Access host", async () => {
    const cookieLess = env();
    for (const row of routes) {
      const deps = makeDeps();
      const resp = await handleRequest(request(row.method, row.path), cookieLess, deps);
      expect(resp.status, `${row.method} ${row.path}`).toBe(row.unauth_status);
      if (row.kind === "container" || row.kind === "run") {
        expect(deps.container.calls, row.path).toHaveLength(0);
        expect(deps.startRun.calls, row.path).toHaveLength(0);
      }
      if (row.kind === "worker") {
        expect(deps.probeGemini.calls, row.path).toHaveLength(0);
      }
    }
  });
});

describe("GeminiProbeIsWorkerLocal", () => {
  it("POST /api/diagnostics/gemini invokes the Worker probe after cookie authentication", async () => {
    const deps = makeDeps();
    const resp = await handleRequest(
      request("POST", "/api/diagnostics/gemini?model=gemini-3.7-flash", {
        cookie: await sessionCookie(),
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
        cookie: await sessionCookie(),
      }),
      env(),
      deps,
    );

    expect(resp.status).toBe(400);
    expect(deps.probeGemini.calls).toHaveLength(0);
  });
});

describe("VotePostRequiresCookieOffAccessHost", () => {
  it("POST /api/vote with no cookie does not call promote", async () => {
    const deps = makeDeps();
    const resp = await handleRequest(
      request("POST", "/api/vote", { body: VOTE_BODY }),
      env({ CYRIS_UI_ACCESS_HOST: undefined }),
      deps,
    );
    expect(resp.status).toBe(401);
    expect(await resp.json()).toEqual({ authorized: false, error: "unauthorized" });
    expect(deps.fetchImpl.calls).toHaveLength(0);
  });

  it("POST /api/vote with a valid cookie forwards the promote bearer", async () => {
    const deps = makeDeps({ fetchStatus: 201, fetchBody: { ok: true } });
    const cookie = await sessionCookie();
    const resp = await handleRequest(
      request("POST", "/api/vote", { body: { ...VOTE_BODY, vote: "up" }, cookie }),
      env(),
      deps,
    );
    expect(resp.status).toBe(201);
    expect(await resp.json()).toEqual({ ok: true });
    const call = deps.fetchImpl.calls[0];
    expect(call.init.headers.Authorization).toBe("Bearer promote-tok");
  });

  it("POST /api/vote with a valid cookie but no promote URL is 503", async () => {
    const cookie = await sessionCookie();
    const resp = await handleRequest(
      request("POST", "/api/vote", { body: VOTE_BODY, cookie }),
      env({ CYRIS_PROMOTE_WORKER_URL: "" }),
      makeDeps(),
    );
    expect(resp.status).toBe(503);
  });

  it("PUT /api/vote with no cookie on a non-Access host is 401", async () => {
    const resp = await handleRequest(request("PUT", "/api/vote"), env(), makeDeps());
    expect(resp.status).toBe(401);
  });
});

describe("VoteProbeReflectsCookieOffAccessHost", () => {
  it("GET /api/vote with no cookie is JSON 401", async () => {
    const resp = await handleRequest(request("GET", "/api/vote"), env(), makeDeps());
    expect(resp.status).toBe(401);
    expect(resp.headers.get("Content-Type")).toBe("application/json");
    expect(await resp.json()).toEqual({ authorized: false, error: "unauthorized" });
  });

  it("GET /api/vote with a valid cookie is authorized", async () => {
    const cookie = await sessionCookie();
    const resp = await handleRequest(request("GET", "/api/vote", { cookie }), env(), makeDeps());
    expect(resp.status).toBe(200);
    expect(await resp.json()).toEqual({ authorized: true });
  });

  it("HEAD /api/vote with no cookie is 401", async () => {
    const resp = await handleRequest(request("HEAD", "/api/vote"), env(), makeDeps());
    expect(resp.status).toBe(401);
  });

  it("GET /api/vote with Accept text/html still returns JSON", async () => {
    const resp = await handleRequest(
      request("GET", "/api/vote", { headers: { Accept: "text/html" } }),
      env(),
      makeDeps(),
    );
    expect(resp.status).toBe(401);
    expect(resp.headers.get("Content-Type")).toBe("application/json");
  });
});

describe("VoteAccessHostProbeExempt", () => {
  it("GET /api/vote on the Access host needs no cookie", async () => {
    const resp = await handleRequest(
      request("GET", "/api/vote", { host: ACCESS_HOST }),
      env({ CYRIS_UI_ACCESS_HOST: ACCESS_HOST }),
      makeDeps(),
    );
    expect(resp.status).toBe(200);
    expect(await resp.json()).toEqual({ authorized: true });
  });

  it("exemption is bound to the hostname", async () => {
    const resp = await handleRequest(
      request("GET", "/api/vote", { host: HOST }),
      env({ CYRIS_UI_ACCESS_HOST: ACCESS_HOST }),
      makeDeps(),
    );
    expect(resp.status).toBe(401);
  });

  it("unset CYRIS_UI_ACCESS_HOST never exempts", async () => {
    const resp = await handleRequest(
      request("GET", "/api/vote", { host: ACCESS_HOST }),
      env(),
      makeDeps(),
    );
    expect(resp.status).toBe(401);
  });
});

describe("VoteAccessHostPostForwards", () => {
  it("POST /api/vote on the Access host forwards without a cookie", async () => {
    const deps = makeDeps({ fetchStatus: 200, fetchBody: { ok: true } });
    const resp = await handleRequest(
      request("POST", "/api/vote", {
        host: ACCESS_HOST,
        body: { url: "https://e.x/a", vote: "down", digest_date: "2026-09-01" },
      }),
      env({ CYRIS_UI_ACCESS_HOST: ACCESS_HOST }),
      deps,
    );
    expect(resp.status).toBe(200);
    expect(await resp.json()).toEqual({ ok: true });
    const call = deps.fetchImpl.calls[0];
    const url = typeof call.input === "string" ? call.input : call.input.url;
    expect(url).toBe("https://promote.example/promote");
    expect(call.init.headers.Authorization).toBe("Bearer promote-tok");
  });

  it("PUT /api/vote on the Access host is 405", async () => {
    const resp = await handleRequest(
      request("PUT", "/api/vote", { host: ACCESS_HOST }),
      env({ CYRIS_UI_ACCESS_HOST: ACCESS_HOST }),
      makeDeps(),
    );
    expect(resp.status).toBe(405);
  });

  it("POST /api/vote on the Access host maps fetch throws to 502", async () => {
    const deps = makeDeps({ fetchThrow: true });
    const resp = await handleRequest(
      request("POST", "/api/vote", { host: ACCESS_HOST, body: VOTE_BODY }),
      env({ CYRIS_UI_ACCESS_HOST: ACCESS_HOST }),
      deps,
    );
    expect(resp.status).toBe(502);
    expect(await resp.json()).toEqual({ error: "promote worker failed" });
  });
});

describe("LoginIssuesCookieOnConstantTimeMatch", () => {
  it("POST /login with the token sets the session cookie", async () => {
    const form = new FormData();
    form.set("token", TOKEN);
    const resp = await handleRequest(request("POST", "/login", { body: form }), env(), makeDeps());
    expect(resp.status).toBe(302);
    expect(resp.headers.get("Location")).toBe("/");
    const cookie = resp.headers.get("Set-Cookie");
    expect(cookie).toContain("HttpOnly");
    expect(cookie).toContain("Secure");
    expect(cookie).toContain("SameSite=Lax");
    expect(cookie).toContain("Path=/");
    expect(cookie).toContain("Max-Age=2592000");
  });

  it("POST /login with a wrong token is 401 and sets no cookie", async () => {
    const form = new FormData();
    form.set("token", "wrongwrongwrongwrongwrongwrongwr");
    const resp = await handleRequest(request("POST", "/login", { body: form }), env(), makeDeps());
    expect(resp.status).toBe(401);
    expect(await resp.text()).toContain("Wrong token.");
    expect(resp.headers.get("Set-Cookie")).toBeNull();
  });

  it("POST /login with the token but no env token is 401", async () => {
    const form = new FormData();
    form.set("token", TOKEN);
    const resp = await handleRequest(
      request("POST", "/login", { body: form }),
      env({ CYRIS_UI_TOKEN: "" }),
      makeDeps(),
    );
    expect(resp.status).toBe(401);
    expect(resp.headers.get("Set-Cookie")).toBeNull();
  });
});

describe("LoginRefusesWeakToken", () => {
  it("POST /login refuses a short deployment token", async () => {
    const form = new FormData();
    form.set("token", "short");
    const resp = await handleRequest(
      request("POST", "/login", { body: form }),
      env({ CYRIS_UI_TOKEN: "short" }),
      makeDeps(),
    );
    expect(resp.status).toBe(503);
    expect(resp.headers.get("Set-Cookie")).toBeNull();
    expect(await resp.text()).toContain("32");
  });

  it("GET /login still renders the form when the token is short", async () => {
    const resp = await handleRequest(
      request("GET", "/login"),
      env({ CYRIS_UI_TOKEN: "short" }),
      makeDeps(),
    );
    expect(resp.status).toBe(200);
    expect(await resp.text()).toContain("action=\"/login\"");
  });

  it("POST /login with a 32-char token still sets a cookie", async () => {
    const form = new FormData();
    form.set("token", TOKEN);
    const resp = await handleRequest(request("POST", "/login", { body: form }), env(), makeDeps());
    expect(resp.status).toBe(302);
    expect(resp.headers.get("Set-Cookie")).toBeTruthy();
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
    const cookie = await sessionCookie();

    const resp = await handleRequest(request("GET", "/settings", { cookie }), env(), deps);

    expect(await resp.text()).toContain("<style>html:root{--type-scale:0.875}</style></head>");
    expect(deps.injectStyle.calls).toHaveLength(1);
  });

  it("leaves the container's JSON alone", async () => {
    const deps = makeDeps({ scale: 1.125, containerResponse: containerAnswering("application/json") });
    const cookie = await sessionCookie();

    await handleRequest(request("GET", "/api/settings", { cookie }), env(), deps);

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
  const save = async (deps, path, cookie) =>
    handleRequest(request("POST", path, { cookie, body: { values: {} } }), env(), deps);

  it("forgets the size when a values save lands", async () => {
    const deps = makeDeps({ containerResponse: containerAnswering("application/json") });

    await save(deps, "/api/settings/values", await sessionCookie());

    expect(deps.typeScale.forgets).toBe(1);
  });

  it("keeps it when the save is refused", async () => {
    const deps = makeDeps({ containerResponse: containerAnswering("application/json", 400) });

    await save(deps, "/api/settings/values", await sessionCookie());

    expect(deps.typeScale.forgets).toBe(0);
  });

  it("keeps it for a request that never reaches the container", async () => {
    const deps = makeDeps();

    const resp = await save(deps, "/api/settings/values", undefined);

    expect(resp.status).toBe(401);
    expect(deps.typeScale.forgets).toBe(0);
    expect(deps.container.calls).toHaveLength(0);
  });

  it("keeps it for another settings route", async () => {
    const deps = makeDeps({ containerResponse: containerAnswering("application/json") });

    await save(deps, "/api/settings", await sessionCookie());

    expect(deps.typeScale.forgets).toBe(0);
  });
});
