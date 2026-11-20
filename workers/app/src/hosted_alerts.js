// The operator's alert channel for every hosted tenant's run. A tenant's run container posts each
// alert, raw error included, to `http://ops.cyris/t/<tenant>/alert`; its own Durable Object
// intercepts that host before start() and hands it to OperatorAlertGateway with `props.tenant`
// set to its name, as it does `db.cyris` (tenant_db.js). The gateway names the tenant in front of
// the alert and posts it to the operator's Discord webhook, a secret of this Worker that no
// container holds. docs/decisions/0035-a-hosted-runs-alerts-go-to-the-operator-through-the-hosted-worker.md.

import { WorkerEntrypoint } from "cloudflare:workers";

// The host a run container posts its alerts to. `OPERATOR_ORIGIN` in src/cyris/adapters/hosted.py
// names the same host; tests/test_hosted_config.py holds the two together.
export const OPS_HOST = "ops.cyris";

// The operator's Discord webhook, set with `scripts/hosted.sh secrets`.
export const OPERATOR_WEBHOOK = "CYRIS_HOSTED_OPERATOR_WEBHOOK";

// Discord refuses a message's content over 2,000 characters.
const DISCORD_CONTENT_MAX = 2000;

const answer = (status, body) => Response.json(body, { status });

// What the operator reads: the tenant the Worker bound this gateway to, then the alert as the
// run worded it. The tenant comes from `props`, never from the body, so no run can speak for
// another tenant.
export const operatorMessage = (tenant, { subject, body }) =>
  `[${tenant}] ${subject}\n${body}`.slice(0, DISCORD_CONTENT_MAX);

export class OperatorAlertGateway extends WorkerEntrypoint {
  async fetch(request) {
    const parts = new URL(request.url).pathname.split("/").filter(Boolean);
    if (request.method !== "POST" || parts.length !== 3 || parts[0] !== "t" || parts[2] !== "alert") {
      return answer(403, { error: "this gateway answers only POST /t/<tenant>/alert" });
    }
    // A gateway registered without a tenant is a wiring fault, never a gateway for every tenant.
    const tenant = this.ctx.props?.tenant;
    if (!tenant || parts[1] !== tenant) return answer(403, { error: `tenant ${parts[1]} is not this run's` });
    let alert;
    try {
      alert = await request.json();
    } catch (error) {
      return answer(400, { error: `the body is not JSON: ${error}` });
    }
    if (typeof alert?.subject !== "string" || typeof alert?.body !== "string") {
      return answer(400, { error: "the body must be {subject, body}" });
    }
    // One line per alert in Workers Logs, sent or not, so a lost alert is still on record. The
    // body is the raw error, which the run's own lines already hold.
    const webhook = this.env[OPERATOR_WEBHOOK];
    if (!webhook) {
      console.log("operator alert not sent", { tenant, subject: alert.subject, reason: `${OPERATOR_WEBHOOK} is not set` });
      return answer(503, { error: `${OPERATOR_WEBHOOK} is not set; run: scripts/hosted.sh secrets` });
    }
    let response;
    try {
      response = await fetch(webhook, {
        method: "POST",
        headers: { "content-type": "application/json" },
        // No mention in an error's text may ping anyone.
        body: JSON.stringify({ content: operatorMessage(tenant, alert), allowed_mentions: { parse: [] } }),
      });
      await response.body?.cancel();
    } catch (error) {
      // The webhook's URL is its credential: no error text that could quote it is logged as is.
      const reason = String(error?.message ?? error).replaceAll(webhook, "<webhook>");
      console.log("operator alert not sent", { tenant, subject: alert.subject, reason });
      return answer(502, { error: "the operator's webhook could not be reached" });
    }
    if (!response.ok) {
      console.log("operator alert not sent", { tenant, subject: alert.subject, reason: `HTTP ${response.status}` });
      return answer(502, { error: `the operator's webhook answered ${response.status}` });
    }
    console.log("operator alert sent", { tenant, subject: alert.subject });
    return answer(202, { sent: true });
  }
}
