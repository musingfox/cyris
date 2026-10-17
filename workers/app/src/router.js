// Request routing and the Cloudflare Access gate. No workerd-only imports: vitest
// runs this under Node. index.js is the thin shim that injects the Container and
// the Access keys.
//
// A request is signed in only with a Cf-Access-Jwt-Assertion header that jose verifies
// against the team's keys, issuer and audience. A protected path refused by the gate
// answers 403 JSON, whatever the method or Accept header.
//
// CYRIS_PRIVATE_ARCHIVE=true puts the archive behind the same gate. Anything else
// leaves the archive public.

import { jwtVerify } from "jose";
import { typeScaleStyle } from "./type_scale.js";

export const PROTECTED = (path) =>
  path === "/settings" ||
  path === "/labels" ||
  path === "/login" ||
  path === "/run" ||
  path.startsWith("/api/") ||
  path.startsWith("/static/");

const privateArchive = (env) => String(env.CYRIS_PRIVATE_ARCHIVE) === "true";

const VOTE_ONLY = (path) => path === "/api/vote";

// `cyris run --period` takes these; anything else would reach the container and fail there.
const RUN_PERIODS = ["morning", "evening"];

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

// A path a browser would read as another host: two slashes, or a slash and a
// backslash. Checked after parsing, because the parser can produce one from a
// path that did not start with it (a dot segment followed by two slashes).
const OFF_ORIGIN = /^\/[/\\]/;

// Where a sign-in returns to: a path on this origin, or the archive. Anything
// that would leave the origin falls back, so /login is never an open redirect,
// and /login itself would send the reader round again.
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
// Access sends the JWT as a header and keeps it in the CF_Authorization cookie; a static archive needs no cookie.
const ACCESS_CREDENTIALS = ["Cf-Access-Jwt-Assertion", "Cookie"];

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

  // The gate is the only sign-in: every protected path, and every path once the archive is private.
  if (PROTECTED(url.pathname) || privateArchive(env)) {
    const refusal = await accessRefusal(request, env, deps);
    if (refusal) return json({ error: "forbidden", detail: refusal }, 403);
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
    // The archive origin is configurable, so it never sees the reader's Access credentials.
    const headers = new Headers(request.headers);
    for (const name of ACCESS_CREDENTIALS) headers.delete(name);
    if (scale !== 1) for (const name of VALIDATORS) headers.delete(name);
    const proxied = new Request(origin + url.pathname + url.search, new Request(request, { headers }));
    return sized(await deps.fetchImpl(proxied), scale, deps);
  }

  // Access has already signed the reader in, so /login only sends them back where they came from.
  if (url.pathname === "/login") {
    const location = returnPath(url.searchParams.get("next"), url.origin);
    return new Response(null, { status: 302, headers: { Location: location } });
  }

  if (VOTE_ONLY(url.pathname)) {
    return handleVote(request, env, deps);
  }

  if (request.method === "POST" && url.pathname === "/api/diagnostics/gemini") {
    return handleGeminiProbe(url, deps);
  }

  if (request.method === "POST" && url.pathname === "/run") {
    const period = url.searchParams.get("period");
    if (period === null) return json(await deps.startRun());
    if (!RUN_PERIODS.includes(period)) {
      return json({ error: `period must be one of ${RUN_PERIODS.join(", ")}` }, 400);
    }
    return json(await deps.startRun(period));
  }

  const response = await deps.container(request);
  // The reader who saved a new size sees it on the next page, not a minute later.
  if (request.method === "POST" && url.pathname === "/api/settings/values" && response.ok) {
    deps.typeScale.forget();
  }
  if (!isHtml(response)) return response;
  return sized(response, await deps.typeScale.current(), deps);
}
