// A hosted reader's sign-in: the single-use link a mail carries, the session cookie it turns into,
// and the rules both keep. Why a mailed link and not Cloudflare Access, and why the cookie is
// shaped as it is: docs/decisions/0033-hosted-readers-sign-in-with-a-mailed-link.md.
// No workerd-only imports: vitest runs this under Node.
//
// No token is stored: the registry (tenant_registry.js) keeps each one's SHA-256 and nothing
// else, so its rows open no session. No token is logged either, and no URL carries one: the
// link's token rides in the fragment, which a browser never sends.

// `__Host-`: the browser takes it only with Secure, Path=/ and no Domain, so it is this host's
// alone and no other host under workers.dev can set or read it.
export const SESSION_COOKIE = "__Host-reader_session";

// How long a sign-in link opens a session, and the least time between two links a reader's asks
// get for one address, so a stranger cannot fill a listed inbox by asking again and again.
export const SIGN_IN_TTL_MS = 15 * 60 * 1000;
export const SIGN_IN_GAP_MS = 60 * 1000;
// The most sign-in links readers may ask for at /login: one address in any hour, one tenant's
// addresses together in any hour, and every tenant's in any 24 hours. Past one, no link is issued
// and the answer to /login is the one it always is. The operator's invitation counts against none
// and is held by none, so the operator can always invite, or re-invite a reader a stranger has
// held. A session lasts 14 days, so a reader needs one link a fortnight: three an hour covers a
// second device and a mistyped confirm, and a stranger asking in a reader's name holds that
// reader until an hour after the asking stops. Ten an hour per tenant is far above a beta
// tenant's readers, and a flood of one tenant holds no other: its 240 a day at most are under
// half the deployment's ceiling. A flood of three tenants, about 12 known addresses, reaches the
// ceiling in about 17 hours and holds every tenant (ADR-0033). The ceiling, 500 a day, is a cost
// backstop only, a hundred times the beta's few links a day: held for a month it is 15,000
// mails, about five dollars past Workers Paid's included 3,000.
export const SIGN_IN_ADDRESS_CAP = 3;
export const SIGN_IN_TENANT_CAP = 10;
export const SIGN_IN_DAILY_CEILING = 500;
export const SIGN_IN_HOUR_MS = 60 * 60 * 1000;
export const SIGN_IN_DAY_MS = 24 * 60 * 60 * 1000;
// How long a session lasts from the sign-in that made it; it is not extended by use.
export const SESSION_TTL_MS = 14 * 24 * 60 * 60 * 1000;
// How long a page's vote token lasts from the page view that made it, within its session's own
// lifetime (ADR-0039). The token sits in the page, where a script injected into it could read it
// and send it away; this bounds how long a stolen token votes from somewhere else. Twelve hours
// covers an issue read the day it arrives, since the default schedule runs twice a day, and a
// reload makes a fresh one.
export const VOTE_TOKEN_TTL_MS = 12 * 60 * 60 * 1000;

// 32 random bytes, base64url without padding: 43 characters.
const TOKEN = /^[A-Za-z0-9_-]{43}$/;

export function newToken() {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return btoa(String.fromCharCode(...bytes)).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

export const isToken = (value) => typeof value === "string" && TOKEN.test(value);

// What the registry keys a link or a session by: the token's SHA-256, as hex.
export async function tokenHash(token) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(token));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

// The session token the request's cookie carries, or null for none or one of the wrong shape.
export function sessionToken(request) {
  for (const part of (request.headers.get("Cookie") ?? "").split(";")) {
    const [name, ...value] = part.trim().split("=");
    if (name === SESSION_COOKIE) return isToken(value.join("=")) ? value.join("=") : null;
  }
  return null;
}

// Lax, not Strict: a reader opens the digest from a link in a mail, and a tenant page is served
// sandboxed (`SITE_POLICY`, hosted_pages.js), so each click from one page to the next leaves an opaque
// origin. A Strict cookie is sent on neither, and the reader would be asked to sign in again.
const ATTRIBUTES = "Path=/; Secure; HttpOnly; SameSite=Lax";

export const sessionCookie = (token) => `${SESSION_COOKIE}=${token}; Max-Age=${SESSION_TTL_MS / 1000}; ${ATTRIBUTES}`;

export const clearedSessionCookie = () => `${SESSION_COOKIE}=; Max-Age=0; ${ATTRIBUTES}`;

// An address as the registry keys it: trimmed and lower-cased, so one mailbox is one row.
export const normalEmail = (raw) => (typeof raw === "string" ? raw.trim().toLowerCase() : "");

// The shape of an address, and RFC 5321's 254 characters: enough to refuse a typo, not a check
// that the mailbox exists.
const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
export const isEmail = (email) => email.length <= 254 && EMAIL.test(email);

// Whether a browser sent this request from another site: an `Origin` that is not this one, or a
// `Sec-Fetch-Site` that says so. A request with neither, such as curl, is no browser's, so no
// page can have forged it. As the local server's check (docs/architecture.md §6).
export function crossSite(request) {
  const origin = request.headers.get("Origin");
  if (origin !== null && origin !== new URL(request.url).origin) return true;
  return ["cross-site", "same-site"].includes(request.headers.get("Sec-Fetch-Site"));
}
