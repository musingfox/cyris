// A hosted tenant's run reaches its AI allowance here, and nothing else of the registry. Its
// container asks `GET http://spend.cyris/t/<tenant>/budget` as it starts, and posts what it spent
// to `POST http://spend.cyris/t/<tenant>/spend` as it ends, failed or not; its own Durable Object
// intercepts that host before start() and hands it to SpendGateway with `props.tenant` set to its
// name, as it does `db.cyris` and `ops.cyris`. So a run reads and charges its own tenant's month
// only, and the allowance itself is the registry's, which no container can write.
// docs/decisions/0038-a-hosted-tenants-ai-spend-is-metered-in-the-registry.md.

import { WorkerEntrypoint } from "cloudflare:workers";
import { tenantRegistry } from "./tenant_registry.js";

// The host a run container asks. `SPEND_ORIGIN` in src/cyris/adapters/hosted_spend.py names the
// same host; tests/test_hosted_config.py holds the two together.
export const SPEND_HOST = "spend.cyris";

const answer = (status, body) => Response.json(body, { status });

const ROUTES = { budget: "GET", spend: "POST" };

export class SpendGateway extends WorkerEntrypoint {
  async fetch(request) {
    const parts = new URL(request.url).pathname.split("/").filter(Boolean);
    const [root, named, action] = parts;
    if (parts.length !== 3 || root !== "t" || ROUTES[action] !== request.method) {
      return answer(403, { error: "this gateway answers only GET /t/<tenant>/budget and POST /t/<tenant>/spend" });
    }
    // A gateway registered without a tenant is a wiring fault, never a gateway for every tenant.
    const tenant = this.ctx.props?.tenant;
    if (!tenant || named !== tenant) return answer(403, { error: `tenant ${named} is not this run's` });
    const registry = tenantRegistry(this.env);
    if (action === "budget") {
      const month = await registry.budget(tenant);
      return month ? answer(200, month) : answer(404, { error: `${tenant} is not a registered tenant` });
    }
    let report;
    try {
      report = await request.json();
    } catch (error) {
      return answer(400, { error: `the body is not JSON: ${error}` });
    }
    const added = await registry.addSpend(tenant, report);
    if (added.refused) {
      console.log("ai spend refused", { tenant, reason: added.refused });
      return answer(400, { error: added.refused });
    }
    // One line per run in Workers Logs, beside the registry's month, for the operator to read.
    const { month, cost_usd, budget_usd, spent } = added.spend;
    const run = { llm_usd: report.llm_usd ?? 0, embedding_usd: report.embedding_usd ?? 0, clef_usd: report.clef_usd ?? 0 };
    console.log("ai spend", { tenant, ...run, month, month_usd: cost_usd, budget_usd, spent });
    return answer(202, added.spend);
  }
}
