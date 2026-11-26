// The hosted Worker's doors. A tenant's site and the sign-in take a tenant session and never an
// Access JWT; /api/vote takes a page's vote token and neither of the two (hosted_vote.js,
// ADR-0039); /privacy and /terms take no credential (hosted_legal.js); every other path, the
// operator's routes above all, takes the Access JWT alone, by
// router.js's own check (ADR-0015), of an identity `CYRIS_HOSTED_OPERATORS` names (ADR-0037), and
// never reads a session. So no tenant session opens an
// operator route, whatever Access covers, and a request with neither credential learns nothing,
// not even which tenants exist. Why the two:
// docs/decisions/0033-hosted-readers-sign-in-with-a-mailed-link.md. No workerd-only imports:
// vitest runs this under Node.
//
//   GET  /hosted/spend[?month=YYYY-MM]                      every tenant's AI spend in a UTC month
//   POST /hosted/backup                                     today's backup, now (hosted_backup.js)
//   GET  /hosted/waitlist                                   hosted waitlist entries in signup order
//   POST /hosted/waitlist/remove {"email": E}               remove an address from the waitlist
//   POST /hosted/tenants/<tenant>/register[?status=S]       register a tenant, or set its status
//   POST /hosted/tenants/<tenant>/schedule?times=T&timezone=Z[&daily_run_cap=N][&max_digest_times=M]
//                                                           the tenant's digest hours, run cap and most hours
//   POST /hosted/tenants/<tenant>/seed                      the schema, settings and sources
//   POST /hosted/tenants/<tenant>/run?period=P[&date=D]     queue one run of the tenant's digest
//   POST /hosted/tenants/<tenant>/poll                      one poll of the tenant's RSS buffer
//   GET  /hosted/tenants/<tenant>/report                    what the tenant's database holds
//   POST /hosted/tenants/<tenant>/budget?usd=N              the tenant's monthly AI allowance
//   POST /hosted/tenants/<tenant>/query                     one {sql, params} on its database
//   GET  /hosted/tenants/<tenant>/address                   the tenant's newsletter address
//   POST /hosted/tenants/<tenant>/rotate-address            a new address; every old one revoked
//   POST /hosted/tenants/<tenant>/revoke-address            every address revoked, none issued
//   POST /hosted/tenants/<tenant>/newsletter-source?name=N&from=A[&homepage=H][&lane=L]
//                                                           a newsletter source for its mail
//   GET  /hosted/tenants/<tenant>/page                      the page /t/<tenant>/ serves, for the operator
//   POST /hosted/tenants/<tenant>/invite   {"email": E}     put E on the invite list and mail it a link
//   POST /hosted/tenants/<tenant>/revoke   {"email": E}     take E off the list, ending its sessions
//   GET  /hosted/tenants/<tenant>/members                   the tenant's invite list
//   POST /hosted/tenants/<new>/restore?date=D&from=T        T's export of day D into the new id <new>
//   GET  /t/<tenant>/...                                    the tenant's pages (hosted_pages.js), to its session
//   GET  /t/<tenant>/settings                               the settings page every edition shares, to its session
//   /api/settings, /api/settings/values, /api/{sources,topics}, /static/...
//                                                           that page's JSON and files, to a session, for its own tenant
//                                                           (hosted_shared_settings.js, ADR-0043)
//   /login, /login/confirm, /logout                         the sign-in (hosted_login.js)
//   GET  /privacy, /terms                                   the privacy policy and terms, to anyone (hosted_legal.js)
//   POST /waitlist                                          join the hosted waitlist (hosted_waitlist.js)
//   POST /api/vote   {url, vote}                            a reader's vote, by a vote token (hosted_vote.js)

import { LEGAL_PATHS, handleLegal } from "./hosted_legal.js";
import { LOGIN_PATHS, handleLogin } from "./hosted_login.js";
import { admitOperator, unsetOperators } from "./hosted_operators.js";
import { PRIVATE_HEADERS, SITE_ROUTE, VOTE_PATH, privateHeaders, siteHeaders } from "./hosted_pages.js";
import { SHARED_PAGE, isSharedPath } from "./hosted_shared_settings.js";
import { handleVote } from "./hosted_vote.js";
import { WAITLIST_PATH, handleWaitlist } from "./hosted_waitlist.js";
import { clearedSessionCookie, crossSite, sessionToken, tokenHash } from "./hosted_session.js";
import { MONTH } from "./ai_budget.js";
import { RUN_PERIODS, accessRefusal, dayIn, refuseRunDate } from "./router.js";
import { ACTIVE } from "./tenant_status.js";

