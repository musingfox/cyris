// Request routing and cookie auth. No workerd-only imports: vitest runs this
// under Node. index.js is the thin shim that injects the Container.
//
// One layer always: CYRIS_UI_TOKEN cookie. A second layer (Cloudflare Access)
// only when the request hostname equals CYRIS_UI_ACCESS_HOST.

import { typeScaleStyle } from "./type_scale.js";

const COOKIE = "cyris_session";
const MIN_TOKEN_LENGTH = 32;

export const PROTECTED = (path) =>
  path === "/settings" ||
  path === "/login" ||
  path === "/run" ||
  path.startsWith("/api/") ||
  path.startsWith("/static/");

const VOTE_ONLY = (path) => path === "/api/vote";

const sha256 = async (text) => {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
};

// XOR-fold two equal-length hex digests. No early return.
const ctEqual = (a, b) => {
  let diff = 0;
  for (let i = 0; i < a.length; i++) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
};

const cookieOf = (request) =>
  (request.headers.get("Cookie") || "")
    .split(";")
    .map((c) => c.trim().split("="))
    .find(([k]) => k === COOKIE)?.[1];

async function authorized(request, env) {
  const token = env.CYRIS_UI_TOKEN;
  if (!token) return false;
  const cookie = cookieOf(request);
  if (!cookie) return false;
  const expected = await sha256(token);
  if (cookie.length !== expected.length) return false;
  return ctEqual(cookie, expected);
}

const LOGIN_PAGE = (message) => `<!DOCTYPE html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cyris</title>
<style>
 body{background:#111;color:#eee;font:14px/1.6 system-ui,sans-serif;display:grid;place-items:center;height:100vh;margin:0}
 form{display:flex;flex-direction:column;gap:.8rem;width:min(20rem,80vw)}
 input,button{padding:.7rem;border-radius:6px;border:1px solid #333;font:inherit}
 input{background:#1b1b1b;color:#eee}
 button{background:#c6ff3d;color:#111;border:0;font-weight:600;cursor:pointer}
 p{color:#ff5b8a;margin:0}
</style>
<form method="POST" action="/login">
  <input type="password" name="token" placeholder="access token" autofocus autocomplete="current-password">
  <button type="submit">Enter</button>
  ${message ? `<p>${message}</p>` : ""}
</form>`;

const html = (body, status = 200) =>
  new Response(body, { status, headers: { "Content-Type": "text/html; charset=utf-8" } });

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

// Pages serves every project at <name>.pages.dev, so naming the project already
// names its origin. DIGEST_ORIGIN stays the override — a custom domain, or a
// project reachable at something other than its own subdomain.
const archiveOrigin = (env) =>
  env.DIGEST_ORIGIN ||
  (env.CYRIS_PROMOTE_PAGES_PROJECT
    ? `https://${env.CYRIS_PROMOTE_PAGES_PROJECT}.pages.dev`
    : "");

const isHtml = (response) => (response.headers.get("Content-Type") || "").startsWith("text/html");

// At type size 1 a page passes untouched, validators and all. At any other
// size an HTML page carries it, and drops its validators: a browser holding
// the Pages ETag would otherwise revalidate to a 304 of the page at its old
// size. The request side drops them too, so Pages never answers 304 here.
const VALIDATORS = ["If-None-Match", "If-Modified-Since"];

function sized(response, scale, deps) {
  if (scale === 1 || !isHtml(response)) return response;
  const injected = deps.injectStyle(response, typeScaleStyle(scale));
  const headers = new Headers(injected.headers);
  for (const name of ["ETag", "Last-Modified", "Content-Length"]) headers.delete(name);
  return new Response(injected.body, {
    status: injected.status,
    statusText: injected.statusText,
    headers,
  });
}

const onAccessHost = (request, env) =>
  Boolean(env.CYRIS_UI_ACCESS_HOST) &&
  new URL(request.url).hostname === env.CYRIS_UI_ACCESS_HOST;

