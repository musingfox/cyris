import { describe, it, expect } from "vitest";
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