const ACTIONS = [
  "register",
  "schedule",
  "seed",
  "run",
  "poll",
  "report",
  "query",
  "page",
  "invite",
  "revoke",
  "members",
  "address",
  "rotate-address",
  "revoke-address",
  "newsletter-source",
  "budget",
  "restore",
];
const ROUTE = new RegExp(`^/hosted/tenants/([^/]+)/(${ACTIONS.join("|")})$`);
const METHOD = {
  register: "POST",
  schedule: "POST",
  seed: "POST",
  run: "POST",
  poll: "POST",
  report: "GET",
  query: "POST",
  page: "GET",
  invite: "POST",
  revoke: "POST",
  members: "GET",
  address: "GET",
  "rotate-address": "POST",
  "revoke-address": "POST",
  "newsletter-source": "POST",
  budget: "POST",
  restore: "POST",
};
// Every POST route changes a tenant or the registry: its status (register), its tables (seed, or
// any SQL by query, or a restored export), its runs and their spend (run, poll), its schedule,
// its AI allowance (budget), its newsletter address and sources, and who may sign in to it
// (invite, revoke); and POST /hosted/backup writes every tenant's export. So
// none takes another site's request (`crossSite`); a GET route changes nothing and is left to
// Access.

// What a run the registry would not queue is answered with: an issue already queued or running,
// or a tenant no longer active, is a conflict; a tenant at its daily cap is told to wait.
const REFUSED_RUN = { live: 409, inactive: 409, cap: 429 };

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

// The registry's live session for this request, never a cached one, or none; its tenant is the
// one the registry bound it to, never one the request names.
async function liveSession(request, deps) {
  const token = sessionToken(request);
  const sessionHash = token ? await tokenHash(token) : null;
  const session = token ? await deps.session(sessionHash) : null;
  return { token, sessionHash, session };
}

// A request under /t/<tenant>: served only to a live session whose tenant is the path's.
async function siteRequest(request, tenant, rest, deps) {
  const { token, sessionHash, session } = await liveSession(request, deps);
  if (!session) return signInFirst(request, token !== null);
  if (session.tenant_id !== tenant) return siteHeaders(json(NOT_FOUND, 404));
  // The tenant's settings page is the Worker's own (ADR-0043), not a page its run wrote; a paused
  // tenant may still change its settings. It carries this session's vote token, as a page the run
  // wrote does, so its probe shows a signed-in reader the site bar's links.
  const voteToken = () => deps.voteToken(sessionHash);
  if (rest === SHARED_PAGE) return siteHeaders(await deps.sharedSettings(request, tenant, rest, voteToken));
  // A paused tenant's pages stay readable; only its runs stop. The page carries a vote token for
  // this session, made only if the page is one that is served.
  return siteHeaders(await deps.page(request, tenant, rest, voteToken));
}

