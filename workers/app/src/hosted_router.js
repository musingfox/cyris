// The hosted Worker's doors: the operator's routes and nothing else. Every request is checked
// for a Cloudflare Access JWT first, by router.js's own check (ADR-0015), so a request
// without one learns nothing, not even which tenants exist. A tenant is served no page yet:
// per-tenant serving is a later card. No workerd-only imports: vitest runs this under Node.
//
//   POST /hosted/tenants/<tenant>/seed                      the schema, settings and sources
//   POST /hosted/tenants/<tenant>/run?period=P[&date=D]     one run of the tenant's digest
//   GET  /hosted/tenants/<tenant>/report                    what the tenant's database holds

import { RUN_PERIODS, accessRefusal, refuseRunDate } from "./router.js";

const ROUTE = /^\/hosted\/tenants\/([^/]+)\/(seed|run|report)$/;
const METHOD = { seed: "POST", run: "POST", report: "GET" };

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

// `deps`: accessKeys, as router.js takes them; `tenants`, the hosted list; and per tenant,
// `seed(tenant)`, `report(tenant)`, `timeZone(tenant)` and `startRun(tenant, period, date)`.
export async function handleHostedRequest(request, env, deps) {
  const refusal = await accessRefusal(request, env, deps);
  if (refusal) return json({ error: "forbidden", detail: refusal }, 403);

  const url = new URL(request.url);
  const match = ROUTE.exec(url.pathname);
  if (!match) return json({ error: "not found", routes: "/hosted/tenants/<tenant>/{seed,run,report}" }, 404);
  const [, tenant, action] = match;
  if (request.method !== METHOD[action]) return json({ error: `${action} takes ${METHOD[action]}` }, 405);
  if (!deps.tenants.includes(tenant)) return json({ error: `${tenant} is not a hosted tenant` }, 404);

  if (action === "seed") return json(await deps.seed(tenant));
  if (action === "report") return json(await deps.report(tenant));

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
