import { afterEach, describe, it, expect, vi } from "vitest";
import { TYPE_SCALES, createTypeScale, typeScaleStyle } from "../src/type_scale.js";

const TOKEN = "tok-SECRET";
const ENV = { CLOUDFLARE_ACCOUNT_ID: "acct-1", CYRIS_STORE_DATABASE_ID: "db-1", CLOUDFLARE_API_TOKEN: TOKEN };
const API = "https://api.cloudflare.com/client/v4/accounts/";

const rows = (value) => ({
  success: true,
  result: [{ results: value === undefined ? [] : [{ value }], success: true }],
});

// A D1 REST API answering `answer()` to every query, recording each call.
function d1(answer) {
  const fetchImpl = async (url, init) => {
    fetchImpl.calls.push({ url, init });
    return answer(url, init);
  };
  fetchImpl.calls = [];
  return fetchImpl;
}

const answering = (value) => d1(() => Response.json(rows(value)));

const replying = (status, body) => d1(() => new Response(body, { status }));

function reader(fetchImpl, options = {}) {
  return createTypeScale({ env: ENV, fetchImpl, ...options });
}

// Every argument any console method was called with, stringified.
function captureConsole() {
  const logged = [];
  for (const method of ["log", "info", "warn", "error", "debug"]) {
    vi.spyOn(console, method).mockImplementation((...args) => {
      const text = (arg) =>
        arg instanceof Error ? `${arg.name}: ${arg.message}` : (JSON.stringify(arg) ?? String(arg));
      logged.push(...args.map(text));
    });
  }
  return logged;
}

afterEach(() => vi.restoreAllMocks());

describe("WorkerReadsTypeScale", () => {
  it("reads the value from D1 over REST", async () => {
    const fetchImpl = answering("1.125");

    expect(await reader(fetchImpl).current()).toBe(1.125);

    expect(fetchImpl.calls).toHaveLength(1);
    const [{ url, init }] = fetchImpl.calls;
    expect(url).toBe("https://api.cloudflare.com/client/v4/accounts/acct-1/d1/database/db-1/query");
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer tok-SECRET");
    expect(JSON.parse(init.body)).toEqual({
      sql: "SELECT value FROM settings WHERE key = ?",
      params: ["digest.type_scale"],
    });
  });

  it.each([
    ["0.875", 0.875],
    ["1", 1],
  ])("reads %s as %s", async (stored, scale) => {
    expect(await reader(answering(stored)).current()).toBe(scale);
  });

  it("styles the root with the scale", () => {
    expect(typeScaleStyle(1.125)).toBe("<style>html:root{--type-scale:1.125}</style>");
  });

  it("has the setting's three steps", () => {
    expect(TYPE_SCALES).toEqual([0.875, 1, 1.125]);
  });
});

const INCOMPLETE = [
  ...["CYRIS_STORE_DATABASE_ID", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"].flatMap((name) => {
    const { [name]: _, ...without } = ENV;
    return [
      [`without ${name}`, without],
      [`with an empty ${name}`, { ...ENV, [name]: "" }],
    ];
  }),
  ["on the json backend", { ...ENV, CYRIS_STORE_BACKEND: "json" }],
];

// Every failed or unusable answer the reader must survive; the token checks reuse them.
const FAILING = [
  ["a 429", () => replying(429, "rate limited")],
  ["a 500", () => replying(500, "tok-SECRET leaked")],
  ["a network error", () => d1(() => { throw new TypeError("network"); })],
  ["an error carrying the token", () => d1(() => { throw new Error(TOKEN); })],
  ["success false", () => d1(() => Response.json({ success: false, errors: [{ code: 7500 }] }))],
  ["no row", () => answering(undefined)],
  ["1.25", () => answering("1.25")],
  ["a JSON string", () => answering('"1.125"')],
  ["true", () => answering("true")],
  ["markup", () => answering("1.125</style><script>alert(1)</script>")],
  ["a body that is not JSON", () => replying(200, "<html>")],
];

