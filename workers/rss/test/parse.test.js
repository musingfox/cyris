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

test("decodes numeric character references in a title", () => {
  const xml = `<rss version="2.0"><channel><item>
    <title>Nvidia CEO Jensen Huang tells Trump &#8216;we&#8217;re not going to let [an AI slowdown] happen&#8217;</title>
    <link>https://c.test/1</link>
    <pubDate>Tue, 18 Mar 2026 10:00:00 GMT</pubDate>
  </item></channel></rss>`;
  const [row] = parseFeed(xml, "C");
  assert.equal(
    row.title,
    "Nvidia CEO Jensen Huang tells Trump ‘we’re not going to let [an AI slowdown] happen’"
  );
});

function imageOf(itemXml) {
  const xml = `<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"
    xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item>
    <title>Pictured</title>
    <link>https://d.test/post/1</link>
    <pubDate>Tue, 18 Mar 2026 10:00:00 GMT</pubDate>
    ${itemXml}
  </item></channel></rss>`;
  return parseFeed(xml, "D")[0].image_url;
}

test("an entry's media:thumbnail is its image, before any other the entry names", () => {
  assert.equal(
    imageOf(`<media:thumbnail url="https://cdn.d.test/thumb.jpg"/>
      <media:content url="https://cdn.d.test/content.jpg" medium="image"/>
      <enclosure url="https://cdn.d.test/enc.jpg" type="image/jpeg"/>
      <content:encoded><![CDATA[<p><img src="https://cdn.d.test/body.jpg"></p>]]></content:encoded>`),
    "https://cdn.d.test/thumb.jpg"
  );
});

test("media:content counts only when it says it is an image", () => {
  assert.equal(
    imageOf(`<media:content url="https://cdn.d.test/talk.mp3" type="audio/mpeg"/>
      <media:content url="https://cdn.d.test/content.png" type="image/png"/>
      <enclosure url="https://cdn.d.test/enc.jpg" type="image/jpeg"/>`),
    "https://cdn.d.test/content.png"
  );
  assert.equal(imageOf(`<media:content url="https://cdn.d.test/clip.mp4"/>`), null);
});

test("a thumbnail inside media:group is read as the entry's own", () => {
  assert.equal(
    imageOf(`<media:group><media:thumbnail url="https://i.d.test/hq.jpg"/></media:group>`),
    "https://i.d.test/hq.jpg"
  );
});

test("an image enclosure is the image when no media element names one", () => {
  assert.equal(imageOf(`<enclosure url="https://cdn.d.test/ep.mp3" type="audio/mpeg"/>`), null);
  assert.equal(
    imageOf(`<enclosure url="https://cdn.d.test/enc.webp" type="image/webp"/>
      <description><![CDATA[<img src="https://cdn.d.test/body.jpg">]]></description>`),
    "https://cdn.d.test/enc.webp"
  );
});

test("else the first img in the entry's content, resolved against the entry's link", () => {
  assert.equal(
    imageOf(
      `<content:encoded><![CDATA[<p>Text</p><img class="a" src="/media/one.jpg?w=1&amp;h=2"><img src="https://cdn.d.test/two.jpg">]]></content:encoded>`
    ),
    "https://d.test/media/one.jpg?w=1&h=2"
  );
  assert.equal(
    imageOf(`<description>&lt;img src="https://cdn.d.test/escaped.jpg"&gt; body</description>`),
    "https://cdn.d.test/escaped.jpg"
  );
});

test("an entry naming no image, or only a data: one, has none", () => {
  assert.equal(imageOf(`<description>no picture here</description>`), null);
  assert.equal(
    imageOf(`<description><![CDATA[<img src="data:image/gif;base64,R0lGOD">]]></description>`),
    null
  );
  assert.equal(
    imageOf(`<description><![CDATA[<img data-src="https://x.test/a.jpg">]]></description>`),
    null
  );
});

test("an entry whose link is relative still keeps an absolute image", () => {
  const xml = `<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><item>
    <title>Relative</title>
    <link>/posts/1</link>
    <pubDate>Tue, 18 Mar 2026 10:00:00 GMT</pubDate>
    <media:thumbnail url="https://cdn.d.test/a.jpg"/>
  </item></channel></rss>`;
  assert.equal(parseFeed(xml, "D")[0].image_url, "https://cdn.d.test/a.jpg");
});

test("an Atom entry's image enclosure link is its image", () => {
  const xml = `<feed xmlns="http://www.w3.org/2005/Atom"><entry>
    <title>Atom pictured</title>
    <link rel="alternate" href="https://b.test/1"/>
    <link rel="enclosure" type="audio/mpeg" href="https://b.test/ep.mp3"/>
    <link rel="enclosure" type="image/jpeg" href="https://b.test/pic.jpg"/>
    <id>urn:uuid:2</id>
    <published>2026-03-18T10:00:00Z</published>
  </entry></feed>`;
  assert.equal(parseFeed(xml, "B")[0].image_url, "https://b.test/pic.jpg");
});
