// The hosted Worker's daily backup (hosted_backup.js) through its entry, hosted.js: at
// BACKUP_HOUR_UTC the tick exports every registered tenant and the registry to R2, tells the
// operator of each copy that failed, and the operator restores one export into a new tenant id.
// Over real SQLite; R2 is a map, and the feeds and the operator's webhook are a stub fetch.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { env } from "cloudflare:workers";
import worker from "../src/hosted.js";
import WORDING from "../src/hosted_alerts.json" with { type: "json" };
import { OPERATOR_WEBHOOK } from "../src/hosted_alerts.js";
import {
  BACKUP_CHECK_HOURS,
  BACKUP_HOUR_UTC,
  REGISTRY_EXPORT,
  backupAll,
  checkBackup,
  restoreExport,
} from "../src/hosted_backup.js";
import { REGISTRY_NAME, tenantRegistry } from "../src/tenant_registry.js";
import { HOSTED_SCHEMA_SQL, SCHEMA_SQL, drainAlarms, namespace, registryNamespace, tenantDb } from "./sql_storage.js";

const HOOK = "https://discord.com/api/webhooks/1/operator";
const DAY = "2026-11-22";
const BACKUP_TIME = new Date(`${DAY}T0${BACKUP_HOUR_UTC}:00:00Z`);
const STAMP = "2026-11-21T00:00:00+00:00";

// R2 as the binding answers: put() keeps a copy of the bytes and its options, get() an object
// body or null, head() its metadata or null. `refuse(key)` makes that put throw.
function r2({ refuse = () => false, log = [] } = {}) {
  const objects = new Map();
  return {
    objects,
    async put(key, value, options) {
      log.push(`r2 ${key}`);
      if (refuse(key)) throw new Error("R2 put failed: 500 internal error");
      objects.set(key, { bytes: new Uint8Array(value), options });
      return { key };
    },
    async get(key) {
      const object = objects.get(key);
      return object ? { arrayBuffer: async () => object.bytes.slice().buffer } : null;
    },
    async head(key) {
      return objects.has(key) ? { key, size: objects.get(key).bytes.byteLength } : null;
    },
  };
}

// The one fetch every edge goes through: a feed or the operator's webhook, each recorded.
function network({ log = [] } = {}) {
  const sent = { alerts: [], feeds: [] };
  const fetch = async (input, init = {}) => {
    const request = input instanceof Request ? input : new Request(input, init);
    const url = new URL(request.url);
    if (request.url === HOOK) {
      sent.alerts.push(JSON.parse(await request.text()).content);
      return new Response(null, { status: 204 });
    }
    log.push(`feed ${url.hostname}`);
    sent.feeds.push(request.url);
    return new Response(`<?xml version="1.0"?><rss version="2.0"><channel><title>f</title></channel></rss>`);
  };
  return { fetch, sent };
}

function bindings({ BACKUP = r2(), make } = {}) {
  const TENANT_DB = namespace(make);
  const TENANT_RUN = { getByName: () => ({ startRun: async () => ({ started: true }) }) };
  const bound = { TENANT_DB, TENANT_RUN, BACKUP, [OPERATOR_WEBHOOK]: HOOK };
  bound.REGISTRY = registryNamespace(bound);
  return bound;
}

// A tenant registered with `status`, seeded with both schemas, an RSS source and a page.
async function tenant(bound, id, { status = "active" } = {}) {
  await tenantRegistry(env).register(id, { status });
  const db = bound.TENANT_DB.getByName(id);
  db.query(SCHEMA_SQL);
  db.query(HOSTED_SCHEMA_SQL);
  db.query("INSERT INTO sources (name, url, type, config) VALUES (?, ?, 'rss', '{}')", [`${id} news`, `https://${id}.test/feed`]);
  db.query("INSERT INTO hosted_pages (path, body, content_type, updated_at) VALUES (?, ?, 'text/html', ?)", [
    "/index.html",
    `<html>${id}</html>`,
    STAMP,
  ]);
  return db;
}

// What a restored copy holds, table by table, for comparing it with the tenant it came from.
const tableRows = (db) =>
  Object.fromEntries(Object.keys(db.tables()).map((name) => [name, db.query(`SELECT * FROM "${name}" ORDER BY rowid`).rows]));

async function restoredFrom(bytes) {
  const { db } = tenantDb();
  await db.restoreDump(bytes);
  return db;
}