describe("TypeScaleReadFallsBackToOne", () => {
  it.each(INCOMPLETE)("is 1 %s", async (_, env) => {
    expect(await createTypeScale({ env, fetchImpl: answering("1.125") }).current()).toBe(1);
  });

  it.each(FAILING)("is 1 on %s", async (_, api) => {
    captureConsole();
    expect(await reader(api()).current()).toBe(1);
  });

  it("gives up on a read that never answers", async () => {
    const hanging = d1((url, init) => new Promise((_, reject) => {
      init.signal.addEventListener("abort", () => reject(init.signal.reason));
    }));
    captureConsole();
    const started = Date.now();

    expect(await reader(hanging, { timeoutMs: 20 }).current()).toBe(1);

    expect(Date.now() - started).toBeLessThan(500);
  });
});

describe("TypeScaleReadSendsTheTokenOnlyToCloudflare", () => {
  it.each(INCOMPLETE)("makes no request %s", async (_, env) => {
    const fetchImpl = answering("1.125");

    await createTypeScale({ env, fetchImpl }).current();

    expect(fetchImpl.calls).toHaveLength(0);
  });

  it.each(FAILING)("asks only Cloudflare's API on %s", async (_, api) => {
    captureConsole();
    const fetchImpl = api();

    await reader(fetchImpl).current();

    expect(fetchImpl.calls).toHaveLength(1);
    expect(fetchImpl.calls[0].url.startsWith(API)).toBe(true);
  });

  it.each(FAILING)("never logs the token on %s", async (_, api) => {
    const logged = captureConsole();

    await reader(api()).current();

    expect(logged.filter((text) => text.includes(TOKEN))).toEqual([]);
  });
});

describe("TypeScaleReadsAreMemoised", () => {
  const clock = (t = 0) => {
    const now = () => now.t;
    now.t = t;
    return now;
  };

  it("asks once for many concurrent page views", async () => {
    const fetchImpl = answering("1.125");
    const scale = reader(fetchImpl);

    const scales = await Promise.all(Array.from({ length: 50 }, () => scale.current()));

    expect(fetchImpl.calls).toHaveLength(1);
    expect(new Set(scales)).toEqual(new Set([1.125]));
  });

  it("asks again once the value is a minute old", async () => {
    const fetchImpl = answering("1.125");
    const now = clock();
    const scale = reader(fetchImpl, { now });
    await scale.current();

    now.t = 59999;
    await scale.current();
    expect(fetchImpl.calls).toHaveLength(1);

    now.t = 60000;
    await scale.current();
    expect(fetchImpl.calls).toHaveLength(2);
  });

  it("holds a failure as 1 for the same minute", async () => {
    captureConsole();
    const fetchImpl = replying(500, "down");
    const now = clock();
    const scale = reader(fetchImpl, { now });

    expect(await scale.current()).toBe(1);
    now.t = 30000;
    expect(await scale.current()).toBe(1);

    expect(fetchImpl.calls).toHaveLength(1);
  });

  it("asks again after forget", async () => {
    const fetchImpl = answering("1.125");
    const now = clock();
    const scale = reader(fetchImpl, { now });
    await scale.current();

    now.t = 10000;
    scale.forget();
    await scale.current();

    expect(fetchImpl.calls).toHaveLength(2);
  });

  it("does not let a read in flight during forget put the old value back", async () => {
    let settleFirst;
    const answers = [
      new Promise((resolve) => { settleFirst = resolve; }),
      Response.json(rows("1.125")),
    ];
    const fetchImpl = d1(() => answers.shift());
    const scale = reader(fetchImpl);

    const first = scale.current();
    scale.forget();
    settleFirst(Response.json(rows("1")));
    await first;

    expect(await scale.current()).toBe(1.125);
    expect(fetchImpl.calls).toHaveLength(2);
  });
});
