import assert from "node:assert/strict";
import { test } from "node:test";

import { parseFeed, stripTrackingParams } from "../src/parse.js";

const RSS = `<?xml version="1.0"?>
<rss version="2.0"><channel><title>Feed</title>
  <item>
    <title>Hello</title>
    <link>https://a.test/1?utm_source=rss&amp;utm_medium=rss</link>
    <pubDate>Tue, 18 Mar 2026 10:00:00 GMT</pubDate>
    <guid>tag:a.test,1</guid>
    <description>body</description>
  </item>
  <item>
    <title>Undated</title>
    <link>https://a.test/2</link>
  </item>
</channel></rss>`;

const ATOM = `<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Atom post</title>
    <link rel="edit" href="https://b.test/edit/1"/>
    <link rel="alternate" href="https://b.test/1"/>
    <id>urn:uuid:1</id>
    <published>2026-03-18T10:00:00Z</published>
    <author><name>Ada</name></author>
    <content>atom body</content>
  </entry>
</feed>`;

test("strips tracking params so the D1 primary key stays stable", () => {
  assert.equal(
    stripTrackingParams("https://dq.yam.com/post/17015?utm_source=rss&utm_medium=rss"),
    "https://dq.yam.com/post/17015"
  );
  assert.equal(stripTrackingParams("https://a.test/1?id=7"), "https://a.test/1?id=7");
  assert.equal(stripTrackingParams("not a url"), "not a url");
});

test("parses RSS 2.0 and drops undated entries", () => {
  const rows = parseFeed(RSS, "A");
  assert.equal(rows.length, 1);
  assert.equal(rows[0].url, "https://a.test/1");
  assert.equal(rows[0].title, "Hello");
  assert.equal(rows[0].content, "body");
  assert.equal(rows[0].source_name, "A");
});

test("normalises dates to ISO8601 so the window query compares in order", () => {
  const [row] = parseFeed(RSS, "A");
  assert.equal(row.published_at, "2026-03-18T10:00:00.000Z");
  assert.ok(row.published_at > "2026-03-17T00:00:00.000Z");
  assert.ok(row.published_at < "2026-03-19T00:00:00.000Z");
});

test("parses Atom and picks the alternate link, not the edit link", () => {
  const rows = parseFeed(ATOM, "B");
  assert.equal(rows.length, 1);
  assert.equal(rows[0].url, "https://b.test/1");
  assert.equal(rows[0].author, "Ada");
  assert.equal(rows[0].content, "atom body");
});

test("malformed feed yields no rows instead of throwing", () => {
  assert.deepEqual(parseFeed("<html>not a feed</html>", "C"), []);
});
