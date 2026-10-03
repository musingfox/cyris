import assert from "node:assert/strict";
import { test } from "node:test";

import worker from "../src/index.js";
import { d1Sqlite } from "./d1-sqlite.js";

const DAY = 86400_000;
const HOUR = 3600_000;

test("the poll deletes an entry 8 days after it entered the buffer, whatever its feed dated it", async () => {
  const DB = d1Sqlite();
  DB.raw.exec("CREATE TABLE sources (name TEXT, url TEXT, type TEXT)");
  const env = { DB, RSS_TOKEN: "t" };
  const auth = { Authorization: "Bearer t" };
  await worker.fetch(new Request("https://rss.example/stats", { headers: auth }), env);

  const now = Date.now();
  const iso = (ms) => new Date(ms).toISOString();
  const seed = DB.raw.prepare(
    `INSERT INTO articles (url, title, content, published_at, source_name, fetched_at)
     VALUES (?, '', '', ?, 'Feed', ?)`
  );
  seed.run("late", iso(now - 10 * DAY), iso(now - DAY));
  seed.run("stale", iso(now - DAY), iso(now - 9 * DAY));
  seed.run("edge", iso(now - 7 * DAY), iso(now - 7 * DAY - 23 * HOUR));

  const quiet = { log: console.log, error: console.error, warn: console.warn };
  console.log = console.error = console.warn = () => {};
  try {
    const response = await worker.fetch(
      new Request("https://rss.example/poll", { method: "POST", headers: auth }),
      env
    );
    assert.deepEqual(await response.json(), {
      feeds: 0,
      entries: 0,
      fresh: 0,
      written: 0,
      failures: [],
    });
  } finally {
    Object.assign(console, quiet);
  }

  const left = DB.raw.prepare("SELECT url FROM articles ORDER BY url").all().map((r) => r.url);
  assert.deepEqual(left, ["edge", "late"]);
});
