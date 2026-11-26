// The self-hosted newsletter Worker's queue: GET /newsletters reads every key KV lists, page by
// page, and email() stamps when each message arrived. Run by the repo root's vitest.
import { describe, expect, it } from "vitest";
import worker from "../src/index.js";

const TOKEN = "queue-token";
const PAGE = 1000;
const MAX_BULK = 100;

// KV as its docs describe it: sorted keys, at most 1000 a list page with a cursor until
// `list_complete`, and a bulk get of at most 100 keys answering a Map.
function fakeKv(entries = {}) {
  const store = new Map(Object.entries(entries));
  const ops = { lists: 0, gets: 0 };
  return {
    ops,
    store,
    async list({ prefix = "", cursor } = {}) {
      ops.lists += 1;
      const names = [...store.keys()].filter((name) => name.startsWith(prefix)).sort();
      const start = cursor ? Number(cursor) : 0;
      const keys = names.slice(start, start + PAGE).map((name) => ({ name }));
      const done = start + PAGE >= names.length;
      return { keys, list_complete: done, ...(done ? {} : { cursor: String(start + PAGE) }) };
    },
    async get(names, type) {
      ops.gets += 1;
      if (!Array.isArray(names) || names.length > MAX_BULK || type !== "json") throw new Error("not a bulk json get");
      return new Map(names.map((name) => [name, store.has(name) ? JSON.parse(store.get(name)) : null]));
    },
    async put(name, value) {
      store.set(name, value);
    },
  };
}

const pull = (kv) =>
  worker.fetch(new Request("https://newsletter.example/newsletters", { headers: { Authorization: `Bearer ${TOKEN}` } }), {
    NEWSLETTER_TOKEN: TOKEN,
    NEWSLETTERS: kv,
  });

describe("GET /newsletters", () => {
  it("reads past KV's first list page, by its cursor, in batches a bulk get takes", async () => {
    const count = 2 * PAGE + 7;
    const kv = fakeKv(
      Object.fromEntries(
        Array.from({ length: count }, (_, i) => {
          const id = `nl:${String(i).padStart(5, "0")}`;
          return [id, JSON.stringify({ id, from: "w@letters.example" })];
        }),
      ),
    );

    const items = await (await pull(kv)).json();

    expect(items).toHaveLength(count);
    expect(new Set(items.map((item) => item.id)).size).toBe(count);
    expect(kv.ops.lists).toBe(3);
    expect(kv.ops.lists + kv.ops.gets).toBeLessThan(1000);
  });

  it("answers an empty queue with an empty list", async () => {
    expect(await (await pull(fakeKv())).json()).toEqual([]);
  });
});

describe("email()", () => {
  it("stores each message with when it arrived", async () => {
    const kv = fakeKv();
    const raw = "From: Writer <w@letters.example>\r\nSubject: Issue 1\r\nDate: Mon, 23 Nov 2026 06:00:00 +0000\r\n\r\nBody\r\n";
    const before = Date.now();

    await worker.email({ raw, from: "w@letters.example", rawSize: raw.length }, { NEWSLETTERS: kv });

    const [stored] = [...kv.store.values()].map((value) => JSON.parse(value));
    expect(stored.date).toBe("2026-11-23T06:00:00.000Z");
    expect(Date.parse(stored.received_at)).toBeGreaterThanOrEqual(before);
  });
});
