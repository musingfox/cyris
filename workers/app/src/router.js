// Request routing and the Cloudflare Access check. No workerd-only imports: vitest
// runs this under Node. index.js is the thin shim that injects the Container and
// the Access keys.
//
// A request is signed in only with a Cf-Access-Jwt-Assertion header that jose verifies
// against the team's keys, issuer and audience.
//
// CYRIS_PRIVATE_ARCHIVE=true puts the archive behind the same Access check.
// Anything else leaves the archive public.

import { jwtVerify } from "jose";
import { typeScaleStyle } from "./type_scale.js";

const COOKIE = "cyris_session";
const MIN_TOKEN_LENGTH = 32;

const INTEREST_PATHS = ["/interest", "/interest.html", "/interest/"];

// The interest map is drawn by the container from the reader's own votes, so it
// is signed-in only on every deployment, flag or not. Matched on the decoded
// path, so an encoded spelling cannot reach the archive proxy instead; a path
// that does not decode is held back the same way. Only /interest is a page:
// the container answers the other spellings 404.
const interestPage = (path) => {
  let decoded;
  try {
    decoded = decodeURIComponent(path).toLowerCase();
  } catch {
    return true;
  }
  return INTEREST_PATHS.includes(decoded);
};

export const PROTECTED = (path) =>
  path === "/settings" ||
  path === "/onboarding" ||
  path === "/labels" ||
  path === "/login" ||
  path === "/run" ||
  interestPage(path) ||
  path.startsWith("/api/") ||
  path.startsWith("/static/");

const privateArchive = (env) => String(env.CYRIS_PRIVATE_ARCHIVE) === "true";

const VOTE_ONLY = (path) => path === "/api/vote";

// `cyris run --period` takes these; anything else would reach the container and fail there.
const RUN_PERIODS = ["morning", "evening"];

// The deployment's own calendar day, YYYY-MM-DD, at `now`.
const dayIn = (timeZone, now) =>
  new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" })
    .format(now);

