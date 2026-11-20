import { afterEach, describe, expect, it, vi } from "vitest";
import { OPERATOR_WEBHOOK, OPS_HOST, OperatorAlertGateway, operatorMessage } from "../src/hosted_alerts.js";

const WEBHOOK = "https://discord.com/api/webhooks/1/operator-token";

const alert = (tenant, body = { subject: "Digest run failed: 11/20 0800 UTC", body: "RuntimeError: the store is gone" }) =>
  new Request(`http://${OPS_HOST}/t/${tenant}/alert`, { method: "POST", body: JSON.stringify(body) });

// A gateway as the tenant's run object registers it, and every request it sends.
function gateway(props, env = { [OPERATOR_WEBHOOK]: WEBHOOK }, answer = () => new Response(null, { status: 204 })) {
  const sent = [];
  vi.stubGlobal("fetch", async (url, init) => {
    sent.push({ url, init, body: JSON.parse(init.body) });
    return answer();
  });
  return { gateway: new OperatorAlertGateway({ props }, env), sent };
}

afterEach(() => vi.unstubAllGlobals());

describe("OperatorAlertGateway", () => {
  it("posts its tenant's alert, raw error and all, to the operator's webhook under the tenant's id", async () => {
    const { gateway: alpha, sent } = gateway({ tenant: "alpha" });

    const response = await alpha.fetch(alert("alpha"));

    expect(response.status).toBe(202);
    expect(sent).toHaveLength(1);
    expect(sent[0].url).toBe(WEBHOOK);
    expect(sent[0].body).toEqual({
      content: "[alpha] Digest run failed: 11/20 0800 UTC\nRuntimeError: the store is gone",
      allowed_mentions: { parse: [] },
    });
  });

  it("names the tenant it was bound to, whatever the alert's own text claims", async () => {
    const { gateway: alpha, sent } = gateway({ tenant: "alpha" });

    await alpha.fetch(alert("alpha", { subject: "[bravo] Digest run failed", body: "x" }));

    expect(sent[0].body.content.startsWith("[alpha] [bravo]")).toBe(true);
  });

  it("refuses another tenant's path with 403 and sends nothing", async () => {
    const { gateway: alpha, sent } = gateway({ tenant: "alpha" });

    const response = await alpha.fetch(alert("bravo"));

    expect(response.status).toBe(403);
    expect(sent).toEqual([]);
  });

  it("refuses everything when it was registered without a tenant", async () => {
    for (const props of [undefined, {}, { tenant: "" }]) {
      const { gateway: unbound, sent } = gateway(props);
      expect((await unbound.fetch(alert("alpha"))).status).toBe(403);
      expect(sent).toEqual([]);
    }
  });

  it("answers only POST /t/<tenant>/alert", async () => {
    const { gateway: alpha, sent } = gateway({ tenant: "alpha" });
    for (const request of [
      new Request(`http://${OPS_HOST}/t/alpha/alert`),
      new Request(`http://${OPS_HOST}/t/alpha/query`, { method: "POST", body: "{}" }),
      new Request(`http://${OPS_HOST}/alert`, { method: "POST", body: "{}" }),
    ]) {
      expect((await alpha.fetch(request)).status).toBe(403);
    }
    expect(sent).toEqual([]);
  });

  it("refuses a body that is not {subject, body}", async () => {
    const { gateway: alpha, sent } = gateway({ tenant: "alpha" });
    const raw = new Request(`http://${OPS_HOST}/t/alpha/alert`, { method: "POST", body: "not json" });

    expect((await alpha.fetch(raw)).status).toBe(400);
    expect((await alpha.fetch(alert("alpha", { subject: "only a subject" }))).status).toBe(400);
    expect(sent).toEqual([]);
  });

  it("answers 503 and logs the alert's tenant and subject when no webhook is set", async () => {
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const { gateway: alpha, sent } = gateway({ tenant: "alpha" }, {});

    const response = await alpha.fetch(alert("alpha"));

    expect(response.status).toBe(503);
    expect(sent).toEqual([]);
    expect(log).toHaveBeenCalledWith(
      "operator alert not sent",
      expect.objectContaining({ tenant: "alpha", subject: "Digest run failed: 11/20 0800 UTC" }),
    );
    log.mockRestore();
  });

  it("answers 502 when the webhook refuses, and never logs the webhook's URL", async () => {
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const refused = gateway({ tenant: "alpha" }, undefined, () => new Response("no", { status: 404 }));
    expect((await refused.gateway.fetch(alert("alpha"))).status).toBe(502);

    const unreachable = gateway({ tenant: "alpha" }, undefined, () => {
      throw new Error(`fetch to ${WEBHOOK} failed`);
    });
    expect((await unreachable.gateway.fetch(alert("alpha"))).status).toBe(502);

    const logged = JSON.stringify(log.mock.calls);
    expect(logged).toContain('"tenant":"alpha"');
    expect(logged).not.toContain("operator-token");
    log.mockRestore();
  });

  it("keeps a long alert inside Discord's 2,000 characters", () => {
    expect(operatorMessage("alpha", { subject: "s", body: "x".repeat(5000) })).toHaveLength(2000);
  });
});
