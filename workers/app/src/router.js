// Request routing and cookie auth. No workerd-only imports: vitest runs this
// under Node. index.js is the thin shim that injects the Container.
//
// One layer always: CYRIS_UI_TOKEN cookie. A second layer (Cloudflare Access)
// only when the request hostname equals CYRIS_UI_ACCESS_HOST.

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

export async function handleRequest(request, env, deps) {
  const url = new URL(request.url);
  const fetchImpl = deps.fetchImpl;

  // /triage was removed in M1; return 404 instead of proxying to the archive.
  if (url.pathname === "/triage" || url.pathname.startsWith("/triage/")) {
    return new Response("Not Found", { status: 404, headers: { "Content-Type": "text/plain" } });
  }

  // The digest is a static snapshot Cloudflare already holds.
  if (!PROTECTED(url.pathname)) {
    if (!env.DIGEST_ORIGIN) {
      return new Response(
        "DIGEST_ORIGIN is not set — point it at your Pages site, e.g. https://<project>.pages.dev",
        { status: 503, headers: { "Content-Type": "text/plain" } },
      );
    }
    return fetchImpl(new Request(env.DIGEST_ORIGIN + url.pathname + url.search, request));
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

  if (request.method === "POST" && url.pathname === "/run") {
    return json(await deps.startRun());
  }

  return deps.container(request);
}