describe("the hosted Worker's daily backup", () => {
  let bound;
  let net;
  let logs;
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(BACKUP_TIME);
    logs = [];
    vi.spyOn(console, "log").mockImplementation((...args) => logs.push(args));
    vi.spyOn(console, "warn").mockImplementation(() => {});
  });
  const start = (options = {}, networkOptions = {}) => {
    bound = bindings(options);
    Object.assign(env, bound);
    net = network(networkOptions);
    vi.stubGlobal("fetch", net.fetch);
  };
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.useRealTimers();
    for (const key of Object.keys(env)) delete env[key];
  });

  it("at its hour writes every registered tenant, paused too, and the registry to R2, and sends no alert", async () => {
    start();
    const alpha = await tenant(bound, "alpha");
    const paused = await tenant(bound, "paused", { status: "paused" });

    await worker.scheduled({ scheduledTime: BACKUP_TIME.getTime() });

    const keys = [`${DAY}/alpha.jsonl.gz`, `${DAY}/paused.jsonl.gz`, `${DAY}/_registry.jsonl.gz`];
    expect([...bound.BACKUP.objects.keys()]).toEqual(keys);
    for (const [key, object] of bound.BACKUP.objects) {
      expect(object.options.sha256).toMatch(/^[0-9a-f]{64}$/);
      expect(object.options.httpMetadata).toEqual({ contentType: "application/gzip" });
      expect(key).toMatch(/\.jsonl\.gz$/);
    }
    expect(tableRows(await restoredFrom(bound.BACKUP.objects.get(`${DAY}/alpha.jsonl.gz`).bytes))).toEqual(tableRows(alpha));
    expect(tableRows(await restoredFrom(bound.BACKUP.objects.get(`${DAY}/paused.jsonl.gz`).bytes))).toEqual(tableRows(paused));
    const registry = await restoredFrom(bound.BACKUP.objects.get(`${DAY}/_registry.jsonl.gz`).bytes);
    expect(registry.query("SELECT id, status FROM tenants ORDER BY id").rows).toEqual([
      { id: "alpha", status: "active" },
      { id: "paused", status: "paused" },
    ]);
    expect(net.sent.alerts).toEqual([]);
    const [, measure] = logs.find(([line]) => line === "hosted backup");
    expect(measure).toMatchObject({ date: DAY, exports: 3, r2: 3, failed: [] });
    expect(measure).not.toHaveProperty("b2");
  });

  it("runs only in the tick of its own UTC hour", async () => {
    start();
    await tenant(bound, "alpha");

    for (const hour of [0, BACKUP_HOUR_UTC - 1, BACKUP_HOUR_UTC + 1, 8, 20, 23]) {
      const at = new Date(`${DAY}T${String(hour).padStart(2, "0")}:00:00Z`);
      vi.setSystemTime(at);
      await worker.scheduled({ scheduledTime: at.getTime() });
    }

    expect(bound.BACKUP.objects.size).toBe(0);
  });

  it("starts once the tick has polled every tenant and queued its run, and runs when the tick fails", async () => {
    const log = [];
    start({ BACKUP: r2({ log }) }, { log });
    await tenant(bound, "alpha");
    await tenant(bound, "bravo");
    for (const id of ["alpha", "bravo"]) {
      await tenantRegistry(env).setSchedule(id, { times: [`0${BACKUP_HOUR_UTC}:00`, "16:00"], timeZone: "UTC", maxDigestTimes: 2 });
    }
    const registry = bound.REGISTRY.getByName(REGISTRY_NAME);
    const queuedAtFirstPut = [];
    const put = bound.BACKUP.put.bind(bound.BACKUP);
    bound.BACKUP.put = async (...args) => {
      if (!queuedAtFirstPut.length) queuedAtFirstPut.push(registry.runs("alpha").length, registry.runs("bravo").length);
      return put(...args);
    };

    await worker.scheduled({ scheduledTime: BACKUP_TIME.getTime() });
    await drainAlarms(registry);

    const firstBackup = log.findIndex((line) => line.startsWith("r2 "));
    expect(log.slice(0, firstBackup).sort()).toEqual(["feed alpha.test", "feed bravo.test"]);
    expect(queuedAtFirstPut).toEqual([1, 1]);
    expect(logs.findIndex(([line]) => line === "hosted tick")).toBeLessThan(logs.findIndex(([line]) => line === "hosted backup"));

    // A tick that throws, its tenant list unreadable, still backs up what it can.
    bound.BACKUP.objects.clear();
    const tenants = registry.tenants.bind(registry);
    registry.tenants = (filter) => (filter?.status ? Promise.reject(new Error("registry overloaded")) : tenants(filter));
    await expect(worker.scheduled({ scheduledTime: BACKUP_TIME.getTime() })).rejects.toThrow("registry overloaded");
    expect([...bound.BACKUP.objects.keys()]).toEqual([`${DAY}/alpha.jsonl.gz`, `${DAY}/bravo.jsonl.gz`, `${DAY}/_registry.jsonl.gz`]);

    // A backup that throws as well leaves the tick's failure the one the invocation reports.
    Object.defineProperty(env, "BACKUP", {
      configurable: true,
      enumerable: true,
      get() {
        throw new Error("binding torn down");
      },
    });
    await expect(worker.scheduled({ scheduledTime: BACKUP_TIME.getTime() })).rejects.toThrow("registry overloaded");
    expect(logs).toContainEqual(["hosted backup failed", { error: "binding torn down" }]);
  });

  it("tells the operator each copy that failed, naming the tenant and the copy, and goes on to the next", async () => {
    const failingExport = () => {
      const db = tenantDb().db;
      db.exportDump = async () => {
        throw new Error("Durable Object reset because its code was updated");
      };
      return db;
    };
    start({ BACKUP: r2({ refuse: (key) => key.endsWith("/charlie.jsonl.gz") }), make: () => tenantDb().db });
    await tenant(bound, "alpha");
    await tenantRegistry(env).register("bravo");
    bound.TENANT_DB.objects.set("bravo", failingExport());
    await tenant(bound, "charlie");
    await tenant(bound, "delta");

    const done = await backupAll(env, { now: BACKUP_TIME });

    expect(net.sent.alerts).toEqual([
      expect.stringMatching(new RegExp(`^\\[bravo\\] ${WORDING.backup_export_failed}\n\`\`\`\nkey=${DAY}/bravo.jsonl.gz\nDurable Object reset`)),
      expect.stringMatching(new RegExp(`^\\[charlie\\] ${WORDING.backup_r2_failed}\n\`\`\`\nkey=${DAY}/charlie.jsonl.gz\nR2 put failed`)),
    ]);
    expect(done.failed).toEqual([
      { name: "bravo", copy: "export" },
      { name: "charlie", copy: "r2" },
    ]);
    expect([...bound.BACKUP.objects.keys()]).toEqual(["alpha", "delta", REGISTRY_EXPORT].map((name) => `${DAY}/${name}.jsonl.gz`));
  });

  it("says no tenant was exported when the list cannot be read, which holds when the registry's export fails too", async () => {
    start();
    await tenant(bound, "alpha");
    const registry = bound.REGISTRY.getByName(REGISTRY_NAME);
    registry.tenants = async () => {
      throw new Error("registry overloaded");
    };
    registry.exportDump = async () => {
      throw new Error("registry overloaded");
    };

    const done = await backupAll(env, { now: BACKUP_TIME });

    expect(done).toMatchObject({ exports: 0, failed: [{ name: REGISTRY_EXPORT, copy: "list" }, { name: REGISTRY_EXPORT, copy: "export" }] });
    expect(net.sent.alerts.map((alert) => alert.split("\n")[0])).toEqual([
      `[${REGISTRY_EXPORT}] Daily backup could not list the tenants: no tenant was exported`,
      `[${REGISTRY_EXPORT}] ${WORDING.backup_export_failed}`,
    ]);
  });
});

