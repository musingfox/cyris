import { describe, it, expect, vi } from "vitest";
import { env } from "./cloudflare_workers_stub.js";
import { CyrisContainer } from "../src/index.js";

function fakeContext(name) {
  const container = {
    running: false,
    starts: [],
    forwarded: [],
    start(options) {
      this.running = true;
      this.starts.push(options);
    },
    setInactivityTimeout: async () => {},
    monitor: () => new Promise(() => {}),
    getTcpPort: () => ({
      fetch: async (url, init) => {
        if (url !== "http://ping") container.forwarded.push({ url, init });
        return new Response("ok");
      },
    }),
  };
  return { container, id: { name }, waitUntil() {}, blockConcurrencyWhile: (run) => run() };
}

describe("the container's environment", () => {
  it("tells a ui instance it sits behind this Worker, and forwards the original URL", async () => {
    const ctx = fakeContext("ui");
    const request = new Request("https://cyris.example/settings");
    await new CyrisContainer(ctx, env).fetch(request);
    const [{ env: started }] = ctx.container.starts;
    expect(started.CYRIS_ROLE).toBe("ui");
    expect(started.CYRIS_BEHIND_APP_WORKER).toBe("true");
    expect(ctx.container.forwarded).toEqual([{ url: "http://cyris.example/settings", init: request }]);
  });

  it("does not depend on any binding, so a Preview without [vars] gets it too", async () => {
    expect(Object.keys(env)).toEqual([]);
    const ctx = fakeContext("ui");
    await new CyrisContainer(ctx, env).fetch(new Request("https://preview.example/settings"));
    expect(ctx.container.starts[0].env.CYRIS_BEHIND_APP_WORKER).toBe("true");
  });

  it("leaves it off a run instance, which serves nothing", async () => {
    const ctx = fakeContext("run");
    await new CyrisContainer(ctx, env).startRun({ CYRIS_ROLE: "run" });
    expect(ctx.container.starts[0].env.CYRIS_BEHIND_APP_WORKER).toBeUndefined();
  });
});

// The hosted cards gave Lifecycle hooks (onStop, onAbnormalStop, logFields) that only the hosted
// Worker passes. The default Worker passes none, so a stop logs the one line it logged before.
describe("the default Worker's container stop", () => {
  for (const [what, monitor, line] of [
    ["exits 0", () => Promise.resolve(), { exitCode: 0 }],
    [
      "exits non-zero",
      () => Promise.reject(Object.assign(new Error("exited"), { exitCode: 3 })),
      { exitCode: 3, reason: "Error: exited" },
    ],
  ]) {
    it(`logs only "container stopped" when its run ${what}, with no hook to call`, async () => {
      const log = vi.spyOn(console, "log").mockImplementation(() => {});
      const ctx = fakeContext("run");
      const waited = [];
      ctx.waitUntil = (promise) => waited.push(promise);
      ctx.container.monitor = monitor;
      const object = new CyrisContainer(ctx, env);
      object.lifecycle.listening = true;

      await object.startRun({ CYRIS_ROLE: "run" });
      await Promise.all(waited);

      expect(object.lifecycle).toMatchObject({ onStop: null, onAbnormalStop: null, logFields: {} });
      expect(log.mock.calls).toEqual([["container stopped", line]]);
      expect(object.lifecycle.listening).toBe(false);
      log.mockRestore();
    });
  }
});
