import { describe, it, expect, vi } from "vitest";
import { Lifecycle, PORT } from "../src/container.js";

// A stand-in for ctx.container that answers like the runtime's: start() throws
// on a running container, and the port refuses until `refusals` run out.
function fakeContext({ running = false, refusals = 0 } = {}) {
  const container = {
    running,
    starts: [],
    timeouts: [],
    pings: [],
    forwarded: [],
    start(options) {
      if (this.running) throw new Error("container is already running");
      this.running = true;
      this.starts.push(options);
    },
    async setInactivityTimeout(ms) {
      this.timeouts.push(ms);
    },
    monitor: () => new Promise(() => {}),
    getTcpPort(port) {
      return {
        fetch: async (url, init) => {
          if (url === "http://ping") {
            container.pings.push(port);
            if (refusals-- > 0) throw new Error("connection refused");
            return new Response("not found", { status: 404 });
          }
          container.forwarded.push({ port, url, init });
          return new Response("ok");
        },
      };
    },
  };
  return { container, waited: [], waitUntil(promise) { this.waited.push(promise); } };
}

const ENV = { CYRIS_ROLE: "run" };

describe("start", () => {
  it("starts a stopped container with internet access and sets its inactivity timeout", async () => {
    const ctx = fakeContext();
    expect(await new Lifecycle(ctx, 900_000).start(ENV)).toBe(true);
    expect(ctx.container.starts).toEqual([{ env: ENV, enableInternet: true }]);
    expect(ctx.container.timeouts).toEqual([900_000]);
    expect(ctx.waited).toHaveLength(1);
  });

  it("leaves a running container alone instead of throwing", async () => {
    const ctx = fakeContext({ running: true });
    expect(await new Lifecycle(ctx, 900_000).start(ENV)).toBe(false);
    expect(ctx.container.starts).toEqual([]);
  });

  it("starts once when two triggers arrive together", async () => {
    const ctx = fakeContext();
    const lifecycle = new Lifecycle(ctx, 900_000);
    await Promise.all([lifecycle.start(ENV), lifecycle.start(ENV)]);
    expect(ctx.container.starts).toHaveLength(1);
  });

  it("starts again once the container has exited", async () => {
    const ctx = fakeContext();
    const lifecycle = new Lifecycle(ctx, 900_000);
    await lifecycle.start(ENV);
    ctx.container.running = false;
    expect(await lifecycle.start(ENV)).toBe(true);
    expect(ctx.container.starts).toHaveLength(2);
  });
});

describe("adopt", () => {
  it("sets the timeout and watches a container a restarted object finds running", async () => {
    const ctx = fakeContext({ running: true });
    await new Lifecycle(ctx, 300_000).adopt();
    expect(ctx.container.timeouts).toEqual([300_000]);
    expect(ctx.waited).toHaveLength(1);
  });

  it("tells onStop how the container stopped: exit 0, or the exit code and reason", async () => {
    vi.spyOn(console, "log").mockImplementation(() => {});
    const stops = [];
    for (const monitor of [
      () => Promise.resolve(),
      () => Promise.reject(Object.assign(new Error("exited"), { exitCode: 3 })),
      () => Promise.reject(new Error("There is no container instance that can be provided to this Durable Object")),
    ]) {
      const ctx = fakeContext({ running: true });
      ctx.container.monitor = monitor;
      await new Lifecycle(ctx, 300_000, { onStop: async (stopped) => stops.push(stopped) }).adopt();
      await Promise.all(ctx.waited);
    }

    expect(stops).toEqual([
      { exitCode: 0 },
      { exitCode: 3, reason: "Error: exited" },
      {
        exitCode: undefined,
        reason: "Error: There is no container instance that can be provided to this Durable Object",
      },
    ]);
    vi.restoreAllMocks();
  });

  it("logs a stop report that fails, and stops listening all the same", async () => {
    const logs = [];
    vi.spyOn(console, "log").mockImplementation((...args) => logs.push(args));
    const ctx = fakeContext({ running: true });
    ctx.container.monitor = () => Promise.resolve();
    const lifecycle = new Lifecycle(ctx, 300_000, {
      onStop: async () => {
        throw new Error("registry unreachable");
      },
    });
    lifecycle.listening = true;

    await lifecycle.adopt();
    await expect(Promise.all(ctx.waited)).resolves.toBeDefined();

    expect(lifecycle.listening).toBe(false);
    expect(logs).toContainEqual(["container stop not reported", { reason: "registry unreachable" }]);
    vi.restoreAllMocks();
  });
});