// The shared settings page's JSON and /static/ files (ADR-0043): to a live session, for the
// session's own tenant, which nothing in the request names.
async function sharedRequest(request, path, deps) {
  const { token, session } = await liveSession(request, deps);
  if (!session) return signInFirst(request, token !== null);
  return deps.sharedSettings(request, session.tenant_id, path);
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
// `schedule(tenant, { times, timeZone, dailyRunCap, maxDigestTimes })`, which answers `{tenant, settings}` or
// `{refused}`, `setBudget(tenant, usd)`, which answers `{tenant}` or `{refused}`, `spend(month)`,
// every tenant's month (tenant_registry.js `spend`), `seed(tenant)`, `report(tenant)`, `queueRun(tenant, { period, date, origin })`,
// which answers `{queued}` or `{refused, code}`, `poll(tenant)`, which answers what the poll did
// or `{error}`, `query(tenant, request)`, which answers the request's `{sql, params}` from that
// tenant's object as its run's gateway does, `inboundAddress(tenant)`, `rotateAddress(tenant)`
// and `revokeAddress(tenant)`, the registry's answers, `addNewsletterSource(tenant, params)`,
// which answers `{source}` or `{refused, status}`, `page(request, tenant, rest, voteToken)`, which
// answers under /t/<tenant> from that tenant's object, `invite(tenant, email)` and
// `revoke(tenant, email)`, which answer the registry's `{member}`, `{revoked}` or
// `{refused, status}`, `members(tenant)`, and `signIn(email, {invitation})` (hosted_login.js's
// createSignIn); `restore(tenant, {date, from})`, which answers what hosted_backup.js's
// restoreExport does, and `backup()`, today's backup's summary. For readers:
// `session(sessionHash)`, the registry's live session or null,
// `sharedSettings(request, tenant, path, voteToken)`, which answers the shared settings page, its
// JSON and its files (hosted_shared_settings.js), the page with the token `voteToken()` answers,
// `voteToken(sessionHash)`, the token that session's pages vote with, `voteSession(token)`, the
// live session a vote token names or null, `recordVote(tenant, {url, vote})`, which queues the
// vote in that tenant's object and answers `{queued}` or `{refused, status}`, and `login`, what
// handleLogin takes.
export async function handleHostedRequest(request, env, deps) {
  // Every answer, a refusal or an operator's JSON as much as a page, is kept out of any index.
  return privateHeaders(await route(request, env, deps));
}

async function route(request, env, deps) {
  const url = new URL(request.url);
  const site = SITE_ROUTE.exec(url.pathname);
  if (site) return siteRequest(request, site[1], site[2] ?? "", deps);
  if (isSharedPath(url.pathname)) return sharedRequest(request, url.pathname, deps);
  if (LOGIN_PATHS.has(url.pathname)) return handleLogin(request, deps.login);
  // The privacy policy and terms take no credential at all: a reader reads them before signing in.
  if (LEGAL_PATHS.has(url.pathname)) return handleLegal(request);
  // The waitlist endpoint accepts cross-origin signups from the marketing site after Turnstile verification.
  if (url.pathname === WAITLIST_PATH) return handleWaitlist(request, env, deps.waitlist);
  // The vote route takes a vote token, which a page served to a session carries; no Access JWT, no
  // cookie, and no operator route is reached through it.
  if (url.pathname === VOTE_PATH) return handleVote(request, deps);

  // Every other path is the operator's: the Access JWT, and only it, of an identity the operator
  // list names (hosted_operators.js). Access lets in whoever its application admits; a reader put
  // there by mistake would otherwise hold the operator's every route.
  const refusal = unsetOperators(env) ?? (await accessRefusal(request, env, deps, admitOperator(env)));
  if (refusal) return json({ error: "forbidden", detail: refusal }, 403);

  // Every tenant's AI spend in one UTC month, this one unless named (ADR-0038): the one operator
  // route that names no tenant, since the question is the whole deployment's.
  if (url.pathname === "/hosted/spend") {
    if (request.method !== "GET") return json({ error: "spend takes GET" }, 405);
    const month = url.searchParams.get("month");
    if (month !== null && !MONTH.test(month)) return json({ error: `month is YYYY-MM, not ${month}` }, 400);
    return json(await deps.spend(month ?? undefined));
  }

  // Today's backup run now, as the tick runs it at its hour, for the operator's restore drill.
  if (url.pathname === "/hosted/backup") {
    if (request.method !== "POST") return json({ error: "backup takes POST" }, 405);
    if (crossSite(request)) return json({ error: "backup is posted from this site or with curl, not from another site" }, 403);
    return json(await deps.backup());
  }

  if (url.pathname === "/hosted/waitlist") {
    if (request.method !== "GET") return json({ error: "waitlist takes GET" }, 405);
    return json(await deps.waitlistEntries());
  }

  if (url.pathname === "/hosted/waitlist/remove") {
    if (request.method !== "POST") return json({ error: "waitlist/remove takes POST" }, 405);
    if (crossSite(request)) {
      return json({ error: "waitlist/remove is posted from this site or with curl, not from another site" }, 403);
    }
    const answer = await deps.removeWaitlist(await bodyEmail(request));
    return answer.refused ? json({ error: answer.refused }, answer.status) : json(answer);
  }

  const match = ROUTE.exec(url.pathname);
  if (!match) {
    return json({ error: "not found", routes: `/hosted/tenants/<tenant>/{${ACTIONS.join(",")}}` }, 404);
  }
  const [, tenant, action] = match;
  if (request.method !== METHOD[action]) return json({ error: `${action} takes ${METHOD[action]}` }, 405);
  // The Access cookie is SameSite=None, so a page on any site can post to the operator's routes
  // with the operator's sign-in, a text/plain body included. So no POST route takes another
  // site's request, before any tenant is looked up or any body read; curl, hosted.sh's, sends none
  // of the headers that would mark one.
  if (METHOD[action] === "POST" && crossSite(request)) {
    return json({ error: `${action} is posted from this site or with curl, not from another site` }, 403);
  }

  // The registry is the tenant list: a tenant is registered here first, and every other
  // route is refused for one it does not hold. Only an active tenant is started.
  if (action === "register") {
    const answer = await deps.register(tenant, url.searchParams.get("status") ?? undefined);
    return answer.refused ? json({ error: answer.refused }, 400) : json(answer.tenant);
  }
  // A restore fills a tenant id the registry does not hold yet (hosted_backup.js), so it comes
  // before the registry check; registering the id is the operator's next step.
  if (action === "restore") {
    const params = { date: url.searchParams.get("date"), from: url.searchParams.get("from") };
    const answer = await deps.restore(tenant, params);
    return answer.refused ? json({ error: answer.refused }, answer.status) : json(answer);
  }
  const registered = await deps.tenant(tenant);
  if (!registered) return json({ error: `${tenant} is not a registered tenant` }, 404);

  if (action === "seed") return json(await deps.seed(tenant));
  if (action === "report") return json(await deps.report(tenant));
  // The operator's way into one tenant's database from outside its run (`cyris --tenant`,
  // ADR-0036): the Access check above is the operator's credential, and the registry check is
  // what keeps a mistyped tenant from creating an object. A paused tenant is still read and
  // written, as its report and seed are; only its runs stop.
  if (action === "query") return deps.query(tenant, request);
  // The registry is where a tenant's schedule lives, checked there; a paused tenant may get one.
  if (action === "schedule") {
    const times = url.searchParams.get("times");
    const answer = await deps.schedule(tenant, {
      times: times === null ? null : times.split(","),
      timeZone: url.searchParams.get("timezone") ?? undefined,
      dailyRunCap: url.searchParams.get("daily_run_cap") ?? undefined,
      maxDigestTimes: url.searchParams.get("max_digest_times") ?? undefined,
    });
    return answer.refused ? json({ error: answer.refused }, 400) : json(answer);
  }
  // A paused tenant may be given an allowance, which its next run after resuming is held to.
  if (action === "budget") {
    const answer = await deps.setBudget(tenant, url.searchParams.get("usd"));
    return answer.refused ? json({ error: answer.refused }, 400) : json(answer.tenant);
  }
  // A tenant's newsletter address, paused or not, since a paused tenant still takes mail. Only
  // the operator reads it here, behind Access; no run container can reach the registry.
  if (action === "address") {
    const held = await deps.inboundAddress(tenant);
    return held ? json({ tenant, ...held }) : json({ error: `${tenant} has no newsletter address` }, 404);
  }
  if (action === "rotate-address") return json({ tenant, ...(await deps.rotateAddress(tenant)) });
  if (action === "revoke-address") return json({ tenant, ...(await deps.revokeAddress(tenant)) });
  if (action === "newsletter-source") {
    const answer = await deps.addNewsletterSource(tenant, Object.fromEntries(url.searchParams));
    return answer.refused ? json({ error: answer.refused }, answer.status) : json({ tenant, ...answer });
  }
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
    // and short-lived. Mailed now, so the operator sees whether it went, and held by no cap on
    // readers' asking, so inviting again always rescues a reader a stranger has held.
    const mail = await deps.signIn(answer.member.email, { invitation: true });
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
  // today or yesterday in the tenant's time zone, the registry's, as POST /run takes one. With
  // none named it is today there, named now: the run waits in the queue, and may start after
  // midnight. A tenant with no schedule has no zone to date its issue in.
  const period = url.searchParams.get("period");
  const date = url.searchParams.get("date");
  if (!RUN_PERIODS.includes(period)) {
    return json({ error: `period must be one of ${RUN_PERIODS.join(", ")}` }, 400);
  }
  const timeZone = registered.timezone;
  if (!timeZone) {
    return json({ error: `${tenant} has no schedule in the registry; POST /hosted/tenants/${tenant}/schedule first` }, 409);
  }
  if (date !== null) {
    const refused = await refuseRunDate(date, { runTimeZone: () => timeZone, now: deps.now });
    if (refused) return json({ error: refused[1] }, refused[0]);
  }
  const issue = { period, date: date ?? dayIn(timeZone, deps.now ? deps.now() : new Date()), origin: "operator" };
  const answer = await deps.queueRun(tenant, issue);
  if (answer.refused) return json({ error: answer.refused, run: answer.run }, REFUSED_RUN[answer.code] ?? 409);
  return json(answer, 202);
}
