import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { recordHealth } from "../src/buffer.js";
import { d1Sqlite } from "./d1-sqlite.js";

// The cases tests/test_feed_health_cases.py runs against `D1FeedHealthWriter`.
const { cases } = JSON.parse(readFileSync(new URL("./feed_health_cases.json", import.meta.url)));

const FEED_HEALTH =
  "CREATE TABLE feed_health (name TEXT PRIMARY KEY, consecutive_failures INTEGER NOT NULL DEFAULT 0, last_error TEXT, last_failed_at TEXT, last_ok_at TEXT)";

assert.ok(cases.length >= 6);

for (const { name, polls, rows } of cases) {
  test(name, async () => {
    const db = d1Sqlite();
    db.raw.exec(FEED_HEALTH);
    for (const { at, outcomes } of polls) await recordHealth(db, outcomes, at);

    const stored = db.raw.prepare("SELECT * FROM feed_health ORDER BY name").all();
    assert.deepEqual(
      Object.fromEntries(stored.map(({ name: feed, ...row }) => [feed, { ...row }])),
      rows
    );
  });
}