describe("ready", () => {
  const timing = { deadlineMs: 50, pollMs: 1 };

  it("waits for the port to answer after a cold start", async () => {
    const ctx = fakeContext({ refusals: 3 });
    await new Lifecycle(ctx, 300_000, timing).ready(ENV);
    expect(ctx.container.pings).toEqual([PORT, PORT, PORT, PORT]);
  });

  it("gives up once the deadline passes", async () => {
    const ctx = fakeContext({ refusals: Infinity });
    await expect(new Lifecycle(ctx, 300_000, timing).ready(ENV)).rejects.toThrow("connection refused");
  });

  it("pings a container it found running once, then trusts it", async () => {
    const ctx = fakeContext({ running: true });
    const lifecycle = new Lifecycle(ctx, 300_000, timing);
    await lifecycle.ready(ENV);
    await lifecycle.ready(ENV);
    expect(ctx.container.starts).toEqual([]);
    expect(ctx.container.pings).toEqual([PORT]);
  });

  it("keeps a slow boot waiting after an earlier request gave up on it", async () => {
    const ctx = fakeContext({ refusals: 200 });
    const lifecycle = new Lifecycle(ctx, 300_000, timing);
    await expect(lifecycle.ready(ENV)).rejects.toThrow("connection refused");
    const pings = ctx.container.pings.length;
    await expect(lifecycle.ready(ENV)).rejects.toThrow("connection refused");
    expect(ctx.container.pings.length).toBeGreaterThan(pings);
  });

  it("starts again when the container stops before its port answers", async () => {
    const ctx = fakeContext({ refusals: 3 });
    const port = ctx.container.getTcpPort(PORT);
    const refuse = port.fetch;
    let crashed = false;
    ctx.container.getTcpPort = () => ({
      fetch: async (url, init) => {
        if (!crashed) {
          crashed = true;
          ctx.container.running = false;
        }
        return refuse(url, init);
      },
    });
    await new Lifecycle(ctx, 300_000, timing).ready(ENV);
    expect(ctx.container.starts).toHaveLength(2);
  });

  it("holds concurrent requests on one cold start", async () => {
    const ctx = fakeContext({ refusals: 2 });
    const lifecycle = new Lifecycle(ctx, 300_000, timing);
    await Promise.all([lifecycle.ready(ENV), lifecycle.ready(ENV)]);
    expect(ctx.container.starts).toHaveLength(1);
    expect(ctx.container.pings).toHaveLength(3);
  });
});

describe("forward", () => {
  it("sends the request to the container's port over plain http", async () => {
    const ctx = fakeContext({ running: true });
    const request = new Request("https://cyris.example/settings");
    await new Lifecycle(ctx, 300_000).forward(request);
    expect(ctx.container.forwarded).toEqual([{ port: PORT, url: "http://cyris.example/settings", init: request }]);
  });

  it("answers 502 when the container drops the request", async () => {
    const ctx = fakeContext({ running: true });
    ctx.container.getTcpPort = () => ({
      fetch: async () => {
        throw new Error("connection reset");
      },
    });
    const response = await new Lifecycle(ctx, 300_000).forward(new Request("https://cyris.example/settings"));
    expect(response.status).toBe(502);
  });
});