async function handleVote(request, env, fetchImpl) {
  // Off the Access hostname the cookie is required, including GET/HEAD probe
  // and methods we would otherwise 405 — cookie check first.
  if (!onAccessHost(request, env) && !(await authorized(request, env))) {
    return json({ authorized: false, error: "unauthorized" }, 401);
  }
  if (request.method === "HEAD" || request.method === "GET") {
    return json({ authorized: true }, 200);
  }
  if (request.method !== "POST") {
    return json({ error: "method not allowed" }, 405);
  }
  const promoteUrl = env.CYRIS_PROMOTE_WORKER_URL;
  if (!promoteUrl) {
    return json({ error: "promote worker not configured" }, 503);
  }
  try {
    const body = await request.json();
    const resp = await fetchImpl(promoteUrl + "/promote", {
      method: "POST",
      headers: {
        Authorization: "Bearer " + env.CYRIS_PROMOTE_TOKEN,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });
    return new Response(resp.body, {
      status: resp.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return json({ error: "promote worker failed" }, 502);
  }
}

const GEMINI_MODEL = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

async function handleGeminiProbe(url, deps) {
  const model = url.searchParams.get("model") ?? "";
  if (!GEMINI_MODEL.test(model)) {
    return json({ error: "model must contain only letters, numbers, dots, underscores, or hyphens" }, 400);
  }
  const result = await deps.probeGemini(model);
  return json(result.body, result.status);
}

export async function handleRequest(request, env, deps) {
  const url = new URL(request.url);
  const fetchImpl = deps.fetchImpl;

  // /triage was removed in M1; return 404 instead of proxying to the archive.
  if (url.pathname === "/triage" || url.pathname.startsWith("/triage/")) {
    return new Response("Not Found", { status: 404, headers: { "Content-Type": "text/plain" } });
  }

  // The digest is a static snapshot Cloudflare already holds.
  if (!PROTECTED(url.pathname)) {
    const origin = archiveOrigin(env);
    if (!origin) {
      return new Response(
        "Neither CYRIS_PROMOTE_PAGES_PROJECT nor DIGEST_ORIGIN is set — name the " +
          "Pages project the digest deploys to, or point DIGEST_ORIGIN at its site",
        { status: 503, headers: { "Content-Type": "text/plain" } },
      );
    }
    const readsPage = request.method === "GET" || request.method === "HEAD";
    const scale = readsPage ? await deps.typeScale.current() : 1;
    let proxied = new Request(origin + url.pathname + url.search, request);
    if (scale !== 1) {
      const headers = new Headers(proxied.headers);
      for (const name of VALIDATORS) headers.delete(name);
      proxied = new Request(proxied, { headers });
    }
    return sized(await fetchImpl(proxied), scale, deps);
  }

  if (url.pathname === "/login") {
    if (request.method !== "POST") return html(LOGIN_PAGE(""));
    const token = env.CYRIS_UI_TOKEN;
    if (!token) return html(LOGIN_PAGE("Wrong token."), 401);
    if (token.length < MIN_TOKEN_LENGTH) {
      return html(
        LOGIN_PAGE(
          "CYRIS_UI_TOKEN must be at least 32 characters — generate one with `openssl rand -hex 32`",
        ),
        503,
      );
    }
    let submitted = "";
    try {
      const form = await request.formData();
      submitted = String(form.get("token") ?? "");
    } catch {
      submitted = "";
    }
    if (!ctEqual(await sha256(submitted), await sha256(token))) {
      return html(LOGIN_PAGE("Wrong token."), 401);
    }
    return new Response(null, {
      status: 302,
      headers: {
        Location: "/",
        "Set-Cookie":
          `${COOKIE}=${await sha256(token)}; HttpOnly; Secure; ` +
          "SameSite=Lax; Path=/; Max-Age=2592000",
      },
    });
  }

  if (VOTE_ONLY(url.pathname)) {
    return handleVote(request, env, fetchImpl);
  }

  if (!(await authorized(request, env))) {
    const wantsHtml = (request.headers.get("Accept") || "").includes("text/html");
    return wantsHtml ? html(LOGIN_PAGE(""), 401) : json({ error: "unauthorized" }, 401);
  }

  if (request.method === "POST" && url.pathname === "/api/diagnostics/gemini") {
    return handleGeminiProbe(url, deps);
  }

  if (request.method === "POST" && url.pathname === "/run") {
    return json(await deps.startRun());
  }

  const response = await deps.container(request);
  // The reader who saved a new size sees it on the next page, not a minute later.
  if (request.method === "POST" && url.pathname === "/api/settings/values" && response.ok) {
    deps.typeScale.forget();
  }
  if (!isHtml(response)) return response;
  return sized(response, await deps.typeScale.current(), deps);
}
