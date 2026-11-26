// Hosted waitlist endpoint: POST /waitlist (and OPTIONS /waitlist preflight), called cross-origin
// from the static landing page (website/index.html) and stored in TenantRegistry (tenant_registry.js).
// Protected by strict Origin allowlist, IP/daily rate limits, Cloudflare Turnstile Siteverify, and
// a registry entry cap. No workerd-only imports: vitest runs this under Node.

import { PRIVATE_HEADERS } from "./hosted_pages.js";
import { isEmail, normalEmail, tokenHash } from "./hosted_session.js";
import { boundedText } from "./hosted_vote.js";

export const WAITLIST_PATH = "/waitlist";
export const WAITLIST_TURNSTILE_ACTION = "waitlist";

export const MAX_WAITLIST_BODY_BYTES = 4096;
export const MAX_WAITLIST_NOTE_CHARS = 500;
export const WAITLIST_MAX_ENTRIES = 5000;
export const WAITLIST_IP_HOURLY_CAP = 5;
export const WAITLIST_DAILY_CAP = 500;
export const WAITLIST_RATE_HOUR_MS = 60 * 60 * 1000;
export const WAITLIST_RATE_DAY_MS = 24 * 60 * 60 * 1000;

export const TURNSTILE_TIMEOUT_MS = 10_000;
export const TURNSTILE_SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify";
export const TURNSTILE_MAX_TOKEN_CHARS = 2048;

export const WAITLIST_LANGUAGES = ["en", "zh-Hant"];

// Strip ASCII control characters except newline (\n), matching single-field sanitization.
// eslint-disable-next-line no-control-regex
const DISALLOWED_CONTROL = /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g;

// Parses comma-separated allowed origins into normalized http(s) origins and hostnames.
export function parseAllowedOrigins(raw) {
  const origins = new Set();
  const hostnames = new Set();
  if (typeof raw !== "string") return { origins, hostnames };
  for (const part of raw.split(",")) {
    const trimmed = part.trim();
    if (!trimmed) continue;
    try {
      const parsed = new URL(trimmed);
      if (parsed.protocol !== "http:" && parsed.protocol !== "https:") continue;
      if (parsed.pathname !== "/" || parsed.search || parsed.hash) continue;
      origins.add(parsed.origin);
      hostnames.add(parsed.hostname);
    } catch {
      // Ignore malformed origin entries so one bad token does not break parsing of valid ones.
    }
  }
  return { origins, hostnames };
}

export function normalizeNote(raw) {
  if (raw === undefined || raw === null) return { note: "" };
  if (typeof raw !== "string") {
    return { refused: "note must be a string", code: "invalid_note" };
  }
  const cleaned = raw.replace(/\r\n?/g, "\n").replace(DISALLOWED_CONTROL, "").trim();
  if (cleaned.length > MAX_WAITLIST_NOTE_CHARS) {
    return { refused: `note must be at most ${MAX_WAITLIST_NOTE_CHARS} characters`, code: "note_too_long" };
  }
  return { note: cleaned };
}

export function normalizeLanguage(raw) {
  return WAITLIST_LANGUAGES.includes(raw) ? raw : "en";
}

export async function verifyTurnstile({ token, secret, remoteip, expectedHostnames, fetchFn = fetch }) {
  if (
    typeof secret !== "string" ||
    !secret.trim() ||
    typeof token !== "string" ||
    token.length === 0 ||
    token.length > TURNSTILE_MAX_TOKEN_CHARS ||
    !expectedHostnames ||
    expectedHostnames.size === 0
  ) {
    return false;
  }
  try {
    const params = new URLSearchParams({ secret: secret.trim(), response: token });
    if (remoteip) params.set("remoteip", remoteip);
    const response = await fetchFn(TURNSTILE_SITEVERIFY_URL, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      signal: AbortSignal.timeout(TURNSTILE_TIMEOUT_MS),
      body: params,
    });
    if (!response.ok) return false;
    const result = await response.json();
    return (
      result?.success === true &&
      result?.action === WAITLIST_TURNSTILE_ACTION &&
      expectedHostnames.has(result?.hostname)
    );
  } catch {
    return false;
  }
}

