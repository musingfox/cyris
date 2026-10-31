import assert from "node:assert/strict";
import { test } from "node:test";

import { d1Sqlite } from "./d1-sqlite.js";

const AUTH = { Authorization: "Bearer t" };

// The shape `articles` had before `image_url`, as production's buffer still holds it.
const OLD_ARTICLES = `CREATE TABLE articles (url TEXT PRIMARY KEY, guid TEXT,
  title TEXT NOT NULL DEFAULT '', content TEXT NOT NULL DEFAULT '', author TEXT,
  published_at TEXT NOT NULL, source_name TEXT NOT NULL, fetched_at TEXT NOT NULL)`;

function columns(DB) {
  return DB.raw
    .prepare("PRAGMA table_info('articles')")
    .all()
    .map((c) => c.name);
}

// A fresh module instance per call, so its once-per-isolate schema flag starts unset.
async function freshWorker(tag) {
  return (await import(`../src/index.js?${tag}`)).default;
}

test("a buffer created before image_url gains the column and keeps its rows", async () => {
  const DB = d1Sqlite();
  DB.raw.exec(OLD_ARTICLES);
  DB.raw.exec(
    `INSERT INTO articles VALUES ('https://e.test/1', NULL, 'kept', '', NULL,
      '2026-10-01T00:00:00.000Z', 'Feed', '2026-10-01T00:00:00.000Z')`
  );
  const worker = await freshWorker("old-table");
  const response = await worker.fetch(
    new Request(
      "https://rss.example/articles?after=2026-09-30T00:00:00.000Z&before=2026-10-02T00:00:00.000Z",
      { headers: AUTH }
    ),
    { DB, RSS_TOKEN: "t" }
  );
  assert.equal(response.status, 200);
  assert.ok(columns(DB).includes("image_url"));
  assert.deepEqual(
    (await response.json()).map((r) => [r.title, r.image_url]),
    [["kept", null]]
  );
});

test("a poll stores the image its feed names for an entry", async () => {
  const DB = d1Sqlite();
  DB.raw.exec("CREATE TABLE sources (name TEXT, url TEXT, type TEXT)");
  DB.raw.exec("INSERT INTO sources VALUES ('Pics', 'https://pics.test/feed', 'rss')");
  const feed = `<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel>
    <item><title>P</title><link>https://pics.test/1</link><pubDate>${new Date().toUTCString()}</pubDate>
    <media:thumbnail url="https://pics.test/1.jpg"/></item></channel></rss>`;
  const saved = { fetch: globalThis.fetch, log: console.log };
  globalThis.fetch = async () => new Response(feed);
  console.log = () => {};
  try {
    const worker = await freshWorker("poll");
    await worker.fetch(
      new Request("https://rss.example/poll", { method: "POST", headers: AUTH }),
      { DB, RSS_TOKEN: "t" }
    );
  } finally {
    globalThis.fetch = saved.fetch;
    console.log = saved.log;
  }
  const rows = DB.raw.prepare("SELECT url, image_url FROM articles").all();
  assert.deepEqual(
    rows.map((r) => [r.url, r.image_url]),
    [["https://pics.test/1", "https://pics.test/1.jpg"]]
  );
});

test("a buffer that already has image_url is left as it is", async () => {
  const DB = d1Sqlite();
  const env = { DB, RSS_TOKEN: "t" };
  const stats = () => new Request("https://rss.example/stats", { headers: AUTH });
  await (await freshWorker("new-table-1")).fetch(stats(), env);
  const before = columns(DB);
  const response = await (await freshWorker("new-table-2")).fetch(stats(), env);
  assert.equal(response.status, 200);
  assert.deepEqual(columns(DB), before);
  assert.equal(before.filter((name) => name === "image_url").length, 1);
});
