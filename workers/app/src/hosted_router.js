// The hosted Worker's doors. A tenant's site and the sign-in take a tenant session and never an
// Access JWT; every other path, the operator's routes above all, takes the Access JWT alone, by
// router.js's own check (ADR-0015), and never reads a session. So no tenant session opens an
// operator route, whatever Access covers, and a request with neither credential learns nothing,
// not even which tenants exist. Why the two:
// docs/decisions/0033-hosted-readers-sign-in-with-a-mailed-link.md. No workerd-only imports:
// vitest runs this under Node.
//
//   POST /hosted/tenants/<tenant>/register[?status=S]       register a tenant, or set its status
//   POST /hosted/tenants/<tenant>/seed                      the schema, settings and sources
//   POST /hosted/tenants/<tenant>/run?period=P[&date=D]     one run of the tenant's digest
//   POST /hosted/tenants/<tenant>/poll                      one poll of the tenant's RSS buffer
//   GET  /hosted/tenants/<tenant>/report                    what the tenant's database holds
//   GET  /hosted/tenants/<tenant>/page                      the page /t/<tenant>/ serves, for the operator
//   POST /hosted/tenants/<tenant>/invite   {"email": E}     put E on the invite list and mail it a link
//   POST /hosted/tenants/<tenant>/revoke   {"email": E}     take E off the list, ending its sessions
//   GET  /hosted/tenants/<tenant>/members                   the tenant's invite list
//   GET  /t/<tenant>/...                                    the tenant's pages (hosted_pages.js), to its session
//   /login, /login/confirm, /logout                         the sign-in (hosted_login.js)

import { LOGIN_PATHS, handleLogin } from "./hosted_login.js";
import { PRIVATE_HEADERS, SITE_ROUTE, siteHeaders } from "./hosted_pages.js";
import { clearedSessionCookie, crossSite, sessionToken, tokenHash } from "./hosted_session.js";
import { RUN_PERIODS, accessRefusal, refuseRunDate } from "./router.js";
import { ACTIVE } from "./tenant_status.js";

const ROUTE = /^\/hosted\/tenants\/([^/]+)\/(register|seed|run|poll|report|page|invite|revoke|members)$/;
const METHOD = {
  register: "POST",
  seed: "POST",
  run: "POST",
  poll: "POST",
  report: "GET",
  page: "GET",
  invite: "POST",
  revoke: "POST",
  members: "GET",
};
// The routes that change who may sign in, and to which tenant.
const CHANGES_MEMBERS = new Set(["invite", "revoke"]);

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

// The one answer a session gets for a path of any tenant but its own, registered or not.
const NOT_FOUND = { error: "not found" };

// Where a request with no live session goes: to the sign-in, before any tenant is looked up, so
// it learns nothing of the path's tenant. A cookie that opened nothing is cleared.
function signInFirst(request, stale) {
  const headers = new Headers({ "Cache-Control": "no-store", ...PRIVATE_HEADERS });
  if (stale) headers.append("Set-Cookie", clearedSessionCookie());
  if (request.method === "GET" || request.method === "HEAD") {
    headers.set("Location", "/login");
    return new Response(null, { status: 302, headers });
  }
  headers.set("Content-Type", "application/json");
  return new Response(JSON.stringify({ error: "sign in first" }), { status: 401, headers });
}

// A request under /t/<tenant>: served only to a live session whose tenant is the path's. The
// session is the registry's answer for this request, never a cached one, and its tenant is the
// one the registry bound it to, never one the request names.
async function siteRequest(request, tenant, rest, deps) {
  const token = sessionToken(request);
  const session = token ? await deps.session(await tokenHash(token)) : null;
  if (!session) return signInFirst(request, token !== null);
  if (session.tenant_id !== tenant) return siteHeaders(json(NOT_FOUND, 404));
  // A paused tenant's pages stay readable; only its runs stop.
  return siteHeaders(await deps.page(request, tenant, rest));
}

// The address in an operator route's JSON body, `{"email": ...}`: in the body, so it is in no URL
// and no log line. Anything else is "", which the registry refuses.
async function bodyEmail(request) {
  try {
    const body = await request.json();
    return typeof body?.email === "string" ? body.email : "";
  } catch {
    return "";
  }
}