const dayBefore = (day) => {
  const date = new Date(`${day}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() - 1);
  return date.toISOString().slice(0, 10);
};

// Why a manual run's named issue date is refused, as [status, error], or null
// to take it. A real YYYY-MM-DD, and today or yesterday in the deployment's time
// zone: yesterday is the onboarding's first issue before the morning hour, and
// anything older would store over an issue already published. Without a
// readable time zone the date cannot be checked, so it is refused rather than
// taken: the wizard, the one caller, can try again.
const UNCHECKED = [503, "could not read the deployment's time zone to check the date; try again"];

async function refuseRunDate(date, deps) {
  const parsed = new Date(`${date}T00:00:00Z`);
  const real = /^\d{4}-\d{2}-\d{2}$/.test(date) && !Number.isNaN(parsed.getTime()) &&
    parsed.toISOString().slice(0, 10) === date;
  if (!real) return [400, "date must be a real YYYY-MM-DD date"];
  const timeZone = deps.runTimeZone ? await deps.runTimeZone() : null;
  if (!timeZone) return UNCHECKED;
  let today;
  try {
    today = dayIn(timeZone, deps.now ? deps.now() : new Date());
  } catch {
    return UNCHECKED;
  }
  if (date === today || date === dayBefore(today)) return null;
  return [400, `date must be today or yesterday in ${timeZone} (${dayBefore(today)} or ${today})`];
}

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

const ACCESS_SETTINGS = ["CYRIS_ACCESS_TEAM_DOMAIN", "CYRIS_ACCESS_AUD"];
const NOT_SIGNED_IN = "This page needs a Cloudflare Access sign-in, and this request has no valid one.";

// null when the request carries a valid Access JWT, else the detail a 403 names.
// Errors from parsing, signature, claims or the key fetch all mean refused, and
// the token never reaches the detail or the console.
async function accessRefusal(request, env, deps) {
  const unset = ACCESS_SETTINGS.filter((name) => !env[name]);
  if (unset.length) {
    return `The Worker has no ${unset.join(" and ")} set, so no Access sign-in can be checked.`;
  }
  const token = request.headers.get("Cf-Access-Jwt-Assertion");
  if (!token || !deps.accessKeys) return NOT_SIGNED_IN;
  try {
    await jwtVerify(token, deps.accessKeys.keySet, {
      issuer: deps.accessKeys.issuer,
      audience: env.CYRIS_ACCESS_AUD,
    });
    return null;
  } catch {
    return NOT_SIGNED_IN;
  }
}

const authorized = async (request, env, deps) => (await accessRefusal(request, env, deps)) === null;

const escapeHtml = (text) =>
  text.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

// A path a browser would read as another host: two slashes, or a slash and a
// backslash. Checked after parsing, because the parser can produce one from a
// path that did not start with it (a dot segment followed by two slashes).
const OFF_ORIGIN = /^\/[/\\]/;

// Where a sign-in returns to: a path on this origin, or the archive. Anything
// that would leave the origin falls back, so /login is never an open redirect,
// and /login itself would only show the form again.
export function returnPath(next, origin) {
  if (typeof next !== "string" || !next.startsWith("/")) return "/";
  let target;
  try {
    target = new URL(next, origin);
  } catch {
    return "/";
  }
  const path = target.pathname + target.search;
  if (target.origin !== origin || OFF_ORIGIN.test(path) || target.pathname === "/login") {
    return "/";
  }
  return path;
}

const LOGIN_PAGE = (message, next = "/") => `<!DOCTYPE html>
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
  <input type="hidden" name="next" value="${escapeHtml(next)}">
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

async function handleVote(request, env, deps) {
  // The Access check comes first, including for GET/HEAD and the methods we would otherwise 405.
  if (!(await authorized(request, env, deps))) {
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
    const resp = await deps.fetchImpl(promoteUrl + "/promote", {
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

  // /triage was removed in M1; return 404 instead of proxying to the archive.
  if (url.pathname === "/triage" || url.pathname.startsWith("/triage/")) {
    return new Response("Not Found", { status: 404, headers: { "Content-Type": "text/plain" } });
  }

  // The digest is a static snapshot Cloudflare already holds.
  if (!PROTECTED(url.pathname)) {
    if (privateArchive(env) && !(await authorized(request, env, deps))) {
      const login = new URL("/login", url.origin);
      login.searchParams.set("next", url.pathname + url.search);
      return new Response(null, {
        status: 302,
        headers: { Location: login.pathname + login.search },
      });
    }
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
    return sized(await deps.fetchImpl(proxied), scale, deps);
  }

  if (url.pathname === "/login") {
    if (request.method !== "POST") {
      return html(LOGIN_PAGE("", returnPath(url.searchParams.get("next"), url.origin)));
    }
    let submitted = "";
    let next = "/";
    try {
      const form = await request.formData();
      submitted = String(form.get("token") ?? "");
      next = returnPath(form.get("next"), url.origin);
    } catch {
      submitted = "";
    }
    const token = env.CYRIS_UI_TOKEN;
    if (!token) return html(LOGIN_PAGE("Wrong token.", next), 401);
    if (token.length < MIN_TOKEN_LENGTH) {
      return html(
        LOGIN_PAGE(
          "CYRIS_UI_TOKEN must be at least 32 characters — generate one with `openssl rand -hex 32`",
          next,
        ),
        503,
      );
    }
    if (!ctEqual(await sha256(submitted), await sha256(token))) {
      return html(LOGIN_PAGE("Wrong token.", next), 401);
    }
    return new Response(null, {
      status: 302,
      headers: {
        Location: next,
        "Set-Cookie":
          `${COOKIE}=${await sha256(token)}; HttpOnly; Secure; ` +
          "SameSite=Lax; Path=/; Max-Age=2592000",
      },
    });
  }

  if (VOTE_ONLY(url.pathname)) {
    return handleVote(request, env, deps);
  }

  const refusal = await accessRefusal(request, env, deps);
  if (refusal) return json({ error: "forbidden", detail: refusal }, 403);

  if (request.method === "POST" && url.pathname === "/api/diagnostics/gemini") {
    return handleGeminiProbe(url, deps);
  }

  if (request.method === "POST" && url.pathname === "/run") {
    const period = url.searchParams.get("period");
    const date = url.searchParams.get("date");
    if (period === null) {
      // `cyris run --if-due` dates its issue by the tick's own clock.
      if (date !== null) return json({ error: "date needs a period" }, 400);
      return json(await deps.startRun());
    }
    if (!RUN_PERIODS.includes(period)) {
      return json({ error: `period must be one of ${RUN_PERIODS.join(", ")}` }, 400);
    }
    if (date === null) return json(await deps.startRun(period));
    const refused = await refuseRunDate(date, deps);
    if (refused) return json({ error: refused[1] }, refused[0]);
    return json(await deps.startRun(period, date));
  }

  const response = await deps.container(request);
  // The reader who saved a new size sees it on the next page, not a minute later.
  if (request.method === "POST" && url.pathname === "/api/settings/values" && response.ok) {
    deps.typeScale.forget();
  }
  if (!isHtml(response)) return response;
  return sized(response, await deps.typeScale.current(), deps);
}
