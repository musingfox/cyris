import assert from "node:assert/strict";
import { test } from "node:test";

import worker from "../src/index.js";
import { d1Sqlite } from "./d1-sqlite.js";

// One database for the file: the Worker applies its schema once per isolate,
// so a second fresh database would never get its tables.
const DB = d1Sqlite();
DB.raw.exec("CREATE TABLE sources (name TEXT, url TEXT, type TEXT)");
const env = { DB, RSS_TOKEN: "t" };

function rss(link) {
  return `<?xml version="1.0"?><rss version="2.0"><channel><title>F</title>
    <item><title>Post</title><link>${link}</link><guid>${link}</guid>
    <pubDate>${new Date().toUTCString()}</pubDate><description>body</description></item>
  </channel></rss>`;
}

// `answers` maps a feed name to the HTTP status its URL answers; 200 serves one entry.
async function poll(answers) {
  DB.raw.exec("DELETE FROM sources");
  const add = DB.raw.prepare("INSERT INTO sources (name, url, type) VALUES (?, ?, 'rss')");
  for (const name of Object.keys(answers)) add.run(name, `https://${name}.test/feed`);

  const errors = [];
  const saved = { fetch: globalThis.fetch, log: console.log, warn: console.warn, error: console.error };
  globalThis.fetch = async (url) => {
    const name = new URL(url).hostname.split(".")[0];
    const status = answers[name];
    return status === 200
      ? new Response(rss(`https://${name}.test/${Date.now()}`))
      : new Response("", { status });
  };
  console.log = console.warn = () => {};
  console.error = (...args) => errors.push(args.join(" "));
  try {
    const response = await worker.fetch(
      new Request("https://rss.example/poll", {
        method: "POST",
        headers: { Authorization: "Bearer t" },
      }),
      env
    );
    return { status: response.status, body: await response.json(), errors };
  } finally {
    globalThis.fetch = saved.fetch;
    Object.assign(console, { log: saved.log, warn: saved.warn, error: saved.error });
  }
}

const health = (name) => DB.raw.prepare("SELECT * FROM feed_health WHERE name = ?").get(name);

test("a failing feed starts a streak that grows with each failed poll", async () => {
  const before = new Date().toISOString();
  await poll({ failing: 503 });
  const first = health("failing");
  assert.equal(first.consecutive_failures, 1);
  assert.equal(first.last_error, "HTTP 503");
  assert.ok(first.last_failed_at >= before);
  assert.equal(first.last_ok_at, null);

  await poll({ failing: 429 });
  const second = health("failing");
  assert.equal(second.consecutive_failures, 2);
  assert.equal(second.last_error, "HTTP 429");
  assert.ok(second.last_failed_at >= first.last_failed_at);
});

test("a success resets the streak and keeps the last error as history", async () => {
  await poll({ flaky: 503 });
  await poll({ flaky: 503 });
  const failed = health("flaky");

  const before = new Date().toISOString();
  await poll({ flaky: 200 });
  const ok = health("flaky");
  assert.equal(ok.consecutive_failures, 0);
  assert.ok(ok.last_ok_at >= before);
  assert.equal(ok.last_error, "HTTP 503");
  assert.equal(ok.last_failed_at, failed.last_failed_at);
});

test("every feed a poll reaches gets a row, the healthy ones too", async () => {
  await poll({ fine: 200, broken: 500 });
  assert.equal(health("fine").consecutive_failures, 0);
  assert.equal(health("fine").last_error, null);
  assert.notEqual(health("fine").last_ok_at, null);
  assert.equal(health("broken").consecutive_failures, 1);
});

test("a feed_health write that fails loses no article and fails no poll", async () => {
  const batch = DB.batch;
  DB.batch = async (statements) => {
    if (statements.some((s) => /feed_health/.test(s.sql))) throw new Error("D1_ERROR: boom");
    return batch(statements);
  };
  let result;
  try {
    result = await poll({ unrecorded: 200, alsounrecorded: 503 });
  } finally {
    DB.batch = batch;
  }

  assert.equal(result.status, 200);
  assert.equal(result.body.written, 1);
  assert.deepEqual(result.body.failures, ["alsounrecorded: Error: HTTP 503"]);
  const stored = DB.raw
    .prepare("SELECT count(*) AS n FROM articles WHERE source_name = 'unrecorded'")
    .get();
  assert.equal(stored.n, 1);
  assert.equal(health("unrecorded"), undefined);
  assert.ok(result.errors.some((line) => /feed_health/.test(line) && /boom/.test(line)));
});
