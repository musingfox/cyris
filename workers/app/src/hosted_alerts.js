// The operator's alert channel for every hosted tenant's run. A tenant's run container posts each
// alert, raw error included, to `http://ops.cyris/t/<tenant>/alert`; its own Durable Object
// intercepts that host before start() and hands it to OperatorAlertGateway with `props.tenant`
// set to its name, as it does `db.cyris` (tenant_db.js). The Worker sends its own alerts, about a
// run that could not start or stopped without reporting, and the registry's run queue about a run
// it failed or lost, through `alertOperator` too. Either way
// the tenant is named in front of the alert and it is posted to the operator's Discord webhook, a
// secret of this Worker that no container holds.
// docs/decisions/0035-a-hosted-runs-alerts-go-to-the-operator-through-the-hosted-worker.md.

import { WorkerEntrypoint } from "cloudflare:workers";
import WORDING from "./hosted_alerts.json";
import { LOCATION_HINT } from "./tenant_db.js";

// The host a run container posts its alerts to. `OPERATOR_ORIGIN` in src/cyris/adapters/hosted.py
// names the same host; tests/test_hosted_config.py holds the two together.
export const OPS_HOST = "ops.cyris";

// The operator's Discord webhook, set with `scripts/hosted.sh secrets`.
export const OPERATOR_WEBHOOK = "CYRIS_HOSTED_OPERATOR_WEBHOOK";

// The alert kinds that end a run (`AlertKind` in src/cyris/service_layer/alerts.py). A run that
// sent one has told the operator why it ended, so its stop needs no alert of the Worker's; a
// vote-similarity warning ends nothing and is not among them.
export const ENDING_KINDS = new Set(["run_failed", "could_not_start", "fetched_nothing"]);

// Discord refuses a message's content over 2,000 characters.
const DISCORD_CONTENT_MAX = 2000;
const SUBJECT_MAX = 200;
const FENCE = "```";

const answer = (status, body) => Response.json(body, { status });

// At most `max` UTF-16 units of `text`, never splitting a character.
function cut(text, max) {
  let out = "";
  for (const ch of text) {
    if (out.length + ch.length > max) break;
    out += ch;
  }
  return out;
}

// The subject is one line of plain text: a line break would let it forge a second
// `[<tenant>]` line, and Markdown would let it draw a masked link.
const plainLine = (text) =>
  cut(
    String(text)
      .replace(/[\r\n\u2028\u2029]+/g, " ")
      .replace(/[\\*_~`|>[\]()<]/g, "\\$&"),
    SUBJECT_MAX,
  );

// What the operator reads: the tenant the Worker bound the alert to, the subject on that line, and
// the body, the raw error, in a code block, so nothing in it renders and no line of it can pass
// for another alert's first line. The tenant comes from the Worker, never from the alert's text.
export function operatorMessage(tenant, { subject, body }) {
  const head = `[${tenant}] ${plainLine(subject)}`;
  // A backtick run in the body cannot close the block early.
  const text = String(body).replaceAll("`", "`\u200b");
  const room = DISCORD_CONTENT_MAX - head.length - `\n${FENCE}\n\n${FENCE}`.length;
  return `${head}\n${FENCE}\n${cut(text, Math.max(0, room))}\n${FENCE}`;
}

// The Worker's own alert of `kind` (hosted_alerts.json words it) with `detail` as its body.
export const workerAlert = (kind, detail) => ({ subject: WORDING[kind], body: detail });

// Post one alert about `tenant` to the operator's webhook, and leave one line in the Worker's logs,
// sent or not. Never throws, so a caller's own failure stands. Answers `{ sent, status }`, the
// status the gateway answers its container with. The registry's run queue calls this for a run it
// failed or lost (tenant_registry.js), as the gateway and the run object do.
export async function alertOperator(env, tenant, alert) {
  const webhook = env[OPERATOR_WEBHOOK];
  const notSent = (reason, status) => {
    console.log("operator alert not sent", { tenant, subject: alert.subject, reason });
    return { sent: false, status };
  };
  if (!webhook) return notSent(`${OPERATOR_WEBHOOK} is not set`, 503);
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
    return notSent(String(error?.message ?? error).replaceAll(webhook, "<webhook>"), 502);
  }
  // Not retried, and a 429 is not paced: a dropped alert keeps this line (ADR-0035).
  if (!response.ok) return notSent(`HTTP ${response.status}`, 502);
  console.log("operator alert sent", { tenant, subject: alert.subject });
  return { sent: true, status: 202 };
}

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
    // Before the webhook: the run has reported why it ended whether or not the post lands, since
    // the Worker's own alert about its stop would go to the same webhook.
    if (ENDING_KINDS.has(alert.kind)) await this.#recordEnding(tenant, alert.kind);
    const { sent, status } = await alertOperator(this.env, tenant, alert);
    return sent ? answer(status, { sent }) : answer(status, { error: "the operator's alert was not sent" });
  }

  // Tells the tenant's run object that this start reported its end (TenantContainer.alertReported).
  async #recordEnding(tenant, kind) {
    try {
      await this.env.TENANT_RUN.getByName(tenant, { locationHint: LOCATION_HINT }).alertReported(kind);
    } catch (error) {
      console.log("operator alert not recorded", { tenant, kind, reason: String(error?.message ?? error) });
    }
  }
}