describe("the later tick's check that the day's backup reached its end", () => {
  let bound;
  let net;
  let logs;
  const CHECK_TIME = new Date(`${DAY}T0${BACKUP_HOUR_UTC + BACKUP_CHECK_HOURS}:00:00Z`);
  const tickAt = async (at) => {
    vi.setSystemTime(at);
    await worker.scheduled({ scheduledTime: at.getTime() });
  };
  const hours = (from, to) =>
    Array.from({ length: to - from }, (_, i) => new Date(`${DAY}T${String(from + i).padStart(2, "0")}:00:00Z`));
  const missing = `[${WORDING.backup_every_export}] ${WORDING.backup_missing}\n\`\`\`\nkey=${DAY}/_registry.jsonl.gz\n\`\`\``;
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    logs = [];
    vi.spyOn(console, "log").mockImplementation((...args) => logs.push(args));
    bound = bindings();
    Object.assign(env, bound);
    net = network();
    vi.stubGlobal("fetch", net.fetch);
  });
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.useRealTimers();
    for (const key of Object.keys(env)) delete env[key];
  });

  it("tells the operator once that day when the backup's tick never ran", async () => {
    await tenant(bound, "alpha");

    for (const at of [...hours(0, BACKUP_HOUR_UTC), ...hours(BACKUP_HOUR_UTC + 1, 24)]) await tickAt(at);

    expect(net.sent.alerts).toEqual([missing]);
    expect(bound.BACKUP.objects.size).toBe(0);
  });

  it("tells the operator when the backup stopped before its last export, the registry's", async () => {
    await tenant(bound, "alpha");
    const registry = bound.REGISTRY.getByName(REGISTRY_NAME);
    registry.exportDump = async () => {
      throw new Error("the invocation was cancelled");
    };

    await tickAt(BACKUP_TIME);
    await tickAt(CHECK_TIME);

    expect([...bound.BACKUP.objects.keys()]).toEqual([`${DAY}/alpha.jsonl.gz`]);
    expect(net.sent.alerts).toEqual([expect.stringContaining(WORDING.backup_export_failed), missing]);
  });

  it("sends nothing when the backup reached its end", async () => {
    await tenant(bound, "alpha");

    for (const at of hours(0, 24)) await tickAt(at);

    expect(net.sent.alerts).toEqual([]);
    expect(logs).toContainEqual(["hosted backup check", { key: `${DAY}/_registry.jsonl.gz`, present: true }]);
  });

  it("tells the operator when R2 cannot be asked, and checks nothing without the binding", async () => {
    bound.BACKUP.head = async () => {
      throw new Error("R2 head failed: 503");
    };

    await tickAt(CHECK_TIME);
    delete env.BACKUP;
    await tickAt(CHECK_TIME);

    expect(net.sent.alerts).toEqual([expect.stringMatching(new RegExp(`^\\[${WORDING.backup_every_export}\\] ${WORDING.backup_check_failed}\n[^]*R2 head failed: 503`))]);
    expect(await checkBackup(env, { now: CHECK_TIME })).toEqual({ unbound: "no BACKUP binding" });
  });
});

