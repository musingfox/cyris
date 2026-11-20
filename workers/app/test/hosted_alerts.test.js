import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ALERT_TIMEOUT_MS,
  OPERATOR_WEBHOOK,
  OPS_HOST,
  OperatorAlertGateway,
  alertOperator,
  operatorMessage,
  workerAlert,
} from "../src/hosted_alerts.js";

const WEBHOOK = "https://discord.com/api/webhooks/1/operator-token";

const alert = (tenant, body = { subject: "Digest run failed: 11/20 0800 UTC", body: "RuntimeError: the store is gone" }) =>
  new Request(`http://${OPS_HOST}/t/${tenant}/alert`, { method: "POST", body: JSON.stringify(body) });

// A gateway as the tenant's run object registers it, every request it sends, and every report it
// records in a tenant's run object.
function gateway(props, env = { [OPERATOR_WEBHOOK]: WEBHOOK }, answer = () => new Response(null, { status: 204 })) {
  const sent = [];
  const recorded = [];
  vi.stubGlobal("fetch", async (url, init) => {
    sent.push({ url, init, body: JSON.parse(init.body) });
    return answer();
  });
  const TENANT_RUN = { getByName: (name) => ({ alertReported: async (kind) => recorded.push({ name, kind }) }) };
  return { gateway: new OperatorAlertGateway({ props }, { TENANT_RUN, ...env }), sent, recorded };
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
      content: "[alpha] Digest run failed: 11/20 0800 UTC\n```\nRuntimeError: the store is gone\n```",
      allowed_mentions: { parse: [] },
    });
  });

  it("names the tenant it was bound to, whatever the alert's own text claims", async () => {
    const { gateway: alpha, sent } = gateway({ tenant: "alpha" });

    await alpha.fetch(alert("alpha", { subject: "[bravo] Digest run failed", body: "x" }));

    expect(sent[0].body.content.startsWith("[alpha] \\[bravo\\]")).toBe(true);
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

  it("records a run-ending alert in the tenant's run object, and a warning not", async () => {
    for (const [kind, expected] of [
      ["run_failed", [{ name: "alpha", kind: "run_failed" }]],
      ["could_not_start", [{ name: "alpha", kind: "could_not_start" }]],
      ["fetched_nothing", [{ name: "alpha", kind: "fetched_nothing" }]],
      ["vote_similarity_rejected", []],
      [undefined, []],
    ]) {
      const { gateway: alpha, recorded } = gateway({ tenant: "alpha" });
      await alpha.fetch(alert("alpha", { kind, subject: "s", body: "b" }));
      expect(recorded).toEqual(expected);
    }
  });

  it("still forwards an alert whose report could not be recorded", async () => {
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const { sent } = gateway({ tenant: "alpha" });
    const alpha = new OperatorAlertGateway({ props: { tenant: "alpha" } }, { [OPERATOR_WEBHOOK]: WEBHOOK });

    const response = await alpha.fetch(alert("alpha", { kind: "run_failed", subject: "s", body: "b" }));

    expect(response.status).toBe(202);
    expect(sent).toHaveLength(1);
    expect(log).toHaveBeenCalledWith("operator alert not recorded", expect.objectContaining({ tenant: "alpha" }));
    log.mockRestore();
  });
});

describe("operatorMessage", () => {
  it("keeps a long alert inside Discord's 2,000 characters, its code block closed", () => {
    const message = operatorMessage("alpha", { subject: "s", body: "x".repeat(5000) });

    expect(message.length).toBeLessThanOrEqual(2000);
    expect(message.endsWith("\n```")).toBe(true);
  });

  it("never splits a character at the cut", () => {
    const message = operatorMessage("alpha", { subject: "s", body: "😀".repeat(2000) });

    expect(message.endsWith("😀\n```")).toBe(true);
  });

  it("lets no subject forge a second tenant's line or draw Markdown", () => {
    const message = operatorMessage("alpha", {
      subject: "Digest run failed\r\n[bravo] Digest run failed\u2028**bold** [link](https://evil.test) `x`",
      body: "x",
    });
    const [head] = message.split("\n```\n");

    expect(head.split("\n")).toHaveLength(1);
    expect(head.startsWith("[alpha] Digest run failed ")).toBe(true);
    expect(head).toContain("\\[bravo\\]");
    expect(head).toContain("\\*\\*bold\\*\\*");
    expect(head).toContain("\\[link\\]\\(https://evil.test\\)");
    // After the Worker's own `[alpha] `, every Markdown character is escaped.
    expect(head.slice("[alpha] ".length)).not.toMatch(/(^|[^\\])[*`[]/);
  });

  it("keeps the body inside its code block, whatever backticks or lines it holds", () => {
    const body = "line\n```\n[bravo] Digest run failed\n**x**";
    const message = operatorMessage("alpha", { subject: "s", body });
    const lines = message.split("\n");

    expect(lines[0]).toBe("[alpha] s");
    expect(lines[1]).toBe("```");
    expect(lines.at(-1)).toBe("```");
    // Only the block's own two fences are fences: the body's backticks are broken up.
    expect(lines.filter((line) => line === "```")).toHaveLength(2);
    expect(message).toContain("[bravo] Digest run failed");
  });
});

describe("alertOperator", () => {
  it("is what a Worker-side caller sends with, worded by workerAlert", async () => {
    const posted = [];
    vi.stubGlobal("fetch", async (_url, init) => {
      posted.push(JSON.parse(init.body).content);
      return new Response(null, { status: 204 });
    });

    const result = await alertOperator({ [OPERATOR_WEBHOOK]: WEBHOOK }, "bravo", workerAlert("stopped", "exitCode=137"));

    expect(result).toEqual({ sent: true, status: 202 });
    expect(posted).toEqual(["[bravo] Digest run stopped before it reported\n```\nexitCode=137\n```"]);
  });

  it("never throws, with no webhook or a webhook that fails", async () => {
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    expect(await alertOperator({}, "bravo", workerAlert("start_failed", "x"))).toEqual({ sent: false, status: 503 });
    vi.stubGlobal("fetch", async () => {
      throw new Error("network down");
    });
    expect(await alertOperator({ [OPERATOR_WEBHOOK]: WEBHOOK }, "bravo", workerAlert("start_failed", "x"))).toEqual({
      sent: false,
      status: 502,
    });
    log.mockRestore();
  });

  it("gives up a webhook that does not answer within ALERT_TIMEOUT_MS, whether or not its fetch heeds the abort", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    try {
      for (const heeds of [true, false]) {
        const signals = [];
        vi.stubGlobal("fetch", (_url, init) => {
          signals.push(init.signal);
          return new Promise((_, reject) => {
            if (heeds) init.signal.addEventListener("abort", () => reject(init.signal.reason));
          });
        });
        let answer = null;
        const posting = alertOperator({ [OPERATOR_WEBHOOK]: WEBHOOK }, "bravo", workerAlert("stopped", "x")).then((a) => {
          answer = a;
        });

        await vi.advanceTimersByTimeAsync(ALERT_TIMEOUT_MS - 1);
        expect(answer, `heeds: ${heeds}`).toBeNull();
        await vi.advanceTimersByTimeAsync(1);
        await posting;

        expect(answer).toEqual({ sent: false, status: 502 });
        expect(signals[0].aborted).toBe(true);
        expect(log).toHaveBeenLastCalledWith("operator alert not sent", {
          tenant: "bravo",
          subject: "Digest run stopped before it reported",
          reason: `no answer within ${ALERT_TIMEOUT_MS} ms`,
        });
      }
    } finally {
      log.mockRestore();
      vi.useRealTimers();
    }
  });
});
