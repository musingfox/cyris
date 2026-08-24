import assert from "node:assert/strict";
import { test } from "node:test";

import { loadFeeds } from "../src/feeds.js";

const BUNDLED = [{ name: "Bundled", url: "https://bundled.test/feed" }];

function env(behaviour) {
  return {
    DB: {
      prepare() {
        return {
          all: behaviour,
        };
      },
    },
  };
}

test("prefers the sources table when it has rows", async () => {
  const rows = [{ name: "A", url: "https://a.test/feed" }];
  const feeds = await loadFeeds(env(async () => ({ results: rows })), BUNDLED);

  assert.deepEqual(feeds, rows);
});

test("falls back to the bundled feeds when the table is empty", async () => {
  // A Worker deployed before the first `cyris sources push` must keep polling.
  const feeds = await loadFeeds(env(async () => ({ results: [] })), BUNDLED);

  assert.equal(feeds, BUNDLED);
});

test("falls back to the bundled feeds when D1 errors", async () => {
  // Polling nothing is a silent outage; the digest would just look like a quiet day.
  const feeds = await loadFeeds(
    env(async () => {
      throw new Error("no such table: sources");
    }),
    BUNDLED
  );

  assert.equal(feeds, BUNDLED);
});
