import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import worker from "../src/index.js";
import { d1Sqlite } from "./d1-sqlite.js";

const cases = JSON.parse(readFileSync(new URL("./articles-window.json", import.meta.url)));
const DB = d1Sqlite();
const env = { DB, RSS_TOKEN: "t" };
const AUTH = { Authorization: "Bearer t" };

async function get(path) {
  return worker.fetch(new Request(`https://rss.example${path}`, { headers: AUTH }), env);
}

await get("/stats");
const insert = DB.raw.prepare(
  `INSERT INTO articles (url, guid, title, content, author, published_at, source_name, fetched_at)
   VALUES (:url, :guid, :title, :content, :author, :published_at, :source_name, :fetched_at)`
);
for (const row of cases.rows) insert.run(row);

test("every shared read returns its window of buffer entries, newest-buffered first", async () => {
  assert.ok(cases.reads.length >= 4);
  for (const read of cases.reads) {
    const query = new URLSearchParams({ after: read.after, before: read.before });
    if (read.limit !== undefined) query.set("limit", read.limit);
    const response = await get(`/articles?${query}`);
    assert.equal(response.status, 200, read.name);
    const rows = await response.json();
    assert.deepEqual(
      rows.map((r) => r.url),
      read.expect,
      read.name
    );
    for (const row of rows) assert.deepEqual(Object.keys(row).sort(), cases.columns, read.name);
  }
});

test("a read needs both bounds", async () => {
  const response = await get("/articles?after=2026-10-01T00:00:00.000Z");
  assert.equal(response.status, 400);
  assert.deepEqual(await response.json(), { error: "after and before required" });
});

test("the buffer is indexed on when an entry arrived, and still on its publish date", () => {
  const info = DB.raw.prepare("PRAGMA index_info('idx_articles_fetched_at')").all();
  assert.deepEqual(
    info.map((r) => r.name),
    ["fetched_at"]
  );
  const names = DB.raw.prepare("PRAGMA index_list('articles')").all().map((r) => r.name);
  assert.ok(names.includes("idx_articles_published_at"));
});