// `deps`: accessKeys, as router.js takes them; and per tenant, `register(tenant, status)`,
// which answers `{tenant}` or `{refused}`, `tenant(tenant)`, its registry row or null,
// `seed(tenant)`, `report(tenant)`, `timeZone(tenant)`, `startRun(tenant, period, date)`,
// `poll(tenant)`, which answers what the poll did or `{error}`, `page(request, tenant, rest)`,
// which answers under /t/<tenant> from that tenant's object, `invite(tenant, email)` and
// `revoke(tenant, email)`, which answer the registry's `{member}`, `{revoked}` or
// `{refused, status}`, `members(tenant)`, and `signIn(email)` (hosted_login.js's createSignIn).
// For readers: `session(sessionHash)`, the registry's live session or null, and `login`, what
// handleLogin takes.
export async function handleHostedRequest(request, env, deps) {
  const url = new URL(request.url);
  const site = SITE_ROUTE.exec(url.pathname);
  if (site) return siteRequest(request, site[1], site[2] ?? "", deps);
  if (LOGIN_PATHS.has(url.pathname)) return handleLogin(request, deps.login);

  // Every other path is the operator's: the Access JWT, and only it.
  const refusal = await accessRefusal(request, env, deps);
  if (refusal) return json({ error: "forbidden", detail: refusal }, 403);

  const match = ROUTE.exec(url.pathname);
  if (!match) {
    return json({ error: "not found", routes: `/hosted/tenants/<tenant>/{${Object.keys(METHOD).join(",")}}` }, 404);
  }
  const [, tenant, action] = match;
  if (request.method !== METHOD[action]) return json({ error: `${action} takes ${METHOD[action]}` }, 405);
  // The Access cookie is SameSite=None, so a page on any site can post to the operator's routes
  // with the operator's sign-in. Who may enter a tenant is the operator's alone to change, so the
  // invite list's routes take no other site's request; curl, hosted.sh's, sends none of the
  // headers that would mark one. The other POST routes are the hardening card's (M1).
  if (CHANGES_MEMBERS.has(action) && crossSite(request)) {
    return json({ error: "the invite list is changed from this site or with curl, not from another site" }, 403);
  }

  // The registry is the tenant list: a tenant is registered here first, and every other
  // route is refused for one it does not hold. Only an active tenant is started.
  if (action === "register") {
    const answer = await deps.register(tenant, url.searchParams.get("status") ?? undefined);
    return answer.refused ? json({ error: answer.refused }, 400) : json(answer.tenant);
  }
  const registered = await deps.tenant(tenant);
  if (!registered) return json({ error: `${tenant} is not a registered tenant` }, 404);

  if (action === "seed") return json(await deps.seed(tenant));
  if (action === "report") return json(await deps.report(tenant));
  // The operator reads a tenant's latest page here, with Access, sandboxed as /t/ serves it; the
  // operator holds no tenant session, so /t/ itself is never theirs.
  if (action === "page") return siteHeaders(await deps.page(request, tenant, "/"));
  if (action === "members") return json(await deps.members(tenant));
  if (action === "revoke") {
    const answer = await deps.revoke(tenant, await bodyEmail(request));
    return answer.refused ? json({ error: answer.refused }, answer.status) : json(answer.revoked);
  }
  if (action === "invite") {
    const answer = await deps.invite(tenant, await bodyEmail(request));
    if (answer.refused) return json({ error: answer.refused }, answer.status);
    // The invitation is the member's first sign-in link, the one /login would mail: single-use
    // and short-lived. Mailed now, so the operator sees whether it went.
    const mail = await deps.signIn(answer.member.email);
    return json({ ...answer.member, mail }, mail.error ? 502 : 200);
  }

  if (registered.status !== ACTIVE) return json({ error: `${tenant} is ${registered.status}, not ${ACTIVE}` }, 409);
  // The tick's poll, out of the cron, so the operator can see a tenant's buffer fill and what one
  // poll costs without waiting for the hour. A paused tenant is polled by neither.
  if (action === "poll") {
    const polled = await deps.poll(tenant);
    return json(polled, polled.error ? 502 : 200);
  }
  // A manual run names its period, so it runs whatever the hour; a date is taken only as
  // today or yesterday in the tenant's own time zone, as POST /run takes one.
  const period = url.searchParams.get("period");
  const date = url.searchParams.get("date");
  if (!RUN_PERIODS.includes(period)) {
    return json({ error: `period must be one of ${RUN_PERIODS.join(", ")}` }, 400);
  }
  if (date !== null) {
    const refused = await refuseRunDate(date, { runTimeZone: () => deps.timeZone(tenant), now: deps.now });
    if (refused) return json({ error: refused[1] }, refused[0]);
  }
  return json(await deps.startRun(tenant, period, date ?? undefined));
}
