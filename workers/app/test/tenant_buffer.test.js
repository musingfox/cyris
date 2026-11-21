import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { env } from "cloudflare:workers";
import worker from "../src/hosted.js";
import { REGISTRY_NAME, tenantRegistry } from "../src/tenant_registry.js";
import POLICY from "../src/hosted_settings_policy.json" with { type: "json" };
import { HOSTED_SCHEMA_SQL, SCHEMA_SQL, drainAlarms, namespace, registryNamespace, tenantDb } from "./sql_storage.js";

// The hosted Worker's hourly tick, hosted.js itself, polling each tenant's feeds into its own
// buffer over real SQLite. Feeds are answered by a stub fetch; TENANT_RUN records each start, and
// how many buffer rows its tenant held at that moment.

const HOUR = 3600_000;
const DAY = 24 * HOUR;
const STAMP = "2026-11-18T00:00:00+00:00";
const DIGEST_HOUR = new Date("2026-11-18T08:00:00Z");

// A feed's XML: one item per `[slug, age in ms]`, dated that long before now.
function rss(feed, items) {
  const entries = items
    .map(
      ([slug, age]) =>
        `<item><title>${feed} ${slug}</title><link>https://${feed}.test/${slug}</link>` +
        `<guid>${feed}-${slug}</guid><pubDate>${new Date(Date.now() - age).toUTCString()}</pubDate>` +
        `<description>${feed} body</description></item>`,
    )
    .join("");
  return `<?xml version="1.0"?><rss version="2.0"><channel><title>${feed}</title>${entries}</channel></rss>`;
}

// `feeds` maps a feed's host name to the items it serves, or to an HTTP status it fails with.
function feedServer(feeds) {
  const fetched = [];
  const fetch = async (url) => {
    const host = new URL(url).hostname.split(".")[0];
    fetched.push(host);
    const answer = feeds[host];
    if (typeof answer === "number") return new Response("", { status: answer });
    return new Response(rss(host, answer ?? [["one", HOUR]]));
  };
  return { fetch, fetched };
}

function bindings() {
  const TENANT_DB = namespace();
  const starts = [];
  const TENANT_RUN = {
    getByName(tenant) {
      return {
        startRun: async (args) => {
          starts.push({ tenant, args, buffered: rows(TENANT_DB, tenant).length });
          return { started: true, tenant };
        },
      };
    },
  };
  const bound = { TENANT_DB, TENANT_RUN, starts };
  bound.REGISTRY = registryNamespace(bound);
  return bound;
}

// The tick, then the registry's alarm, which starts the runs the tick queued.
async function tick(bound) {
  await worker.scheduled();
  await drainAlarms(bound.REGISTRY.getByName(REGISTRY_NAME));
}

// A tenant's database as the seed leaves it: both schemas, a schedule due at 08:00 and 20:00 UTC,
// and an RSS source per feed host. `hostedSchema: false` is a tenant seeded before the buffer.
function seeded(TENANT_DB, tenant, feeds, { hostedSchema = true } = {}) {
  const db = TENANT_DB.getByName(tenant);
  db.query(SCHEMA_SQL);
  if (hostedSchema) db.query(HOSTED_SCHEMA_SQL);
  for (const [key, value] of [
    ["general.digest_schedule", ["08:00", "20:00"]],
    ["general.timezone", "UTC"],
  ]) {
    db.query("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)", [key, JSON.stringify(value), STAMP]);
  }
  for (const feed of feeds) {
    db.query("INSERT INTO sources (name, url, type, config) VALUES (?, ?, 'rss', '{}')", [
      `${feed} news`,
      `https://${feed}.test/feed`,
    ]);
  }
  return db;
}

const hasBuffer = (TENANT_DB, tenant) =>
  TENANT_DB.getByName(tenant)
    .query("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'articles'")
    .rows.length === 1;

