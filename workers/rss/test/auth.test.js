import assert from "node:assert/strict";
import { test } from "node:test";

import worker from "../src/index.js";

test("an unset RSS_TOKEN refuses the literal 'Bearer undefined'", async () => {
  const request = new Request("https://rss.example/stats", {
    headers: { Authorization: "Bearer undefined" },
  });
  const response = await worker.fetch(request, {});
  assert.equal(response.status, 401);
});