describe("a start with interception and a retry (the hosted run)", () => {
  const NO_INSTANCE_ERROR =
    "There is no container instance that can be provided to this Durable Object, try again later";
  const RETRY = { maxWaitMs: 90_000, backoffMs: [1_000, 2_000, 4_000, 8_000, 10_000] };

  // The fake container refuses `refusals` starts in turn, each as start() itself (`"start"`) or
  // as the port's first answer (`"port"`), then grants one; `order` logs what was called.
  function held(refusals = []) {
    const ctx = fakeContext();
    const order = [];
    const container = ctx.container;
    const start = container.start.bind(container);
    let pending = null;
    container.start = (options) => {
      order.push("start");
      const refusal = refusals.shift();
      if (refusal === "start") throw new Error(NO_INSTANCE_ERROR);
      start(options);
      pending = refusal ?? null;
    };
    container.destroy = async () => {
      order.push("destroy");
      container.running = false;
    };
    container.getTcpPort = () => ({
      fetch: async () => {
        if (pending === "port") throw new Error(`Network connection lost. ${NO_INSTANCE_ERROR}`);
        throw new Error("connection refused: the run listens on no port");
      },
    });
    return { ctx, order };
  }

  // A clock that only the backoff moves, so 90 s pass without waiting them.
  function clocked(ctx, order, intercept = async () => order.push("intercept")) {
    let clock = 0;
    const slept = [];
    const lifecycle = new Lifecycle(ctx, 900_000, {
      intercept,
      startRetry: RETRY,
      now: () => clock,
      sleep: async (ms) => {
        slept.push(ms);
        clock += ms;
      },
    });
    return { lifecycle, slept };
  }

  it("registers its interception before start(), and again before every start", async () => {
    const { ctx, order } = held(["start"]);
    const { lifecycle } = clocked(ctx, order);

    expect(await lifecycle.start(ENV)).toBe(true);
    expect(order).toEqual(["intercept", "start", "intercept", "start"]);
    expect(ctx.container.starts).toEqual([{ env: ENV, enableInternet: true }]);
  });

  it("does not start a container whose interception could not be registered", async () => {
    const { ctx, order } = held();
    const { lifecycle } = clocked(ctx, order, async () => {
      throw new Error("interceptOutboundHttp failed");
    });

    await expect(lifecycle.start(ENV)).rejects.toThrow("interceptOutboundHttp failed");
    expect(order).toEqual([]);
  });

  it("starts again after each backoff while no instance can be provided", async () => {
    const { ctx, order } = held(["start", "port", "start"]);
    const { lifecycle, slept } = clocked(ctx, order);

    expect(await lifecycle.start(ENV)).toBe(true);
    expect(slept).toEqual([1_000, 2_000, 4_000]);
    // The start the port refused had left the container running; it is destroyed before the next.
    const starts = order.filter((step) => step !== "intercept");
    expect(starts).toEqual(["start", "start", "destroy", "start", "start"]);
    expect(ctx.container.timeouts).toEqual([900_000]);
  });

  it("gives up once the next wait would pass 90 seconds", async () => {
    const { ctx, order } = held(Array(50).fill("start"));
    const { lifecycle, slept } = clocked(ctx, order);

    await expect(lifecycle.start(ENV)).rejects.toThrow(NO_INSTANCE_ERROR);
    // 15 s of rising backoff, then 10 s at a time: 85 s, since one more wait would end at 95.
    expect(slept).toEqual([1_000, 2_000, 4_000, 8_000, ...Array(7).fill(10_000)]);
    expect(slept.reduce((sum, ms) => sum + ms, 0)).toBeLessThanOrEqual(RETRY.maxWaitMs);
  });

  it("throws any other failure at once", async () => {
    const { ctx, order } = held();
    ctx.container.start = () => {
      throw new Error("image not found");
    };
    const { lifecycle, slept } = clocked(ctx, order);

    await expect(lifecycle.start(ENV)).rejects.toThrow("image not found");
    expect(slept).toEqual([]);
  });

  it("leaves a start without them as it was: one start(), and no retry", async () => {
    const ctx = fakeContext();
    let starts = 0;
    ctx.container.start = () => {
      starts++;
      throw new Error(NO_INSTANCE_ERROR);
    };

    await expect(new Lifecycle(ctx, 900_000).start(ENV)).rejects.toThrow(NO_INSTANCE_ERROR);
    expect(starts).toBe(1);
  });
});
