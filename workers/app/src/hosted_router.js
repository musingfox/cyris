// The hosted Worker's doors: the operator's routes and each tenant's site. Every request is
// checked for a Cloudflare Access JWT first, by router.js's own check (ADR-0015), so a request
// without one learns nothing, not even which tenants exist. Until tenant sessions land, the
// sites too are served to whoever Access lets in, the operator. No workerd-only imports:
// vitest runs this under Node.
//
//   POST /hosted/tenants/<tenant>/register[?status=S]       register a tenant, or set its status
//   POST /hosted/tenants/<tenant>/schedule?times=T&timezone=Z[&daily_run_cap=N]
//                                                           the tenant's digest hours and run cap
//   POST /hosted/tenants/<tenant>/seed                      the schema, settings and sources
//   POST /hosted/tenants/<tenant>/run?period=P[&date=D]     queue one run of the tenant's digest
//   POST /hosted/tenants/<tenant>/poll                      one poll of the tenant's RSS buffer
//   GET  /hosted/tenants/<tenant>/report                    what the tenant's database holds
//   POST /hosted/tenants/<tenant>/query                     one {sql, params} on its database
//   GET  /t/<tenant>/...                                    the tenant's pages (hosted_pages.js)

import { SITE_ROUTE } from "./hosted_pages.js";
import { RUN_PERIODS, accessRefusal, dayIn, refuseRunDate } from "./router.js";
import { ACTIVE } from "./tenant_status.js";

const ROUTE = /^\/hosted\/tenants\/([^/]+)\/(register|schedule|seed|run|poll|report|query)$/;
const METHOD = { register: "POST", schedule: "POST", seed: "POST", run: "POST", poll: "POST", report: "GET", query: "POST" };

// What a run the registry would not queue is answered with: an issue already queued or running,
// or a tenant no longer active, is a conflict; a tenant at its daily cap is told to wait.
const REFUSED_RUN = { live: 409, inactive: 409, cap: 429 };

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

// `deps`: accessKeys, as router.js takes them; and per tenant, `register(tenant, status)`,
// which answers `{tenant}` or `{refused}`, `tenant(tenant)`, its registry row or null,
// `schedule(tenant, { times, timeZone, dailyRunCap })`, which answers `{tenant, settings}` or
// `{refused}`, `seed(tenant)`, `report(tenant)`, `queueRun(tenant, { period, date, origin })`,
// which answers `{queued}` or `{refused, code}`, `poll(tenant)`, which answers what the poll did
// or `{error}`, `query(tenant, request)`, which answers the request's `{sql, params}` from that
// tenant's object as its run's gateway does, and `page(request, tenant, rest)`, which answers
// under /t/<tenant> from that tenant's object.
export async function handleHostedRequest(request, env, deps) {
  const refusal = await accessRefusal(request, env, deps);
  if (refusal) return json({ error: "forbidden", detail: refusal }, 403);

  const url = new URL(request.url);
  const site = SITE_ROUTE.exec(url.pathname);
  if (site) {
    const [, tenant, rest = ""] = site;
    // A paused tenant's pages stay readable; only its runs stop.
    if (!(await deps.tenant(tenant))) return json({ error: `${tenant} is not a registered tenant` }, 404);
    return deps.page(request, tenant, rest);
  }
  const match = ROUTE.exec(url.pathname);
  if (!match) {
    return json({ error: "not found", routes: "/hosted/tenants/<tenant>/{register,schedule,seed,run,poll,report,query}, /t/<tenant>/" }, 404);
  }
  const [, tenant, action] = match;
  if (request.method !== METHOD[action]) return json({ error: `${action} takes ${METHOD[action]}` }, 405);

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
    });
    return answer.refused ? json({ error: answer.refused }, 400) : json(answer);
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
