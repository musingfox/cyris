// The container's lifecycle on the Durable Object's own ctx.container, kept
// apart from index.js so a test can hand it a fake container. The Container
// class from @cloudflare/containers did all of this for us; Cloudflare stops
// maintaining it after 2026-12-31.

export const PORT = 8766;
// What the Container class allowed a cold start: 8s for an instance, then 20s
// for the port, polled every 300ms with 5s per attempt.
const READY_DEADLINE_MS = 28_000;
const POLL_MS = 300;
const PING_MS = 5_000;

// What start(), or the container's port, answers while every instance the container
// application may run is held: for about 10 s after a destroy(), measured on the trial
// account on 2026-11-18 (docs/decisions/0028-one-sqlite-durable-object-per-hosted-tenant.md).
export const NO_INSTANCE = /no container instance that can be provided/i;

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export class Lifecycle {
  // `intercept`, when given, registers the container's outbound interceptions; it runs before
  // every start(), because an interception lasts as long as the container it was registered on.
  // `startRetry`, when given, is `{ maxWaitMs, backoffMs }`: a start that meets NO_INSTANCE is
  // tried again after each backoff in turn, the last repeating, until the next wait would pass
  // `maxWaitMs`. Without them a start is what it always was: one start() and no retry.
  // `logFields`, when given, goes on every line this logs: a hosted tenant's container names
  // its tenant, since every tenant's lines share one Workers Logs stream. `onAbnormalStop`, when
  // given, is awaited with monitor()'s error after a container that exited non-zero or was killed.
  constructor(
    ctx,
    inactivityTimeoutMs,
    {
      deadlineMs = READY_DEADLINE_MS,
      pollMs = POLL_MS,
      intercept = null,
      startRetry = null,
      logFields = {},
      onAbnormalStop = null,
      sleep = pause,
      now = Date.now,
    } = {},
  ) {
    this.ctx = ctx;
    this.inactivityTimeoutMs = inactivityTimeoutMs;
    this.deadlineMs = deadlineMs;
    this.pollMs = pollMs;
    this.intercept = intercept;
    this.startRetry = startRetry;
    this.logFields = logFields;
    this.onAbnormalStop = onAbnormalStop;
    this.sleep = sleep;
    this.now = now;
  }

  // A Durable Object restart, a deploy for one, drops both the timeout and the
  // monitor, so a restarted object calls this on a container it finds running.
  adopt() {
    this.ctx.waitUntil(
      this.ctx.container.monitor().then(
        () => console.log("container stopped", { ...this.logFields, exitCode: 0 }),
        (error) => {
          console.log("container stopped", { ...this.logFields, exitCode: error?.exitCode, reason: String(error) });
          return this.onAbnormalStop?.(error);
        },
      ).finally(() => {
        this.listening = false;
      }),
    );
    return this.ctx.container.setInactivityTimeout(this.inactivityTimeoutMs);
  }

  // start() throws on a running container, where the Container class skipped
  // it: an hourly tick landing on a manual run must not fail. Concurrent
  // callers share one start, so the second never sees a half-started one.
  start(env) {
    this.starting ??= this.#start(env).finally(() => {
      this.starting = null;
    });
    return this.starting;
  }

  async #start(env) {
    if (this.ctx.container.running) return false;
    const began = this.now();
    for (let attempt = 0; ; attempt++) {
      try {
        if (this.intercept) await this.intercept();
        this.ctx.container.start({ env, enableInternet: true });
        if (this.startRetry) await this.#granted();
        break;
      } catch (error) {
        const delay = this.#retryDelay(error, attempt, began);
        if (delay === null) throw error;
        console.log("no container instance yet; starting again", {
          ...this.logFields,
          attempt: attempt + 1,
          delayMs: delay,
        });
        // A start that failed can leave `running` true, and start() throws on a running container.
        if (this.ctx.container.running) await this.ctx.container.destroy();
        await this.sleep(delay);
      }
    }
    await this.adopt();
    return true;
  }

  // start() returns before an instance is granted, and a run container listens on no port, so
  // one request to the port tells the two apart: a held application answers NO_INSTANCE, a
  // granted one anything else (a refused connection, a timeout, an answer).
  async #granted() {
    try {
      const response = await this.ctx.container
        .getTcpPort(PORT)
        .fetch("http://ping", { signal: AbortSignal.timeout(PING_MS) });
      await response.body?.cancel();
    } catch (error) {
      if (NO_INSTANCE.test(String(error))) throw error;
    }
  }

  // How long to wait before the next start, or null to give up and throw `error`.
  #retryDelay(error, attempt, began) {
    if (!this.startRetry || !NO_INSTANCE.test(String(error))) return null;
    const { maxWaitMs, backoffMs } = this.startRetry;
    const delay = backoffMs[Math.min(attempt, backoffMs.length - 1)];
    return this.now() - began + delay <= maxWaitMs ? delay : null;
  }

  // start() returns before the container listens, and running says nothing
  // about the port, so a container this object has not yet heard answer is
  // polled. Any HTTP answer, a 404 included, means the server is up.
  ready(env) {
    this.booting ??= this.#boot(env).finally(() => {
      this.booting = null;
    });
    return this.booting;
  }

  async #boot(env) {
    if (this.listening && this.ctx.container.running) return;
    const deadline = Date.now() + this.deadlineMs;
    for (;;) {
      // An allocation can fail after start() returns, and only monitor() is
      // told; running turning false is how this loop hears of it.
      await this.start(env);
      try {
        const response = await this.ctx.container
          .getTcpPort(PORT)
          .fetch("http://ping", { signal: AbortSignal.timeout(PING_MS) });
        await response.body?.cancel();
        this.listening = true;
        return;
      } catch (error) {
        if (Date.now() + this.pollMs >= deadline) throw error;
      }
      await pause(this.pollMs);
    }
  }

  async forward(request) {
    try {
      return await this.ctx.container.getTcpPort(PORT).fetch(request.url.replace("https:", "http:"), request);
    } catch (error) {
      console.log("container error:", error);
      return new Response(`Container dropped the request: ${error}`, { status: 502 });
    }
  }
}
