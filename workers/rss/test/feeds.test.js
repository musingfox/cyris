import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { test } from "node:test";

import { loadFeeds } from "../src/feeds.js";

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

function capturingErrors() {
  const calls = [];
  const original = console.error;
  console.error = (...args) => calls.push(args.join(" "));
  return { calls, restore: () => { console.error = original; } };
}

test("polls exactly the rows in the sources table", async () => {
  const rows = [{ name: "A", url: "https://a.test/feed" }];
  const feeds = await loadFeeds(env(async () => ({ results: rows })));

  assert.deepEqual(feeds, rows);
});

test("an empty sources table polls nothing and says how to fill it", async () => {
  // No bundled list to fall back to: a fork would poll feeds nobody chose.
  const errors = capturingErrors();
  let feeds;
  try {
    feeds = await loadFeeds(env(async () => ({ results: [] })));
  } finally {
    errors.restore();
  }

  assert.deepEqual(feeds, []);
  assert.equal(errors.calls.length, 1);
  assert.match(errors.calls[0], /cyris sources push/);
});

test("the bundled feed list is gone", () => {
  const root = new URL("../", import.meta.url);
  assert.equal(existsSync(new URL("src/feeds.json", root)), false);
  assert.equal(existsSync(new URL("gen-feeds.py", root)), false);
  const index = readFileSync(new URL("src/index.js", root), "utf8");
  assert.doesNotMatch(index, /feeds\.json/);
});
