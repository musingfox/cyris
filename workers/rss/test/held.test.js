import assert from "node:assert/strict";
import { test } from "node:test";

import { pollFeeds } from "../src/buffer.js";
import { d1Sqlite } from "./d1-sqlite.js";

// pollFeeds' `held` option is the hosted poll's (workers/app/src/tenant_buffer.js). This Worker's
// own poll passes none, and then fetches a feed as it always has: the runtime follows its
// redirects, and its whole body is read, however large.

function database() {
  const DB = d1Sqlite();
  DB.raw.exec(
    "CREATE TABLE articles (url TEXT PRIMARY KEY, guid TEXT, title TEXT NOT NULL DEFAULT '', content TEXT NOT NULL DEFAULT '', author TEXT, published_at TEXT NOT NULL, source_name TEXT NOT NULL, fetched_at TEXT NOT NULL, image_url TEXT);" +
      "CREATE TABLE feed_health (name TEXT PRIMARY KEY, consecutive_failures INTEGER NOT NULL DEFAULT 0, last_error TEXT, last_failed_at TEXT, last_ok_at TEXT);"
  );
  return DB;
}

// A feed of one entry, padded with `padding` bytes of description.
const rss = (link, padding = 0) =>
  `<?xml version="1.0"?><rss version="2.0"><channel><title>F</title>
    <item><title>Post</title><link>${link}</link><guid>${link}</guid>
    <pubDate>${new Date().toUTCString()}</pubDate><description>${"x".repeat(padding)}</description></item>
  </channel></rss>`;

async function withFetch(answer, run) {
  const saved = { fetch: globalThis.fetch, log: console.log, warn: console.warn };
  const requests = [];
  globalThis.fetch = async (url, init) => {
    requests.push({ url: String(url), init });
    return answer(String(url));
  };
  console.log = console.warn = () => {};
  try {
    return { result: await run(), requests };
  } finally {
    Object.assign(globalThis, { fetch: saved.fetch });
    Object.assign(console, { log: saved.log, warn: saved.warn });
  }
}

test("with no option, a feed is fetched with the runtime's own redirects and read whole", async () => {
  const big = 6 * 1024 * 1024;
  const { result, requests } = await withFetch(
    () => new Response(rss("https://big.test/post", big)),
    () => pollFeeds(database(), [{ name: "Big", url: "https://big.test/feed" }])
  );

  assert.deepEqual(result.failures, []);
  assert.equal(result.written, 1);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].init.redirect, undefined);
});

test("with no option, a feed on any host is fetched as it is named", async () => {
  const { result, requests } = await withFetch(
    () => new Response(rss("http://127.0.0.1/post")),
    () => pollFeeds(database(), [{ name: "Local", url: "http://127.0.0.1:8080/feed" }])
  );

  assert.deepEqual(result.failures, []);
  assert.deepEqual(requests.map((r) => r.url), ["http://127.0.0.1:8080/feed"]);
});

test("with `held`, each hop is checked before it is asked and the body is read only so far", async () => {
  const held = {
    maxRedirects: 1,
    maxBytes: 4096,
    refuse: (url) => (new URL(url).hostname === "blocked.test" ? "blocked" : null),
  };
  const routes = {
    "https://a.test/feed": () => new Response(null, { status: 301, headers: { Location: "https://b.test/feed" } }),
    "https://b.test/feed": () => new Response(rss("https://b.test/post")),
    "https://c.test/feed": () => new Response(null, { status: 302, headers: { Location: "https://blocked.test/" } }),
    "https://d.test/feed": () => new Response(rss("https://d.test/post", 8192)),
  };
  const { result, requests } = await withFetch(
    (url) => routes[url](),
    () =>
      pollFeeds(
        database(),
        ["a", "c", "d"].map((name) => ({ name, url: `https://${name}.test/feed` })),
        { held }
      )
  );

  assert.equal(result.written, 1);
  assert.deepEqual(result.failures, [
    "c: Error: redirect to https://blocked.test/ refused: blocked",
    "d: Error: feed over 4096 bytes",
  ]);
  assert.ok(requests.every((r) => r.init.redirect === "manual"));
  assert.ok(!requests.some((r) => r.url.includes("blocked.test")));
});