// A tenant's buffer rows, oldest URL first; none when it has no buffer.
const rows = (TENANT_DB, tenant) =>
  hasBuffer(TENANT_DB, tenant)
    ? TENANT_DB.getByName(tenant).query("SELECT url, source_name, fetched_at FROM articles ORDER BY url").rows
    : [];

const urls = (TENANT_DB, tenant) => rows(TENANT_DB, tenant).map((row) => row.url);

describe("the hosted tick polls each tenant's feeds into its own buffer", () => {
  let bound;
  let logs;
  beforeEach(() => {
    bound = bindings();
    Object.assign(env, bound);
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(DIGEST_HOUR);
    logs = [];
    vi.spyOn(console, "log").mockImplementation((...args) => logs.push(args));
    vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.spyOn(console, "error").mockImplementation(() => {});
  });
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.useRealTimers();
    for (const key of Object.keys(env)) delete env[key];
  });

  // Registered, and scheduled at 08:00 and 20:00 UTC unless `scheduled` is false.
  const register = async (tenant, status, { scheduled = true } = {}) => {
    await tenantRegistry(env).register(tenant, { status });
    if (scheduled) await tenantRegistry(env).setSchedule(tenant, { times: ["08:00", "20:00"], timeZone: "UTC" });
  };
  const serve = (feeds) => {
    const server = feedServer(feeds);
    vi.stubGlobal("fetch", server.fetch);
    return server;
  };

  it("gives two tenants with different feeds only their own feeds' entries", async () => {
    await register("alpha");
    await register("bravo");
    seeded(bound.TENANT_DB, "alpha", ["north", "east"]);
    seeded(bound.TENANT_DB, "bravo", ["south"]);
    serve({});

    await tick(bound);

    expect(urls(bound.TENANT_DB, "alpha")).toEqual(["https://east.test/one", "https://north.test/one"]);
    expect(urls(bound.TENANT_DB, "bravo")).toEqual(["https://south.test/one"]);
    expect(rows(bound.TENANT_DB, "alpha").map((row) => row.source_name)).toEqual(["east news", "north news"]);
  });

  it("gives two tenants subscribed to one feed each its entries, fetched once per tenant", async () => {
    await register("alpha");
    await register("bravo");
    seeded(bound.TENANT_DB, "alpha", ["shared", "north"]);
    seeded(bound.TENANT_DB, "bravo", ["shared"]);
    const server = serve({});

    await tick(bound);

    expect(urls(bound.TENANT_DB, "alpha")).toEqual(["https://north.test/one", "https://shared.test/one"]);
    expect(urls(bound.TENANT_DB, "bravo")).toEqual(["https://shared.test/one"]);
    expect(server.fetched.filter((host) => host === "shared")).toHaveLength(2);
  });

  it("polls neither a paused tenant nor one the registry never heard of", async () => {
    await register("alpha");
    await register("paused", "paused");
    seeded(bound.TENANT_DB, "alpha", ["north"]);
    seeded(bound.TENANT_DB, "paused", ["quiet"]);
    seeded(bound.TENANT_DB, "stranger", ["unasked"]);
    const server = serve({});

    await tick(bound);

    expect(server.fetched).toEqual(["north"]);
    expect(rows(bound.TENANT_DB, "paused")).toEqual([]);
    expect(rows(bound.TENANT_DB, "stranger")).toEqual([]);
    expect(bound.starts.map((start) => start.tenant)).toEqual(["alpha"]);
  });

  it("leaves the other tenant's buffer filled and its run started when one tenant's poll fails", async () => {
    await register("alpha");
    await register("broken");
    await register("unseeded", undefined, { scheduled: false });
    seeded(bound.TENANT_DB, "alpha", ["north"]);
    seeded(bound.TENANT_DB, "broken", ["east"]);
    bound.TENANT_DB.getByName("broken").pollRss = async () => {
      throw new Error("the object was reset");
    };
    serve({});

    await tick(bound);

    expect(urls(bound.TENANT_DB, "alpha")).toEqual(["https://north.test/one"]);
    const failed = logs.filter(([line]) => line === "rss poll failed").map(([, detail]) => detail);
    expect(failed).toEqual(
      expect.arrayContaining([
        { tenant: "broken", error: "the object was reset" },
        { tenant: "unseeded", error: expect.stringContaining("could not read sources") },
      ]),
    );
    // A failed poll still lets its tenant's run start, on what its buffer already holds; an
    // unseeded tenant has no schedule in the registry, so it starts nothing, and no table was made for it.
    expect(bound.starts.map((start) => start.tenant).sort()).toEqual(["alpha", "broken"]);
    expect(hasBuffer(bound.TENANT_DB, "unseeded")).toBe(false);
  });

  it("records a failing feed in that tenant's feed_health, and only there", async () => {
    await register("alpha");
    await register("bravo");
    seeded(bound.TENANT_DB, "alpha", ["north", "down"]);
    seeded(bound.TENANT_DB, "bravo", ["south"]);
    serve({ down: 503 });

    await tick(bound);

    const health = (tenant) =>
      bound.TENANT_DB.getByName(tenant)
        .query("SELECT name, consecutive_failures, last_error FROM feed_health ORDER BY name").rows;
    expect(health("alpha")).toEqual([
      { name: "down news", consecutive_failures: 1, last_error: "HTTP 503" },
      { name: "north news", consecutive_failures: 0, last_error: null },
    ]);
    expect(health("bravo")).toEqual([{ name: "south news", consecutive_failures: 0, last_error: null }]);
    expect(urls(bound.TENANT_DB, "alpha")).toEqual(["https://north.test/one"]);
  });

  it("polls before it starts a run, so the run that tick starts sees the hour's entries", async () => {
    await register("alpha");
    seeded(bound.TENANT_DB, "alpha", ["north"]);
    serve({ north: [["first", HOUR], ["second", 2 * HOUR]] });

    await tick(bound);

    expect(bound.starts).toEqual([{ tenant: "alpha", args: { period: "morning", date: "2026-11-18", run: { id: 1, attempt: 1 } }, buffered: 2 }]);
    // Stamped when the poll started, which is before the run's window closes.
    expect(rows(bound.TENANT_DB, "alpha").map((row) => row.fetched_at)).toEqual([
      DIGEST_HOUR.toISOString(),
      DIGEST_HOUR.toISOString(),
    ]);
  });

  it("polls off the digest hours too, and starts nothing then", async () => {
    await register("alpha");
    seeded(bound.TENANT_DB, "alpha", ["north"]);
    serve({});
    vi.setSystemTime(new Date("2026-11-18T13:00:00Z"));

    await tick(bound);

    expect(urls(bound.TENANT_DB, "alpha")).toEqual(["https://north.test/one"]);
    expect(bound.starts).toEqual([]);
  });

  it("gives a tenant seeded before the buffer existed its table at the next tick", async () => {
    await register("alpha");
    seeded(bound.TENANT_DB, "alpha", ["north"], { hostedSchema: false });
    expect(hasBuffer(bound.TENANT_DB, "alpha")).toBe(false);
    serve({});

    await tick(bound);

    expect(urls(bound.TENANT_DB, "alpha")).toEqual(["https://north.test/one"]);
  });

  it("keeps workers/rss's rules: the retention window, INSERT OR IGNORE and the prune", async () => {
    await register("alpha");
    const db = seeded(bound.TENANT_DB, "alpha", ["north"]);
    const iso = (ms) => new Date(ms).toISOString();
    const now = DIGEST_HOUR.getTime();
    const seed = "INSERT INTO articles (url, title, content, published_at, source_name, fetched_at) VALUES (?, '', '', ?, 'north news', ?)";
    db.query(seed, ["https://north.test/stale", iso(now - DAY), iso(now - 9 * DAY)]);
    db.query(seed, ["https://north.test/late", iso(now - 10 * DAY), iso(now - DAY)]);
    serve({ north: [["new", HOUR], ["history", 9 * DAY]] });

    await tick(bound);
    vi.setSystemTime(new Date(now + HOUR));
    await tick(bound);

    // The feed's 9-day-old entry never entered; the row buffered 9 days ago was pruned; the
    // row its feed dated 10 days back but buffered yesterday stays; the new entry, seen twice,
    // is one row stamped by the first poll.
    expect(rows(bound.TENANT_DB, "alpha")).toEqual([
      { url: "https://north.test/late", source_name: "north news", fetched_at: iso(now - DAY) },
      { url: "https://north.test/new", source_name: "north news", fetched_at: iso(now) },
    ]);
    const polls = logs.filter(([line]) => line === "rss poll").map(([, detail]) => detail);
    expect(polls.map((poll) => poll.written)).toEqual([1, 0]);
  });

  it("logs one measurement line per tick: feeds, feed requests and its own object calls", async () => {
    await register("alpha");
    await register("bravo");
    seeded(bound.TENANT_DB, "alpha", ["north", "east", "west"]);
    seeded(bound.TENANT_DB, "bravo", ["south"]);
    serve({ west: 500 });

    await tick(bound);

    const ticks = logs.filter(([line]) => line === "hosted tick").map(([, detail]) => detail);
    expect(ticks).toEqual([
      {
        tenants: 2,
        polled: 2,
        failed_polls: 0,
        queued: 2,
        feeds: 4,
        feed_fetches: 4,
        max_tenant_fetches: 3,
        written: 3,
        // The registry, then per tenant its poll and its queued run; the schedule is the registry
        // row's, and a digest hour reads no digest_runs.
        object_calls: 1 + 2 * 2,
        wall_ms: expect.any(Number),
      },
    ]);
    const polls = logs.filter(([line]) => line === "rss poll").map(([, detail]) => detail);
    expect(polls).toContainEqual({ tenant: "alpha", feeds: 3, fetches: 3, entries: 2, fresh: 2, written: 2, failed_feeds: 1, skipped_feeds: 0 });
  });
});