function respond(body, status, allowOrigin, extraHeaders = {}) {
  const headers = new Headers({
    ...(body === null ? {} : { "Content-Type": "application/json" }),
    "Cache-Control": "no-store",
    Vary: "Origin",
    ...PRIVATE_HEADERS,
    ...extraHeaders,
  });
  if (allowOrigin) {
    headers.set("Access-Control-Allow-Origin", allowOrigin);
    headers.set("Access-Control-Allow-Methods", "POST, OPTIONS");
    headers.set("Access-Control-Allow-Headers", "Content-Type");
    headers.set("Access-Control-Max-Age", "600");
  }
  return new Response(body === null ? null : JSON.stringify(body), { status, headers });
}

// `deps`: `{ recordAttempt(ipHash), join({ email, note, language }), fetchTurnstile? }`.
export async function handleWaitlist(request, env, deps) {
  const allowed = parseAllowedOrigins(env?.CYRIS_WAITLIST_ORIGINS);
  const secret = typeof env?.CYRIS_WAITLIST_TURNSTILE_SECRET === "string"
    ? env.CYRIS_WAITLIST_TURNSTILE_SECRET.trim()
    : "";
  const origin = request.headers.get("Origin");
  const allowOrigin = origin && allowed.origins.has(origin) ? origin : null;

  if (allowed.origins.size === 0 || !secret) {
    return respond({ error: "waitlist is not configured", code: "waitlist_not_configured" }, 503, allowOrigin);
  }

  if (request.method === "OPTIONS") {
    if (!allowOrigin) return respond({ error: "origin not allowed", code: "origin_refused" }, 403, null);
    return respond(null, 204, allowOrigin);
  }

  if (request.method !== "POST") {
    return respond({ error: "waitlist takes POST" }, 405, allowOrigin, { Allow: "POST, OPTIONS" });
  }

  if (!allowOrigin) {
    return respond({ error: "origin not allowed", code: "origin_refused" }, 403, null);
  }

  let text;
  try {
    text = await boundedText(request, MAX_WAITLIST_BODY_BYTES);
  } catch {
    return respond({ error: "the body could not be read", code: "invalid_body" }, 400, allowOrigin);
  }
  if (text === null) {
    return respond({ error: "the body is too large", code: "body_too_large" }, 413, allowOrigin);
  }

  let body;
  try {
    body = JSON.parse(text);
  } catch {
    return respond({ error: "the body is not JSON", code: "invalid_body" }, 400, allowOrigin);
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    return respond({ error: "the body must be a JSON object", code: "invalid_body" }, 400, allowOrigin);
  }

  const email = normalEmail(body.email);
  if (!isEmail(email)) {
    return respond({ error: "enter a valid email address", code: "invalid_email" }, 400, allowOrigin);
  }

  const noteResult = normalizeNote(body.note);
  if (noteResult.refused) {
    return respond({ error: noteResult.refused, code: noteResult.code }, 400, allowOrigin);
  }

  const language = normalizeLanguage(body.language);
  const token = body["cf-turnstile-response"];
  if (typeof token !== "string" || token.length === 0 || token.length > TURNSTILE_MAX_TOKEN_CHARS) {
    return respond({ error: "verification is required", code: "missing_verification" }, 400, allowOrigin);
  }

  const clientIp = (request.headers.get("CF-Connecting-IP") ?? "").trim();
  const ipHash = clientIp ? await tokenHash(clientIp) : "unknown";
  const rate = await deps.recordAttempt(ipHash);
  if (rate?.refused) {
    return respond({ error: "too many requests", code: "rate_limited" }, 429, allowOrigin);
  }

  const verified = await verifyTurnstile({
    token,
    secret,
    remoteip: clientIp || undefined,
    expectedHostnames: allowed.hostnames,
    fetchFn: deps.fetchTurnstile ?? fetch,
  });
  if (!verified) {
    return respond({ error: "verification failed", code: "verification_failed" }, 403, allowOrigin);
  }

  const saved = await deps.join({ email, note: noteResult.note, language });
  if (saved?.refused) {
    return respond({ error: saved.refused, code: saved.code ?? "waitlist_full" }, saved.status ?? 503, allowOrigin);
  }

  return respond({ ok: true }, 200, allowOrigin);
}
