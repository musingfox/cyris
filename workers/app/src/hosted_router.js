// The hosted Worker's doors: the operator's routes and nothing else. Every request is checked
// for a Cloudflare Access JWT first, by router.js's own check (ADR-0015), so a request
// without one learns nothing, not even which tenants exist. A tenant is served no page yet:
// per-tenant serving is a later card. No workerd-only imports: vitest runs this under Node.
//
//   POST /hosted/tenants/<tenant>/register[?status=S]       register a tenant, or set its status
//   POST /hosted/tenants/<tenant>/seed                      the schema, settings and sources
//   POST /hosted/tenants/<tenant>/run?period=P[&date=D]     one run of the tenant's digest
//   GET  /hosted/tenants/<tenant>/report                    what the tenant's database holds

import { RUN_PERIODS, accessRefusal, refuseRunDate } from "./router.js";
import { ACTIVE } from "./tenant_status.js";

const ROUTE = /^\/hosted\/tenants\/([^/]+)\/(register|seed|run|report)$/;
const METHOD = { register: "POST", seed: "POST", run: "POST", report: "GET" };

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

// `deps`: accessKeys, as router.js takes them; and per tenant, `register(tenant, status)`,
// which answers `{tenant}` or `{refused}`, `tenant(tenant)`, its registry row or null,
// `seed(tenant)`, `report(tenant)`, `timeZone(tenant)` and `startRun(tenant, period, date)`.
export async function handleHostedRequest(request, env, deps) {
  const refusal = await accessRefusal(request, env, deps);
  if (refusal) return json({ error: "forbidden", detail: refusal }, 403);

  const url = new URL(request.url);
  const match = ROUTE.exec(url.pathname);
  if (!match) return json({ error: "not found", routes: "/hosted/tenants/<tenant>/{register,seed,run,report}" }, 404);
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

  if (registered.status !== ACTIVE) return json({ error: `${tenant} is ${registered.status}, not ${ACTIVE}` }, 409);
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