describe("the operator's restore of one export", () => {
  let bound;
  beforeEach(() => {
    vi.spyOn(console, "log").mockImplementation(() => {});
    bound = bindings();
    delete bound[OPERATOR_WEBHOOK];
    Object.assign(env, bound);
  });
  afterEach(() => {
    vi.restoreAllMocks();
    for (const key of Object.keys(env)) delete env[key];
  });

  it("loads a tenant's export into a new, unregistered id, which then holds the tenant's rows", async () => {
    const alpha = await tenant(bound, "alpha");
    await backupAll(env, { now: BACKUP_TIME });

    const answer = await restoreExport(env, { date: DAY, from: "alpha", into: "alpha-restored" });

    expect(answer).toMatchObject({ tenant: "alpha-restored", from: "alpha", date: DAY, key: `${DAY}/alpha.jsonl.gz` });
    expect(answer.restored.sources).toBe(1);
    expect(tableRows(bound.TENANT_DB.getByName("alpha-restored"))).toEqual(tableRows(alpha));
    // Registering it is the operator's next, separate step.
    expect(await tenantRegistry(env).tenant("alpha-restored")).toBeNull();
  });

  it("refuses an id that is registered, holds tables, or no tenant may take, and an export that is not there", async () => {
    await tenant(bound, "alpha");
    await tenant(bound, "bravo", { status: "paused" });
    await backupAll(env, { now: BACKUP_TIME });
    bound.TENANT_DB.getByName("used").query("CREATE TABLE notes (v)");

    const refusals = {
      bravo: await restoreExport(env, { date: DAY, from: "alpha", into: "bravo" }),
      used: await restoreExport(env, { date: DAY, from: "alpha", into: "used" }),
      reserved: await restoreExport(env, { date: DAY, from: "alpha", into: "login" }),
      edge: await restoreExport(env, { date: DAY, from: "alpha", into: "-new" }),
      registry: await restoreExport(env, { date: DAY, from: REGISTRY_EXPORT, into: "new" }),
      date: await restoreExport(env, { date: "22-11-2026", from: "alpha", into: "new" }),
      missing: await restoreExport(env, { date: "2026-11-01", from: "alpha", into: "new" }),
    };

    expect(Object.fromEntries(Object.entries(refusals).map(([name, answer]) => [name, answer.status]))).toEqual({
      bravo: 409,
      used: 409,
      reserved: 400,
      edge: 400,
      registry: 400,
      date: 400,
      missing: 404,
    });
    expect(refusals.bravo.refused).toMatch(/already registered/);
    expect(refusals.used.refused).toMatch(/already holds 1 table/);
    expect(refusals.missing.refused).toBe("no export at 2026-11-01/alpha.jsonl.gz");
    expect(bound.TENANT_DB.getByName("used").tables()).toEqual({ notes: 0 });
    expect(bound.TENANT_DB.getByName("bravo").tables().hosted_pages).toBe(1);
    expect(bound.TENANT_DB.objects.has("new")).toBe(false);
  });
});