// The poll's own hold on a tenant's feeds (tenant_buffer.js `heldPoll`, ADR-0040), on one
// tenant's object: what each feed URL answers is `routes[url]`, a feed's XML, a status, or
// `{ redirect: location }`; every request is kept with the options it was made with.
describe("a hosted tenant's poll holds each feed to the source rules", () => {
  const OWN = "cyris-hosted.example.workers.dev";
  let requests;

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(DIGEST_HOUR);
    vi.spyOn(console, "log").mockImplementation(() => {});
    vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.spyOn(console, "error").mockImplementation(() => {});
  });
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  function serve(routes) {
    requests = [];
    vi.stubGlobal("fetch", async (url, init) => {
      requests.push({ url: String(url), redirect: init?.redirect });
      const answer = routes[String(url)];
      if (answer === undefined) return new Response("", { status: 404 });
      if (typeof answer === "number") return new Response("", { status: answer });
      if (answer.redirect) return new Response(null, { status: 302, headers: { Location: answer.redirect } });
      if (answer.stream) return new Response(answer.stream);
      return new Response(answer);
    });
  }

  function tenantWith(urls) {
    const { db } = tenantDb();
    db.query(SCHEMA_SQL);
    db.query(HOSTED_SCHEMA_SQL);
    urls.forEach((url, i) =>
      db.query("INSERT INTO sources (name, url, type, config) VALUES (?, ?, 'rss', '{}')", [`feed ${String(i).padStart(2, "0")}`, url]),
    );
    return db;
  }

  const feed = (host) => rss(host, [["one", HOUR]]);

  it("follows a feed's redirects by hand, each one asked only after its URL passes", async () => {
    serve({
      "https://old.example/feed": { redirect: "https://new.example/feed" },
      "https://new.example/feed": { redirect: "/rss" },
      "https://new.example/rss": feed("new"),
    });
    const db = tenantWith(["https://old.example/feed"]);

    const polled = await db.pollRss({ ownHosts: [OWN] });

    expect(polled.failures).toEqual([]);
    expect(polled.written).toBe(1);
    expect(requests.map((r) => [r.url, r.redirect])).toEqual([
      ["https://old.example/feed", "manual"],
      ["https://new.example/feed", "manual"],
      ["https://new.example/rss", "manual"],
    ]);
  });

  it.each([
    ["the metadata address", "http://169.254.169.254/latest/meta-data"],
    ["localhost", "http://localhost:8080/admin"],
    ["the hosted Worker itself", `https://${OWN}/hosted/spend`],
    ["another workers.dev name", "https://cyris-rss.someone.workers.dev/articles"],
    ["a non-http scheme", "file:///etc/passwd"],
  ])("never asks a redirect to %s", async (_name, target) => {
    serve({ "https://feed.example/rss": { redirect: target } });
    const db = tenantWith(["https://feed.example/rss"]);

    const polled = await db.pollRss({ ownHosts: [OWN] });

    expect(polled.failures).toHaveLength(1);
    expect(requests.map((r) => r.url)).toEqual(["https://feed.example/rss"]);
  });

  it("never asks a stored URL the write rules refuse, whoever wrote the row", async () => {
    serve({ "https://ok.example/feed": feed("ok") });
    const db = tenantWith(["http://10.0.0.1/feed", "https://ok.example/feed", `https://${OWN}/t/bravo/`]);

    const polled = await db.pollRss({ ownHosts: [OWN] });

    expect(requests.map((r) => r.url)).toEqual(["https://ok.example/feed"]);
    expect(polled.failures).toHaveLength(2);
  });

  it("fails a feed that redirects more than the policy's hops", async () => {
    const hops = POLICY.poll.max_redirects;
    const routes = {};
    for (let i = 0; i <= hops; i++) routes[`https://hop.example/${i}`] = { redirect: `https://hop.example/${i + 1}` };
    routes[`https://hop.example/${hops + 1}`] = feed("hop");
    serve(routes);
    const within = { ...routes, [`https://hop.example/${hops}`]: feed("hop") };

    const over = await tenantWith(["https://hop.example/0"]).pollRss();
    serve(within);
    const at = await tenantWith(["https://hop.example/0"]).pollRss();

    expect(over.failures).toHaveLength(1);
    expect(over.failures[0]).toContain(`more than ${hops} redirects`);
    expect(at.failures).toEqual([]);
  });

  it("fails a feed whose body passes the policy's bytes, declared or not, reading no further", async () => {
    const max = POLICY.poll.max_feed_bytes;
    let pulled = 0;
    const endless = new ReadableStream({
      pull(controller) {
        pulled += 1 << 20;
        controller.enqueue(new Uint8Array(1 << 20).fill(32));
      },
    });
    serve({ "https://big.example/feed": { stream: endless }, "https://ok.example/feed": feed("ok") });

    const polled = await tenantWith(["https://big.example/feed", "https://ok.example/feed"]).pollRss();

    expect(polled.failures).toEqual([`feed 00: Error: feed over ${max} bytes`]);
    expect(polled.written).toBe(1);
    expect(pulled).toBeLessThanOrEqual(max + 2 * (1 << 20));
  });

  it("polls the first feeds by name up to the policy's cap, and logs the ones it skipped", async () => {
    const warn = vi.mocked(console.warn);
    const count = POLICY.max_sources + 2;
    const urls = Array.from({ length: count }, (_, i) => `https://f${i}.example/feed`);
    serve(Object.fromEntries(urls.map((url, i) => [url, feed(`f${i}`)])));

    const polled = await tenantWith(urls).pollRss();

    expect(requests).toHaveLength(POLICY.max_sources);
    expect(polled.fetches).toBe(POLICY.max_sources);
    expect(polled.skipped).toBe(2);
    expect(warn).toHaveBeenCalledWith("rss poll skipped feeds past the source cap", {
      cap: POLICY.max_sources,
      skipped: [`feed ${POLICY.max_sources}`, `feed ${POLICY.max_sources + 1}`],
    });
  });
});
